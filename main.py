import os, json, aiohttp, asyncio, re, logging, signal, html
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Dict
from aiogram import Bot, Dispatcher, types, F
from aiogram.enums import ParseMode, ChatAction
from aiogram.filters import Command, StateFilter
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.exceptions import TelegramUnauthorizedError

# ================= КОНФІГУРАЦІЯ =================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
TOKEN = os.environ.get("BOT_TOKEN")

bot = Bot(token=TOKEN) if TOKEN else None
dp = Dispatcher(storage=MemoryStorage())

items_data = {}
is_db_ready = False
http_session: Optional[aiohttp.ClientSession] = None
scan_semaphore = asyncio.Semaphore(5)
history_cache: Dict[str, dict] = {}
is_shutting_down = False

CACHE_TTL = 3600
CITIES = ["Bridgewatch", "Martlock", "Lymhurst", "Thetford", "Fort Sterling", "Caerleon", "Brecilien", "Black Market"]
CITY_EMOJIS = {"Lymhurst":"🟢","Martlock":"🔵","Caerleon":"🔴","Thetford":"🟣","Bridgewatch":"🟠","Fort Sterling":"⚪","Brecilien":"🌸","Black Market":"⚫"}
QUALITY_NAMES = {1:"Обычное", 2:"Хорошее", 3:"Выдающееся", 4:"Отличное", 5:"Шедевр"}
TRASH = ["Знаток ","Мастер ","Великий мастер ","Старейшина ","Ученик ","Новичок "]
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

class BotState(StatesGroup):
    waiting_for_buy_limit = State()
    waiting_for_profit_limit = State()
    picking_from = State()
    picking_to = State()
    calc_count = State()
    calc_buy = State()
    calc_sell = State()
    confirm_reset = State()

# ================= СЛУЖБОВІ ФУНКЦІЇ =================
async def safe_delete(msg):
    try:
        await msg.delete()
    except Exception:
        pass

def get_item_icon(unique_name):
    un = unique_name.lower()
    if any(x in un for x in ["hood", "cowl", "helmet", "cap"]): return "🪖"
    if any(x in un for x in ["armor", "jacket", "robe", "garb"]): return "🧥"
    if any(x in un for x in ["shoes", "boots", "sandals"]): return "🥾"
    if any(x in un for x in ["sword", "axe", "bow", "staff", "hammer", "mace", "dagger", "spear", "glove"]): return "⚔️"
    if "bag" in un: return "🎒"
    if "cape" in un: return "🧣"
    if "mount" in un: return "🐴"
    return "📦"

def fmt_t(s):
    try:
        if not s: return "??"
        dt = datetime.fromisoformat(s.split(".")[0].replace("Z", "")).replace(tzinfo=timezone.utc)
        m = int((datetime.now(timezone.utc) - dt).total_seconds() / 60)
        return f"{m}м" if m < 60 else f"{m//60}г"
    except Exception:
        return "??"

