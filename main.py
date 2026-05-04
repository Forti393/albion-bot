import os, json, aiohttp, asyncio, re, logging, html
from datetime import datetime, timezone
from typing import List, Optional
from aiogram import Bot, Dispatcher, types, F
from aiogram.enums import ParseMode, ChatAction
from aiogram.filters import Command, StateFilter
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

# ================= ЛОГУВАННЯ =================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ================= КОНФІГУРАЦІЯ =================
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0")) 
TOKEN = os.environ.get("BOT_TOKEN")

if not TOKEN:
    logger.error("🚨 BOT_TOKEN відсутній!")
    exit(1)

bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# Глобальні змінні
items_data = {}; is_db_ready = False
http_session: Optional[aiohttp.ClientSession] = None 
scan_semaphore = asyncio.Semaphore(5) 
history_cache = {}
is_shutting_down = False

# Константи
CACHE_TTL = 3600 
MAX_CACHE_SIZE = 4000
CITIES = ["Bridgewatch", "Martlock", "Lymhurst", "Thetford", "Fort Sterling", "Caerleon", "Brecilien", "Black Market"]
CITY_EMOJIS = {"Lymhurst":"🟢","Martlock":"🔵","Caerleon":"🔴","Thetford":"🟣","Bridgewatch":"🟠","Fort Sterling":"⚪","Brecilien":"🌸","Black Market":"⚫"}
QUALITY_NAMES = {1:"Обычное", 2:"Хорошее", 3:"Выдающееся", 4:"Отличное", 5:"Шедевр"}
TRASH = ["Знаток ","Мастер ","Великий мастер ","Старейшина ","Ученик ","Новичок "]
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

class BotState(StatesGroup):
    waiting_for_buy_limit = State(); waiting_for_profit_limit = State()
    picking_from = State(); picking_to = State()
    calc_count = State(); calc_buy = State(); calc_sell = State()
    confirm_reset = State()

# ================= СЛУЖБОВІ ФУНКЦІЇ =================
async def safe_delete(msg):
    try: await msg.delete()
    except Exception: pass

def clean_history_cache():
    global history_cache
    if len(history_cache) > MAX_CACHE_SIZE:
        now = datetime.now(timezone.utc)
        history_cache = {k: v for k, v in history_cache.items() if (now - v['time']).total_seconds() < CACHE_TTL}
        if len(history_cache) > MAX_CACHE_SIZE: history_cache.clear()

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
        dt = datetime.fromisoformat(s.split(".")[0].replace("Z","")).replace(tzinfo=timezone.utc)
        m = int((datetime.now(timezone.utc)-dt).total_seconds()/60)
        return f"{m}м" if m < 60 else f"{m//60}г"
    except Exception: return "??"

