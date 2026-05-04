import os, json, aiohttp, asyncio, re, logging, time, signal, random, html
from datetime import datetime, timezone
from typing import List, Optional
from aiogram import Bot, Dispatcher, types, F
from aiogram.enums import ParseMode, ChatAction
from aiogram.filters import Command, StateFilter
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.exceptions import TelegramUnauthorizedError

# ================= ЛОГУВАННЯ =================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ================= КОНФІГУРАЦІЯ =================
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0")) 
TOKEN = os.environ.get("BOT_TOKEN", "").strip() # Очищаємо від зайвих пробілів

if not TOKEN:
    logger.error("🚨 BOT_TOKEN відсутній у змінних середовища!")
    exit(1)

bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# Глобальні змінні
items_data = {}; is_db_ready = False
http_session: Optional[aiohttp.ClientSession] = None 
scan_semaphore = asyncio.Semaphore(3) # Зменшив для стабільності
history_cache = {}
is_shutting_down = False

# Константи
CACHE_TTL = 3600 
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

async def get_item_liquidity(item_id, city, quality):
    global http_session, history_cache
    if not http_session or http_session.closed or is_shutting_down: return 0, 0
    
    cache_key, now = f"{item_id}|{city}|{quality}", datetime.now(timezone.utc)
    if cache_key in history_cache and (now - history_cache[cache_key]['time']).total_seconds() < CACHE_TTL:
        return history_cache[cache_key]['volume'], history_cache[cache_key]['avg_p']
        
    url = f"https://europe.albion-online-data.com/api/v2/stats/history/{item_id}?locations={city}&time-series=1&qualities={quality}"
    async with scan_semaphore:
        try:
            async with http_session.get(url, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    # Перевірка структури відповіді
                    if data and isinstance(data, list) and len(data) > 0:
                        history = data[0].get('data', [])
                        if history:
                            # Беремо останні 3 дні для середнього значення, щоб уникнути нулів
                            recent_days = history[-3:]
                            total_vol = sum(d.get('item_count', 0) for d in recent_days)
                            prices = [d.get('average_price', 0) for d in recent_days if d.get('average_price', 0) > 0]
                            
                            avg_vol = round(total_vol / len(recent_days), 1)
                            avg_price = int(sum(prices) / len(prices)) if prices else 0
                            
                            history_cache[cache_key] = {'volume': avg_vol, 'avg_p': avg_price, 'time': now}
                            return avg_vol, avg_price
        except Exception as e:
            logger.error(f"Помилка ліквідності для {item_id}: {e}")
    
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
                logger.info(f"✅ БД завантажена: {len(items_data)} предметів")
    except Exception as e: 
        logger.error(f"Помилка завантаження БД: {e}")
        is_db_ready = False

async def scan_logic(d, f_c=None, t_c=None):
    global http_session
    if not items_data or not http_session or is_shutting_down: return []
    pre_res = []; b_l = d.get("buy_limit", 0); p_l = d.get("profit_limit", 4000)
    ext = d.get("extra", False); check_liq_limit = d.get("check_liq", False) 
    
    i_list = list(items_data.keys()); cities = [f_c, t_c] if f_c and t_c else CITIES
    for i in range(0, len(i_list), 50):
        if is_shutting_down: break
        chunk = i_list[i:i+50]
        url = f"https://europe.albion-online-data.com/api/v2/stats/prices/{','.join(chunk)}?locations={','.join(cities)}"
        
        data = None
        try:
            async with http_session.get(url, timeout=20) as resp:
                if resp.status == 200: data = await resp.json()
        except: continue
        
        if not data: continue
        
        now = datetime.now(timezone.utc)
        grouped = {}
        for e in data:
            k = f"{e['item_id']}|{e['quality']}"; grouped.setdefault(k, {})[e['city']] = e
            
        for k, c_d in grouped.items():
            i_id, q = k.split("|")
            srcs = [f_c] if f_c else [c for c in c_d if c != "Black Market"]
            
            for sc in srcs:
                if sc not in c_d: continue
                buy = c_d[sc].get('sell_price_min', 0)
                if buy <= 500 or buy > b_l: continue
                
                bd_str = c_d[sc]['sell_price_min_date']
                try: b_dt = datetime.fromisoformat(bd_str.split(".")[0].replace("Z","")).replace(tzinfo=timezone.utc)
                except: continue
                if (now-b_dt).total_seconds()/60 > 180: continue
                
                targets = [t_c] if t_c else [c for c in c_d if c != sc]
                for tc in targets:
                    if tc not in c_d: continue
                    sk = 'buy_price_max_date' if tc == "Black Market" else 'sell_price_min_date'
                    sell = c_d[tc].get('buy_price_max' if tc == "Black Market" else 'sell_price_min', 0)
                    
                    if sell <= buy or (sell/buy) > 8: continue
                    sd_str = c_d[tc].get(sk)
                    try: s_dt = datetime.fromisoformat(sd_str.split(".")[0].replace("Z","")).replace(tzinfo=timezone.utc)
                    except: continue
                    if (now-s_dt).total_seconds()/60 > 180: continue
                    
                    p_n = int(sell*0.895-buy)
                    if p_n >= p_l:
                        if ext and ((now-b_dt).total_seconds()/60 > 30 or (now-s_dt).total_seconds()/60 > 30): continue
                        pre_res.append({'id':i_id,'q':int(q),'from':sc,'to':tc,'buy':buy,'sell':sell,
                                        'p_p':int(sell*0.935-buy),'p_n':p_n,'bd':bd_str,'sd':sd_str})

    # Фільтрація дублікатів та сортування
    pre_res.sort(key=lambda x: x['p_n'], reverse=True)
    res_final = []
    
    for item in pre_res[:30]: 
        vol, avg_p = await get_item_liquidity(item['id'], item['to'], item['q'])
        item['vol'] = vol
        item['avg_p'] = avg_p
        
        if check_liq_limit and vol < 1.0: continue # Якщо навіть 1 шт в день не продається - скіпаємо
            
        res_final.append(item)
        if len(res_final) >= 15: break

    return res_final

async def disp_res(msg, res, d):
    messages, full_text = [], ""
    for idx, r in enumerate(res, 1):
        id_parts = r['id'].split("@")
        b_id, enc = id_parts[0], (id_parts[1] if len(id_parts) > 1 else "0")
        icon = get_item_icon(b_id)
        tier = b_id.split('_')[0][1:]
        
        name = items_data.get(b_id, {}).get("LocalizedNames", {}).get("RU-RU", b_id)
        name = re.sub(r'\s*\([^)]*\)', '', html.escape(name.upper()))
        for t in TRASH: name = name.replace(t, "")
        tbd, tsd = fmt_t(r.get('bd')), fmt_t(r.get('sd'))
        
        liq_val = r.get('vol', 0)
        avg_val = r.get('avg_p', 0)
        
        item_block = (
            f"{idx}) {icon} <b>{name}</b> [{tier}.{enc}]\n"
            f"✨ {QUALITY_NAMES.get(r['q'], 'Обычное')}\n"
            f"📥 {CITY_EMOJIS[r['from']]} {r['buy']:,} | 🕒 {tbd}\n"
            f"📤 {CITY_EMOJIS[r['to']]} {r['sell']:,} | 🕒 {tsd}\n"
            f"<pre>"
            f"Прибуток:\n"
            f"👑 {r['p_p']:,}\n"
            f"💀 {r['p_n']:,}\n"
            f"📈 Попит: {liq_val} шт/д\n"
            f"📊 Сер. ціна: {avg_val:,}"
            f"</pre>\n"
            f"───────────────────\n\n"
        )
        if len(full_text) + len(item_block) > 3900: 
            messages.append(full_text); full_text = item_block
        else: full_text += item_block
    if full_text: messages.append(full_text)
    for t in messages: await msg.answer(t, parse_mode=ParseMode.HTML)

def get_main_kb(d):
    mode, budget, searched = d.get("mode"), d.get("buy_limit", 0), d.get("has_searched", False)
    m_btn = "🗺 Режим" if not mode else ("🌍 Всі міста" if mode == "all" else "📍 Шлях")
    kb = []
    if budget > 0 and mode: kb.append([KeyboardButton(text="🚀 Запустити сканер")])
    if searched:
        kb.append([KeyboardButton(text=f"🗺 Режим"), KeyboardButton(text=f"⚡ 30хв: {'ON' if d.get('extra') else 'OFF'}")])
        kb.append([KeyboardButton(text=f"📊 Попит: {'ON' if d.get('check_liq') else 'OFF'}"), KeyboardButton(text="🧮 Калькулятор")])
        kb.append([KeyboardButton(text="💰 Бюджет"), KeyboardButton(text="🔄 Скинути")])
    else:
        kb.append([KeyboardButton(text="💰 Бюджет"), KeyboardButton(text="🗺 Режим")])
        kb.append([KeyboardButton(text="🧮 Калькулятор"), KeyboardButton(text="❓ Допомога")])
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

@dp.message(Command("start"), StateFilter('*'))
async def cmd_start(m, state: FSMContext):
    await state.clear()
    await m.answer("👋 <b>Вітаю в Albion Trade Bot!</b>\n1. Встанови <b>💰 Бюджет</b>\n2. Обери <b>🗺 Режим</b>", parse_mode=ParseMode.HTML, reply_markup=get_main_kb({}))

@dp.message(F.text == "🚀 Запустити сканер", StateFilter('*'))
async def main_search(m, state: FSMContext):
    d = await state.get_data()
    if not is_db_ready: return await m.answer("⏳ БД вантажиться...")
    await bot.send_chat_action(m.chat.id, ChatAction.TYPING)
    s_msg = await m.answer("🔍 Аналізую ринок Європи..."); res = await scan_logic(d, d.get('f_c'), d.get('t_c'))
    await safe_delete(s_msg)
    if not d.get("has_searched"): await state.update_data(has_searched=True); d['has_searched'] = True
    if not res: await m.answer("📭 Нічого не знайдено. Спробуй збільшити бюджет.")
    else: await disp_res(m, res, d)
    await m.answer("✅ Готово!", reply_markup=get_main_kb(d))

@dp.message(F.text == "🗺 Режим", StateFilter('*'))
async def choose_mode(m, state: FSMContext):
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🎲 Всі міста", callback_data="set_mode_all")],[InlineKeyboardButton(text="📍 Шлях", callback_data="set_mode_custom")]])
    await m.answer("Оберіть режим пошуку:", reply_markup=kb)

@dp.callback_query(F.data.startswith("set_mode_"))
async def set_mode_cb(cb, state: FSMContext):
    mode = cb.data.split("_")[2]
    if mode == "all":
        await state.update_data(mode="all", f_c=None, t_c=None)
        await cb.message.edit_text("🌍 Режим 'Всі міста' активовано!")
        await cb.message.answer("Керування:", reply_markup=get_main_kb(await state.get_data()))
    else:
        await state.set_state(BotState.picking_from)
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=f"{CITY_EMOJIS[c]} {c}", callback_data=f"city_{c}")] for c in CITIES if c!="Black Market"])
        await cb.message.edit_text("Звідки купуємо:", reply_markup=kb)
    await cb.answer()