async def get_item_liquidity(item_id, city, quality):
    global http_session, history_cache
    if not http_session or http_session.closed or is_shutting_down:
        logger.warning(f"Ліквідність: сесія недоступна для {item_id}")
        return 0, 0

    cache_key = f"{item_id}|{city}|{quality}"
    now = datetime.now(timezone.utc)
    if cache_key in history_cache and (now - history_cache[cache_key]['time']).total_seconds() < CACHE_TTL:
        return history_cache[cache_key]['volume'], history_cache[cache_key]['avg_p']

    url = f"https://europe.albion-online-data.com/api/v2/stats/history/{item_id}?locations={city}&time-series=1&qualities={quality}"
    async with scan_semaphore:
        try:
            async with http_session.get(url, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data and isinstance(data, list) and len(data) > 0:
                        history = data[0].get('data', [])
                        vol_24h, vol_total = 0, 0
                        price_vol_24h, price_vol_total = 0, 0
                        entries_24h, entries_total = 0, 0

                        for day in history:
                            v = day.get('item_count', 0)
                            p = day.get('avg_price') or day.get('average_price', 0)
                            if v <= 0 or p <= 0: continue
                            try:
                                ts = datetime.fromisoformat(day['timestamp'].replace("Z", "+00:00"))
                                is_recent = (now - ts).total_seconds() <= 86400
                            except:
                                is_recent = False
                            if is_recent:
                                vol_24h += v
                                price_vol_24h += (p * v)
                                entries_24h += 1
                            vol_total += v
                            price_vol_total += (p * v)
                            entries_total += 1

                        if vol_24h > 0:
                            res_vol = vol_24h
                            res_p = int(price_vol_24h / vol_24h)
                        elif vol_total > 0:
                            res_vol = int(vol_total / max(entries_total, 1))
                            res_p = int(price_vol_total / vol_total)
                        else:
                            res_vol, res_p = 0, 0

                        history_cache[cache_key] = {'volume': res_vol, 'avg_p': res_p, 'time': now}
                        if res_vol > 0 or res_p > 0:
                            logger.info(f"Ліквідність: {item_id} в {city} якість {quality} -> об'єм {res_vol}, сер.ціна {res_p}")
                        else:
                            logger.info(f"Ліквідність: {item_id} в {city} якість {quality} -> немає даних")
                        return res_vol, res_p
                else:
                    logger.warning(f"Помилка отримання історії {item_id}: HTTP {resp.status}")
        except asyncio.TimeoutError:
            logger.warning(f"Таймаут історії {item_id}")
        except Exception as e:
            logger.error(f"Помилка історії {item_id}: {e}")
    return 0, 0

async def download_items():
    global items_data, is_db_ready, http_session
    try:
        async with http_session.get(
            "https://raw.githubusercontent.com/ao-data/ao-bin-dumps/master/formatted/items.json",
            timeout=60
        ) as r:
            if r.status == 200:
                data = await r.json(content_type=None)
                allowed = ["weapon","armor","plate","leather","cloth","bag","cape","potion","meal","mount","tool","offhand"]
                items_data = {
                    i["UniqueName"]: i
                    for i in data
                    if i.get("UniqueName","").startswith(("T4_", "T5_", "T6_", "T7_", "T8_"))
                    and any(x in i.get("UniqueName","").lower() for x in allowed)
                }
                is_db_ready = True
                logger.info(f"Базу предметів завантажено: {len(items_data)} позицій")
    except Exception as e:
        logger.error(f"Помилка завантаження предметів: {e}")
async def scan_logic(d, f_c=None, t_c=None):
    if not items_data or not http_session or is_shutting_down:
        logger.warning("Сканування неможливе: немає даних або сесії")
        return []

    pre_res = []
    b_l = d.get("buy_limit", 0)
    p_l = d.get("profit_limit", 4000)
    ext = d.get("extra", False)
    check_liq = d.get("check_liq", False)
    # Збільшено до 720 хвилин (12 годин) для ширшого охоплення
    MAX_AGE_MINUTES = 720

    i_list = list(items_data.keys())
    cities = [f_c, t_c] if f_c and t_c else CITIES

    logger.info(f"Сканування: бюджет {b_l}, профіт {p_l}, ext={ext}, check_liq={check_liq}")

    for i in range(0, len(i_list), 50):
        if is_shutting_down:
            break
        chunk = i_list[i:i+50]
        url = f"https://europe.albion-online-data.com/api/v2/stats/prices/{','.join(chunk)}?locations={','.join(cities)}"
        data = None
        async with scan_semaphore:
            try:
                async with http_session.get(url, timeout=15) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                    else:
                        logger.warning(f"Помилка цін з API, статус {resp.status}")
            except Exception as e:
                logger.error(f"Помилка запиту цін: {e}")
                continue

        if not data:
            continue

        now = datetime.now(timezone.utc)
        grouped = {}
        for e in data:
            k = f"{e['item_id']}|{e['quality']}"
            grouped.setdefault(k, {})[e['city']] = e

        for k, c_d in grouped.items():
            i_id, q = k.split("|")
            srcs = [f_c] if f_c else [c for c in c_d if c != "Black Market"]
            for sc in srcs:
                if sc not in c_d:
                    continue
                buy = c_d[sc].get('sell_price_min', 0)
                # Фікс бюджету: перевіряємо тільки якщо бюджет > 0
                if buy <= 500:
                    continue
                if b_l > 0 and buy > b_l:
                    continue
                try:
                    b_dt = datetime.fromisoformat(c_d[sc]['sell_price_min_date'].split(".")[0]).replace(tzinfo=timezone.utc)
                    age_b = (now - b_dt).total_seconds() / 60
                    if age_b > MAX_AGE_MINUTES:
                        continue
                except:
                    continue

                targets = [t_c] if t_c else [c for c in c_d if c != sc]
                for tc in targets:
                    if tc not in c_d:
                        continue
                    is_bm = (tc == "Black Market")
                    sell = c_d[tc].get('buy_price_max' if is_bm else 'sell_price_min', 0)
                    if sell <= buy:
                        continue
                    try:
                        sk = 'buy_price_max_date' if is_bm else 'sell_price_min_date'
                        s_dt = datetime.fromisoformat(c_d[tc][sk].split(".")[0]).replace(tzinfo=timezone.utc)
                        age_s = (now - s_dt).total_seconds() / 60
                        if age_s > MAX_AGE_MINUTES:
                            continue
                    except:
                        continue

                    tax = 0.91 if is_bm else 0.895
                    p_n = int(sell * tax - buy)
                    p_p = int(sell * (tax + 0.04) - buy)

                    if p_n >= p_l:
                        if ext and (age_b > 30 or age_s > 30):
                            continue
                        pre_res.append({
                            'id': i_id,
                            'q': int(q),
                            'from': sc,
                            'to': tc,
                            'buy': buy,
                            'sell': sell,
                            'p_p': p_p,
                            'p_n': p_n,
                            'bd': c_d[sc]['sell_price_min_date'],
                            'sd': c_d[tc][sk]
                        })

    logger.info(f"Кандидатів після фільтрації цін: {len(pre_res)}")
    pre_res.sort(key=lambda x: x['p_n'], reverse=True)
    candidates_for_liquidity = pre_res[:300]
    enriched = []

    logger.info(f"Перед ліквідністю: {len(candidates_for_liquidity)} кандидатів")

    for item in candidates_for_liquidity:
        vol, avg_p = await get_item_liquidity(item['id'], item['to'], item['q'])

        # Антифейк тепер 4x замість 3x
        if avg_p > 0 and item['sell'] > (avg_p * 4):
            logger.info(f"Відкинуто {item['id']} як пастка: ціна продажу {item['sell']} > 4*сер.ціна {avg_p}")
            continue

        # Розумна ліквідність
        if check_liq:
            min_vol = 2 if item['buy'] > 100000 else 5
            if vol < min_vol:
                logger.debug(f"Відкинуто {item['id']} через низький об'єм ({vol})")
                continue

        item['vol'] = vol
        item['avg_p'] = avg_p
        # Новий score: прибуток * (1 + об'єм/20)
        item['score'] = int(item['p_n'] * (1 + vol / 20))
        enriched.append(item)

    logger.info(f"Після перевірки ліквідності: {len(enriched)}")
    dedup = {}
    for item in enriched:
        key = f"{item['id']}|{item['q']}"
        if key not in dedup or item['score'] > dedup[key]['score']:
            dedup[key] = item

    final_list = sorted(dedup.values(), key=lambda x: x['score'], reverse=True)[:15]
    if not final_list:
        logger.warning("❌ Нічого не знайдено після всіх фільтрів")
    else:
        logger.info(f"Фінальний список: {len(final_list)} унікальних предметів")
    return final_list

async def disp_res(msg: types.Message, res: list, d: dict):
    if not res:
        await msg.answer("📭 Нічого не знайдено. Спробуйте змінити фільтри.")
        return

    await msg.answer(f"🔎 Знайдено <b>{len(res)}</b> результатів:", parse_mode=ParseMode.HTML)

    messages, full_text = [], ""
    for idx, r in enumerate(res, 1):
        id_parts = r['id'].split("@")
        b_id = id_parts[0]
        tier = b_id.split('_')[0][1:]
        enc = id_parts[1] if len(id_parts) > 1 else "0"
        icon = get_item_icon(b_id)
        name = items_data.get(b_id, {}).get("LocalizedNames", {}).get("RU-RU", b_id)
        name = re.sub(r'\s*\([^)]*\)', '', html.escape(name.upper()))
        for t in TRASH:
            name = name.replace(t, "")

        tbd = fmt_t(r.get('bd'))
        tsd = fmt_t(r.get('sd'))

        liq = r.get('vol', 0)
        lbl = "🔥" if liq > 100 else ("⚡" if liq > 30 else ("✅" if liq > 5 else "🐢"))

        avg_str = f"{r['avg_p']:,}" if r['avg_p'] > 0 else "???"

        item_block = (
            f"{idx}) {icon} <b>{name}</b> [{tier}.{enc}]\n"
            f"✨ {QUALITY_NAMES.get(r['q'], 'Обычное')}\n"
            f"📥 {CITY_EMOJIS[r['from']]} {r['buy']:,} | 🕒 {tbd}\n"
            f"📤 {CITY_EMOJIS[r['to']]} {r['sell']:,} | 🕒 {tsd}\n"
            f"<pre>"
            f"Прибуток:\n"
            f"{f'👑 {r['p_p']:,}':<17} Попит: {lbl} {liq} шт/д\n"
            f"{f'💀 {r['p_n']:,}':<17} Сер.ціна: {avg_str}"
            f"</pre>\n"
            f"───────────────────\n\n"
        )

        if len(full_text) + len(item_block) > 3900:
            messages.append(full_text)
            full_text = item_block
        else:
            full_text += item_block

    if full_text:
        messages.append(full_text)

    for t in messages:
        await msg.answer(t, parse_mode=ParseMode.HTML)

    await msg.answer(f"📊 Усього знайдено <b>{len(res)}</b> позицій.", parse_mode=ParseMode.HTML)

# ================= КНОПКИ ТА ОБРОБНИКИ (без змін) =================
def get_main_kb(d):
    mode = d.get("mode")
    budget = d.get("buy_limit", 0)
    searched = d.get("has_searched", False)

    m_btn = "Режим: 🌍 Всі міста" if mode == "all" else ("Режим: 📍 Шлях" if mode == "custom" else "🗺 Режим")
    kb = []
    if budget > 0 and mode:
        kb.append([KeyboardButton(text="🚀 Запустити сканер")])
    if searched:
        kb.append([KeyboardButton(text=m_btn), KeyboardButton(text=f"⚡ 30хв: {'ON' if d.get('extra') else 'OFF'}")])
        kb.append([
            KeyboardButton(text=f"📊 Попит Ліміт: {'ON' if d.get('check_liq') else 'OFF'}"),
            KeyboardButton(text="🧮 Калькулятор")
        ])
        kb.append([KeyboardButton(text="💰 Бюджет"), KeyboardButton(text="🔄 Скинути")])
    else:
        kb.append([KeyboardButton(text="💰 Бюджет"), KeyboardButton(text=m_btn)])
        kb.append([KeyboardButton(text="🧮 Калькулятор"), KeyboardButton(text="❓ Допомога")])
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

@dp.message(Command("start"), StateFilter('*'))
async def cmd_start(m: types.Message, state: FSMContext):
    await state.clear()
    await m.answer("👋 <b>Привіт! Я Albion Trade Bot.</b>\nВкажіть Бюджет та Режим.", parse_mode=ParseMode.HTML, reply_markup=get_main_kb({}))

@dp.message(F.text == "🚀 Запустити сканер", StateFilter('*'))
async def main_search(m: types.Message, state: FSMContext):
    d = await state.get_data()
    if not is_db_ready:
        return await m.answer("⏳ База предметів ще завантажується...")
    await bot.send_chat_action(m.chat.id, ChatAction.TYPING)
    s_msg = await m.answer("🔍 Шукаю вигідні маршрути...")
    res = await scan_logic(d, d.get('f_c'), d.get('t_c'))
    await safe_delete(s_msg)

    await state.update_data(has_searched=True)
    d['has_searched'] = True

    await disp_res(m, res, d)
    await m.answer("✅ Сканування завершено", reply_markup=get_main_kb(d))

@dp.message(F.text.startswith("Режим:") | (F.text == "🗺 Режим"), StateFilter('*'))
async def choose_mode(m: types.Message, state: FSMContext):
    await m.answer("Оберіть режим:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎲 Всі міста", callback_data="set_mode_all")],
        [InlineKeyboardButton(text="📍 Шлях", callback_data="set_mode_custom")]
    ]))