async def get_item_liquidity(item_id, city):
    global http_session
    if not http_session or http_session.closed or is_shutting_down: return 0, 0
    clean_history_cache()
    
    cache_key, now = f"{item_id}|{city}", datetime.now(timezone.utc)
    if cache_key in history_cache and (now - history_cache[cache_key]['time']).total_seconds() < CACHE_TTL:
        c_data = history_cache[cache_key]
        return c_data['vol'], c_data['avg']
        
    url = f"https://europe.albion-online-data.com/api/v2/stats/history/{item_id}?locations={city}&time-series=1"
    async with scan_semaphore:
        try:
            async with http_session.get(url, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data and data[0].get('data'):
                        last_data = data[0]['data'][-1]
                        vol = last_data.get('item_count', 0)
                        avg = last_data.get('avg_price', 0)
                        history_cache[cache_key] = {'vol': vol, 'avg': avg, 'time': now}
                        return vol, avg
        except aiohttp.ClientError as e: logger.warning(f"HTTP Error: {e}")
        except asyncio.TimeoutError: logger.warning("Timeout in history API")
        except Exception as e: logger.exception(f"Unexpected error in history API: {e}")
    return 0, 0

async def download_items():
    global items_data, is_db_ready, http_session
    try:
        async with http_session.get("https://raw.githubusercontent.com/ao-data/ao-bin-dumps/master/formatted/items.json", timeout=60) as r:
            if r.status == 200:
                data = await r.json(content_type=None)
                allowed = ["weapon","armor","plate","leather","cloth","bag","cape","potion","meal","mount","tool","offhand"]
                items_data = {i["UniqueName"]: i for i in data if i.get("UniqueName","").startswith(("T4_","T5_","T6_","T7_","T8_")) and any(x in i.get("UniqueName","").lower() for x in allowed)}
                is_db_ready = True
                logger.info("✅ БД завантажена!")
            else:
                logger.error(f"Помилка БД: Статус {r.status}")
                is_db_ready = False
    except Exception as e:
        logger.exception(f"Помилка завантаження БД: {e}")
        is_db_ready = False

async def scan_logic(d, f_c=None, t_c=None):
    global http_session
    if not items_data or not http_session or http_session.closed or is_shutting_down: return []
    pre_res = []; b_l, p_l = d.get("buy_limit", 0), d.get("profit_limit", 4000)
    ext, check_liq = d.get("extra", False), d.get("check_liq", False)
    
    i_list = list(items_data.keys())
    cities_to_fetch = [f_c, t_c] if f_c and t_c else CITIES
    
    for i in range(0, len(i_list), 50):
        if is_shutting_down: break
        url = f"https://europe.albion-online-data.com/api/v2/stats/prices/{','.join(i_list[i:i+50])}?locations={','.join(cities_to_fetch)}"
        data = None
        async with scan_semaphore:
            for attempt in range(3):
                try:
                    async with http_session.get(url, timeout=20) as resp:
                        if resp.status == 200: data = await resp.json(); break
                        if resp.status == 429: await asyncio.sleep(2 ** attempt); continue
                except Exception: await asyncio.sleep(1)
        if not data: continue
        now = datetime.now(timezone.utc)
        grouped = {}
        for e in data:
            k = f"{e['item_id']}|{e['quality']}"; grouped.setdefault(k, {})[e['city']] = e
            
        for k, c_d in grouped.items():
            try:
                parts = k.split("|")
                if len(parts) != 2: continue
                i_id, q = parts
                
                # Фільтруємо звідки ми можемо купувати (ніколи з Black Market)
                srcs = [f_c] if f_c else [c for c in c_d if c != "Black Market"]
                
                for sc in srcs:
                    if sc not in c_d or sc == "Black Market": continue
                    buy = c_d[sc].get('sell_price_min', 0)
                    if buy <= 500 or buy > b_l: continue
                    bd_str = c_d[sc]['sell_price_min_date']
                    b_dt = datetime.fromisoformat(bd_str.split(".")[0].replace("Z","")).replace(tzinfo=timezone.utc)
                    if (now-b_dt).total_seconds()/60 > 180: continue
                    
                    targets = [t_c] if t_c else [c for c in c_d if c != sc]
                    for tc in targets:
                        if tc not in c_d: continue
                        sk = 'buy_price_max_date' if tc == "Black Market" else 'sell_price_min_date'
                        sell = c_d[tc].get('buy_price_max' if tc == "Black Market" else 'sell_price_min', 0)
                        if sell <= buy or (sell/buy) > 10: continue
                        sd_str = c_d[tc].get(sk)
                        if not sd_str: continue
                        s_dt = datetime.fromisoformat(sd_str.split(".")[0].replace("Z","")).replace(tzinfo=timezone.utc)
                        if (now-s_dt).total_seconds()/60 > 180: continue
                        p_n = int(sell*0.895-buy)
                        if p_n >= p_l:
                            if ext and ((now-b_dt).total_seconds()/60 > 30 or (now-s_dt).total_seconds()/60 > 30): continue
                            pre_res.append({'id':i_id,'q':int(q),'from':sc,'to':tc,'buy':buy,'sell':sell,
                                            'p_p':int(sell*0.935-buy),'p_n':p_n,'bd':bd_str,'sd':sd_str})
            except Exception as e:
                logger.error(f"Error parsing item data: {e}")

    if check_liq and pre_res:
        pre_res.sort(key=lambda x: x['p_n'], reverse=True)
        res_liq = []
        for item in pre_res[:25]:
            safe_id = item['id'].split("@")[0] if "@" in item['id'] else item['id']
            vol, avg = await get_item_liquidity(safe_id, item['to'])
            profit_margin = item['p_n'] / item['buy']
            if vol > 0 or profit_margin <= 0.15:
                item['vol'] = vol
                item['avg'] = avg
                res_liq.append(item)
        return res_liq[:15]
    return pre_res
async def disp_res(msg, res, d):
    res.sort(key=lambda x: x['p_n'], reverse=True)
    show_liq = d.get("check_liq")
    messages, full_text = [], ""
    for idx, r in enumerate(res[:15], 1):
        try:
            b_id = r['id'].split("@")[0]
            enc = r['id'].split("@")[1] if "@" in r['id'] else "0"
            icon = get_item_icon(b_id)
            tier = b_id.split('_')[0][1:] if '_' in b_id else "4"
            
            name = items_data.get(b_id, {}).get("LocalizedNames", {}).get("RU-RU", b_id)
            name = re.sub(r'\s*\([^)]*\)', '', html.escape(name.upper()))
            for t in TRASH: name = name.replace(t, "")
            
            tbd, tsd = fmt_t(r.get('bd')), fmt_t(r.get('sd'))
            p_p_str, p_n_str = f"{r['p_p']:,}", f"{r['p_n']:,}"
            
            liq_part = f"Попит: {r.get('vol', 0)} шт/д" if show_liq else ""
            avg_part = f"Сер.ціна: {int(r.get('avg', 0)):,}" if show_liq else ""
            
            item_block = (
                f"{idx}) {icon} <b>{name}</b> [{tier}.{enc}]\n"
                f"✨ {QUALITY_NAMES.get(r['q'], 'Обычное')}\n"
                f"📥 {CITY_EMOJIS[r['from']]} {r['buy']:,} | 🕒 {tbd}\n"
                f"📤 {CITY_EMOJIS[r['to']]} {r['sell']:,} | 🕒 {tsd}\n"
                f"<pre>\n"
                f"Прибуток:\n"
                f"👑 {p_p_str:<14} {liq_part}\n"
                f"💀 {p_n_str:<14} {avg_part}\n"
                f"</pre>\n"
                f"───────────────────\n\n"
            )
            if len(full_text) + len(item_block) > 3900: messages.append(full_text); full_text = item_block
            else: full_text += item_block
        except Exception as e:
            logger.error(f"Render error for {r.get('id')}: {e}")
            
    if full_text: messages.append(full_text)
    for t in messages: await msg.answer(t, parse_mode=ParseMode.HTML)

def get_main_kb(d):
    mode, budget, searched = d.get("mode"), d.get("buy_limit", 0), d.get("has_searched", False)
    
    if not mode: m_btn = "🗺 Режим"
    elif mode == "all": m_btn = "Режим: 🌍 Всі міста"
    else: m_btn = "Режим: 📍 Шлях"

    kb = []
    if budget > 0 and mode:
        kb.append([KeyboardButton(text="🚀 Запустити сканер")])

    if searched:
        e_l = f"⚡ 30хв: {'ON' if d.get('extra') else 'OFF'}"
        liq_l = f"📊 Попит: {'ON' if d.get('check_liq') else 'OFF'}"
        kb.append([KeyboardButton(text=m_btn), KeyboardButton(text=e_l)])
        kb.append([KeyboardButton(text=liq_l), KeyboardButton(text="🧮 Калькулятор")])
        kb.append([KeyboardButton(text="💰 Бюджет"), KeyboardButton(text="🔄 Скинути")])
    else:
        kb.append([KeyboardButton(text="💰 Бюджет"), KeyboardButton(text=m_btn)])
        help_btn = [KeyboardButton(text="❓ Допомога")] if not (budget > 0 and mode) else []
        kb.append([KeyboardButton(text="🧮 Калькулятор")] + help_btn)

    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

@dp.message(Command("start"), StateFilter('*'))
async def cmd_start(m, state: FSMContext):
    await state.clear()
    welcome = (
        "👋 <b>Привіт! Я Albion Trade Bot.</b>\n\n"
        "Для початку роботи виконай два кроки:\n"
        "1️⃣ Натисни <b>💰 Бюджет</b> (це макс. ціна за одиницю товару).\n"
        "2️⃣ Обери <b>🗺 Режим</b> пошуку.\n\n"
        "Після цього з'явиться кнопка запуску сканера."
    )
    await m.answer(welcome, parse_mode=ParseMode.HTML, reply_markup=get_main_kb({}))

@dp.message(Command("help"), StateFilter('*'))
@dp.message(F.text == "❓ Допомога", StateFilter('*'))
async def cmd_help(m, state: FSMContext):
    help_text = (
        "📖 <b>Інструкція:</b>\n"
        "• <b>Бюджет:</b> Обмежує ціну купівлі одного предмета.\n"
        "• <b>Режим:</b> 'Всі міста' шукають скрізь, 'Шлях' — між двома точками.\n"
        "• <b>⚡ 30хв:</b> Фільтрує тільки найсвіжіші ціни.\n"
        "• <b>📊 Попит:</b> Аналізує об'єм продажів та виводить середню ціну.\n"
        "• <b>🧮 Калькулятор:</b> Розрахунок чистого прибутку."
    )
    await m.answer(help_text, parse_mode=ParseMode.HTML)

@dp.message(F.text == "🚀 Запустити сканер", StateFilter('*'))
async def main_search(m, state: FSMContext):
    d = await state.get_data()
    if not is_db_ready: return await m.answer("⏳ БД вантажиться або недоступна. Зачекай.")
    await bot.send_chat_action(m.chat.id, ChatAction.TYPING)
    s_msg = await m.answer("🔍 Шукаю..."); res = await scan_logic(d, d.get('f_c'), d.get('t_c'))
    await safe_delete(s_msg)
    
    if not d.get("has_searched"): await state.update_data(has_searched=True)
    
    if not res: await m.answer("📭 Порожньо")
    else: await disp_res(m, res, d)
    
    await m.answer("✅ Готово!", reply_markup=get_main_kb(await state.get_data()))

@dp.message(F.text.startswith("Режим:") | (F.text == "🗺 Режим"), StateFilter('*'))
async def choose_mode(m, state):
    await m.answer("Оберіть режим:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎲 Всі міста", callback_data="set_mode_all")],
        [InlineKeyboardButton(text="📍 Шлях", callback_data="set_mode_custom")]
    ]))