@dp.callback_query(F.data.startswith("city_"))
async def city_pick(cb, state: FSMContext):
    curr = await state.get_state(); c = cb.data.split("_")[1]
    if curr == BotState.picking_from.state:
        await state.update_data(f_c=c); await state.set_state(BotState.picking_to)
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=f"{CITY_EMOJIS[ci]} {ci}", callback_data=f"city_{ci}")] for ci in CITIES if ci!=c])
        await cb.message.edit_text(f"З міста {c}. Куди веземо?", reply_markup=kb)
    elif curr == BotState.picking_to.state:
        await state.update_data(t_c=c, mode="custom"); await state.set_state(None)
        d = await state.get_data()
        await cb.message.edit_text(f"📍 Маршрут {d['f_c']} ➡️ {c} встановлено!")
        await cb.message.answer("Оберіть дію:", reply_markup=get_main_kb(d))
    await cb.answer()

@dp.message(F.text == "💰 Бюджет", StateFilter('*'))
async def limit_menu(m, state: FSMContext):
    d = await state.get_data(); b, p = d.get("buy_limit", 0), d.get("profit_limit", 4000)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💰 Бюджет", callback_data="set_limit_buy"), InlineKeyboardButton(text="📈 Профіт", callback_data="set_limit_profit")]])
    await m.answer(f"⚙️ <b>Налаштування:</b>\n💰 Макс. ціна закупа: {b:,}\n📈 Мін. чистий прибуток: {p:,}", parse_mode=ParseMode.HTML, reply_markup=kb)