@dp.callback_query(F.data.startswith("set_mode_"))
async def set_mode_cb(cb: types.CallbackQuery, state: FSMContext):
    m_type = cb.data.split("_")[2]
    if m_type == "all":
        await state.update_data(mode="all")
        await cb.message.edit_text("🌍 Режим: Всі міста!")
        await cb.message.answer("Оновлено", reply_markup=get_main_kb(await state.get_data()))
    else:
        await state.set_state(BotState.picking_from)
        await cb.message.edit_text("Звідки:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"{CITY_EMOJIS[c]} {c}", callback_data=f"city_{c}")] for c in CITIES if c != "Black Market"
        ]))

@dp.callback_query(F.data.startswith("city_"))
async def city_pick(cb: types.CallbackQuery, state: FSMContext):
    curr = await state.get_state()
    c = cb.data.split("_")[1]
    if curr == BotState.picking_from.state:
        await state.update_data(f_c=c)
        await state.set_state(BotState.picking_to)
        await cb.message.edit_text(f"З: {c}. Куди:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"{CITY_EMOJIS[ci]} {ci}", callback_data=f"city_{ci}")] for ci in CITIES if ci != c
        ]))
    elif curr == BotState.picking_to.state:
        await state.update_data(t_c=c, mode="custom")
        await state.set_state(None)
        await cb.message.edit_text(f"📍 Шлях збережено!")
        await cb.message.answer("Оновлено", reply_markup=get_main_kb(await state.get_data()))