@dp.callback_query(F.data.startswith("set_mode_"))
async def set_mode_cb(cb, state: FSMContext):
    m = cb.data.split("_")[2]; await state.update_data(mode=m); await cb.answer()
    d = await state.get_data()
    if m == "all": 
        await cb.message.answer("🌍 Режим: Всі міста!", reply_markup=get_main_kb(d))
        if d.get("buy_limit", 0) > 0: await cb.message.answer("✨ Вдалого пошуку!")
    else: 
        await state.set_state(BotState.picking_from)
        # Звідки: Всі міста ОКРІМ Black Market
        await cb.message.answer("Звідки:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"{CITY_EMOJIS[c]} {c}", callback_data=f"city_{c}")] for c in CITIES if c!="Black Market"
        ]))

@dp.callback_query(F.data.startswith("city_"))
async def city_pick(cb, state: FSMContext):
    await cb.answer(); curr = await state.get_state(); c = cb.data.split("_")[1]
    if curr and "picking_from" in str(curr):
        await state.update_data(f_c=c); await state.set_state(BotState.picking_to)
        # Куди: Всі міста ОКРІМ обраного, АЛЕ Black Market ТУТ Є
        await cb.message.edit_text(f"З: {c}. Куди:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"{CITY_EMOJIS[ci]} {ci}", callback_data=f"city_{ci}")] for ci in CITIES if ci!=c
        ]))
    elif curr and "picking_to" in str(curr):
        await state.update_data(t_c=c, mode="custom"); await state.set_state(None)
        d = await state.get_data()
        await cb.message.answer(f"📍 Шлях {c} активовано!", reply_markup=get_main_kb(d))
        if d.get("buy_limit", 0) > 0: await cb.message.answer("✨ Вдалого пошуку!")