@dp.callback_query(F.data.startswith("set_limit_"))
async def set_limit_cb(cb, state: FSMContext):
    t = cb.data.split("_")[2]; await state.set_state(BotState.waiting_for_buy_limit if t=="buy" else BotState.waiting_for_profit_limit)
    await cb.message.answer(f"Введіть число для {'Бюджету' if t=='buy' else 'Профіту'}:", reply_markup=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="❌ Скасувати")]], resize_keyboard=True))
    await cb.answer()

@dp.message(F.text.regexp(r"⚡ 30хв:|📊 Попит:"), StateFilter('*'))
async def toggle_filters(m, state: FSMContext):
    d = await state.get_data()
    if "30хв" in m.text:
        val = not d.get("extra", False); await state.update_data(extra=val)
    else:
        val = not d.get("check_liq", False); await state.update_data(check_liq=val)
    await m.answer("Налаштування оновлено!", reply_markup=get_main_kb(await state.get_data()))

@dp.message(F.text == "🔄 Скинути", StateFilter('*'))
async def reset_confirm(m, state: FSMContext):
    await state.clear(); await m.answer("🔄 Всі дані скинуто!", reply_markup=get_main_kb({}))

@dp.message(StateFilter(BotState.waiting_for_buy_limit, BotState.waiting_for_profit_limit, BotState.calc_count, BotState.calc_buy, BotState.calc_sell))
async def numeric_handler(m, state: FSMContext):
    if m.text == "❌ Скасувати":
        await state.set_state(None); return await m.answer("Скасовано", reply_markup=get_main_kb(await state.get_data()))
    try:
        v = int(m.text.replace(" ","")); curr = await state.get_state()
        if "waiting_for_buy_limit" in str(curr):
            await state.update_data(buy_limit=v); await state.set_state(None)
            await m.answer(f"✅ Бюджет: {v:,}", reply_markup=get_main_kb(await state.get_data()))
        elif "waiting_for_profit_limit" in str(curr):
            await state.update_data(profit_limit=v); await state.set_state(None)
            await m.answer(f"✅ Профіт: {v:,}", reply_markup=get_main_kb(await state.get_data()))
    except: await m.answer("Введіть тільки цифри!")

async def main():
    global http_session
    try:
        http_session = aiohttp.ClientSession(headers=HEADERS)
        await bot.delete_webhook(drop_pending_updates=True)
        asyncio.create_task(download_items())
        logger.info("🚀 Бот запускається...")
        await dp.start_polling(bot)
    except TelegramUnauthorizedError:
        logger.error("❌ ПОМИЛКА: Невірний токен! Перевірте BOT_TOKEN.")
    except Exception as e:
        logger.error(f"💥 Критична помилка: {e}")
    finally:
        if http_session: await http_session.close()
        await bot.session.close()

if __name__ == "__main__":
    try: asyncio.run(main())
    except KeyboardInterrupt: pass