@dp.message(F.text == "💰 Бюджет", StateFilter('*'))
async def limit_menu(m: types.Message, state: FSMContext):
    d = await state.get_data()
    await m.answer(
        f"⚙️ Бюджет: {d.get('buy_limit',0):,}\n📈 Профіт: {d.get('profit_limit',4000):,}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💰 Бюджет", callback_data="set_limit_buy"),
             InlineKeyboardButton(text="📈 Профіт", callback_data="set_limit_profit")]
        ])
    )

@dp.callback_query(F.data.startswith("set_limit_"))
async def set_limit_cb(cb: types.CallbackQuery, state: FSMContext):
    t = cb.data.split("_")[2]
    await state.set_state(BotState.waiting_for_buy_limit if t == "buy" else BotState.waiting_for_profit_limit)
    await cb.message.answer("Введіть число:", reply_markup=ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="❌ Скасувати")]], resize_keyboard=True
    ))
    await cb.answer()

@dp.message(F.text == "🧮 Калькулятор", StateFilter('*'))
async def calc_start(m: types.Message, state: FSMContext):
    await state.set_state(BotState.calc_count)
    await m.answer("📦 Кількість:", reply_markup=ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="❌ Скасувати")]], resize_keyboard=True
    ))

@dp.message(F.text == "🔄 Скинути", StateFilter('*'))
async def reset_confirm(m: types.Message, state: FSMContext):
    await state.set_state(BotState.confirm_reset)
    await m.answer("Скинути всі дані?", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Так", callback_data="reset_yes"),
         InlineKeyboardButton(text="❌ Ні", callback_data="reset_no")]
    ]))