@dp.message(F.text == "💰 Бюджет", StateFilter('*'))
async def limit_menu(m, state: FSMContext):
    d = await state.get_data(); b, p = d.get("buy_limit", 0), d.get("profit_limit", 4000)
    await m.answer(f"⚙️ Бюджет: {b:,}\n📈 Профіт: {p:,}", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💰 Бюджет", callback_data="set_limit_buy"), InlineKeyboardButton(text="📈 Профіт", callback_data="set_limit_profit")]
    ]))

@dp.message(F.text == "🧮 Калькулятор", StateFilter('*'))
async def calc_start(m, state: FSMContext):
    await state.set_state(BotState.calc_count)
    await m.answer("📦 Введи кількість предметів:", reply_markup=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="❌ Скасувати")]], resize_keyboard=True))

@dp.message(F.text == "🔄 Скинути", StateFilter('*'))
async def reset_confirm(m, state: FSMContext):
    await state.set_state(BotState.confirm_reset)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Так", callback_data="reset_yes"), 
         InlineKeyboardButton(text="❌ Ні", callback_data="reset_no")]
    ])
    await m.answer("⚠️ <b>Це скине всі введені дані!</b>\nПродовжити?", parse_mode=ParseMode.HTML, reply_markup=kb)

@dp.callback_query(F.data.startswith("reset_"), StateFilter(BotState.confirm_reset))
async def reset_action(cb, state: FSMContext):
    if cb.data == "reset_yes":
        await state.clear()
        await cb.message.edit_text("🔄 Дані скинуто.")
        await cb.message.answer("Почнемо спочатку?", reply_markup=get_main_kb({}))
    else:
        await cb.message.edit_text("🚫 Скасовано.")
    await state.set_state(None); await cb.answer()