@dp.callback_query(F.data.startswith("reset_"), StateFilter(BotState.confirm_reset))
async def reset_action(cb: types.CallbackQuery, state: FSMContext):
    if cb.data == "reset_yes":
        await state.clear()
        await cb.message.edit_text("🔄 Скинуто.")
        await cb.message.answer("Почнемо заново?", reply_markup=get_main_kb({}))
    else:
        await cb.message.edit_text("🚫 Скасовано.")
    await state.set_state(None)

@dp.message(StateFilter(BotState.waiting_for_buy_limit, BotState.waiting_for_profit_limit,
                         BotState.calc_count, BotState.calc_buy, BotState.calc_sell))
async def numeric_handler(m: types.Message, state: FSMContext):
    if m.text == "❌ Скасувати":
        await state.set_state(None)
        return await m.answer("Скасовано", reply_markup=get_main_kb(await state.get_data()))

    try:
        v = int(m.text.replace(" ", ""))
        curr = await state.get_state()
        d = await state.get_data()

        if "buy_limit" in str(curr):
            await state.update_data(buy_limit=v)
            await state.set_state(None)
        elif "profit_limit" in str(curr):
            await state.update_data(profit_limit=v)
            await state.set_state(None)
        elif "calc_count" in str(curr):
            await state.update_data(c=v)
            await state.set_state(BotState.calc_buy)
            return await m.answer("📥 Ціна КУПІВЛІ:")
        elif "calc_buy" in str(curr):
            await state.update_data(b=v)
            await state.set_state(BotState.calc_sell)
            return await m.answer("📤 Ціна ПРОДАЖУ:")
        elif "calc_sell" in str(curr):
            await state.set_state(None)
            p_p = int((v * 0.935) - d['b']) * d['c']
            p_n = int((v * 0.895) - d['b']) * d['c']
            await m.answer(f"📊 Результат:\n👑 Пр: <b>{p_p:,}</b>\n💀 Пр: <b>{p_n:,}</b>",
                           reply_markup=get_main_kb(d), parse_mode=ParseMode.HTML)
            return

        await m.answer(f"✅ Збережено: {v:,}", reply_markup=get_main_kb(await state.get_data()))
    except ValueError:
        await m.answer("❌ Введіть ціле число!")

@dp.message(F.text.regexp(r"⚡ 30хв:|📊 Попит Ліміт:"), StateFilter('*'))
async def toggles(m: types.Message, state: FSMContext):
    d = await state.get_data()
    key = "extra" if "⚡" in m.text else "check_liq"
    val = not d.get(key, False)
    await state.update_data({key: val})
    await m.answer("Змінено", reply_markup=get_main_kb(await state.get_data()))

async def main():
    global http_session
    if not TOKEN:
        logger.critical("BOT_TOKEN не задано! Зупиняюсь.")
        return

    http_session = aiohttp.ClientSession(headers=HEADERS)
    asyncio.create_task(download_items())

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    except TelegramUnauthorizedError:
        logger.critical("❌ Токен бота недійсний або відкликаний. Перевірте BOT_TOKEN.")
    except Exception as e:
        logger.exception(f"Критична помилка під час запуску: {e}")
    finally:
        if http_session and not http_session.closed:
            await http_session.close()
        if bot and hasattr(bot, 'session') and bot.session:
            await bot.session.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Бот зупинений вручну.")