@dp.message(StateFilter(BotState.waiting_for_buy_limit, BotState.waiting_for_profit_limit, BotState.calc_count, BotState.calc_buy, BotState.calc_sell))
async def numeric_handler(m, state: FSMContext):
    if m.text == "❌ Скасувати":
        await state.set_state(None)
        return await m.answer("Скасовано", reply_markup=get_main_kb(await state.get_data()))
    try:
        v = int(m.text.replace(" ","")); curr = await state.get_state()
        if "waiting_for_buy_limit" in str(curr):
            await state.update_data(buy_limit=v); await state.set_state(None)
            d = await state.get_data()
            await m.answer(f"✅ Бюджет: {v:,}", reply_markup=get_main_kb(d))
            if d.get("mode"): await m.answer("✨ Вдалого пошуку!")
        elif "waiting_for_profit_limit" in str(curr):
            await state.update_data(profit_limit=v); await state.set_state(None)
            await m.answer(f"✅ Профіт: {v:,}", reply_markup=get_main_kb(await state.get_data()))
        elif "calc_count" in str(curr):
            await state.update_data(c=v); await state.set_state(BotState.calc_buy)
            await m.answer("📥 Введи ціну КУПІВЛІ:")
        elif "calc_buy" in str(curr):
            await state.update_data(b=v); await state.set_state(BotState.calc_sell)
            await m.answer("📤 Введи ціну ПРОДАЖУ:")
        elif "calc_sell" in str(curr):
            d = await state.get_data(); await state.set_state(None)
            p_p, p_n = int((v*0.935)-d['b'])*d['c'], int((v*0.895)-d['b'])*d['c']
            await m.answer(f"📊 Результат ({d['c']} шт):\n👑 Пр: <b>{p_p:,}</b>\n💀 Пр: <b>{p_n:,}</b>", reply_markup=get_main_kb(d), parse_mode=ParseMode.HTML)
    except Exception: await m.answer("❌ Вводь тільки числа!")

@dp.message(F.text.regexp(r"⚡ 30хв:"), StateFilter('*'))
async def toggle_extra(m, state: FSMContext):
    d = await state.get_data(); val = not d.get("extra", False); await state.update_data(extra=val)
    await m.answer(f"⚡ Фільтр 30хв: {'УВІМКНЕНО' if val else 'ВИМКНЕНО'}", reply_markup=get_main_kb(await state.get_data()))

@dp.message(F.text.regexp(r"📊 Попит:"), StateFilter('*'))
async def toggle_liq(m, state: FSMContext):
    d = await state.get_data(); val = not d.get("check_liq", False); await state.update_data(check_liq=val)
    await m.answer(f"📊 Аналіз попиту: {'УВІМКНЕНО' if val else 'ВИМКНЕНО'}", reply_markup=get_main_kb(await state.get_data()))

@dp.callback_query(F.data.startswith("set_limit_"))
async def set_limit_cb(cb, state: FSMContext):
    t = cb.data.split("_")[2]; await state.set_state(BotState.waiting_for_buy_limit if t=="buy" else BotState.waiting_for_profit_limit); await cb.answer()
    await cb.message.answer(f"Введи {'бюджет' if t=='buy' else 'мін. профіт'}:", reply_markup=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="❌ Скасувати")]], resize_keyboard=True))

async def main():
    global http_session, is_shutting_down
    http_session = aiohttp.ClientSession(headers=HEADERS)
    await bot.delete_webhook(drop_pending_updates=True)
    asyncio.create_task(download_items())
    try: 
        await dp.start_polling(bot)
    finally:
        is_shutting_down = True
        if http_session: await http_session.close()
        await bot.session.close()

if __name__ == "__main__":
    try: asyncio.run(main())
    except KeyboardInterrupt: logger.info("Бот зупинений.")
    except Exception as e: logger.exception(e)
