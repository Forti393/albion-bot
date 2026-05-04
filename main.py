import os, asyncio, aiohttp, logging, re, html, random
from datetime import datetime, timezone
from typing import List, Optional, Dict
from aiogram import Bot, Dispatcher, types, F
from aiogram.enums import ParseMode, ChatAction
from aiogram.filters import Command, StateFilter
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

# ================= КОНФІГУРАЦІЯ =================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

TOKEN = os.environ.get("BOT_TOKEN")
if not TOKEN:
    exit("🚨 BOT_TOKEN відсутній!")

bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# Глобальні змінні
items_data = {}; is_db_ready = False
http_session: Optional[aiohttp.ClientSession] = None 
scan_semaphore = asyncio.Semaphore(5) 
history_cache = {} # {key: {'vol': int, 'avg': int, 'time': datetime}}
MAX_CACHE_SIZE = 2000
is_shutting_down = False

CITIES = ["Bridgewatch", "Martlock", "Lymhurst", "Thetford", "Fort Sterling", "Caerleon", "Brecilien", "Black Market"]
CITY_EMOJIS = {
    "Lymhurst":"🟢", "Martlock":"🔵", "Caerleon":"🔴", "Thetford":"🟣", 
    "Bridgewatch":"🟠", "Fort Sterling":"⚪", "Brecilien":"🌸", "Black Market":"⚫"
}
QUALITY_NAMES = {1:"Обычное", 2:"Хорошее", 3:"Выдающееся", 4:"Отличное", 5:"Шедевр"}
HEADERS = {"User-Agent": "AlbionTradeBot/2.0 (Compatible; AIO)"}

class BotState(StatesGroup):
    waiting_for_buy_limit = State(); waiting_for_profit_limit = State()
    picking_from = State(); picking_to = State()
    calc_count = State(); calc_buy = State(); calc_sell = State()
    confirm_reset = State()

# ================= СЛУЖБОВІ ФУНКЦІЇ =================
async def get_item_stats(item_id: str, city: str):
    """Отримує об'єм та середню ціну з історії"""
    global http_session
    if not http_session or is_shutting_down: return 0, 0
    
    cache_key = f"{item_id}|{city}"
    now = datetime.now(timezone.utc)
    
    if cache_key in history_cache:
        cached = history_cache[cache_key]
        if (now - cached['time']).total_seconds() < 3600:
            return cached['vol'], cached['avg']

    url = f"https://europe.albion-online-data.com/api/v2/stats/history/{item_id}?locations={city}&time-series=1"
    try:
        async with http_session.get(url, timeout=10) as resp:
            if resp.status == 200:
                data = await resp.json()
                if data and data[0].get('data'):
                    last_entry = data[0]['data'][-1]
                    vol = last_entry.get('item_count', 0)
                    avg = int(last_entry.get('avg_price', 0))
                    
                    if len(history_cache) > MAX_CACHE_SIZE:
                        history_cache.clear() # Просте очищення при переповненні
                    
                    history_cache[cache_key] = {'vol': vol, 'avg': avg, 'time': now}
                    return vol, avg
    except Exception as e:
        logger.debug(f"History error for {item_id}: {e}")
    return 0, 0

async def download_items():
    global items_data, is_db_ready, http_session
    url = "https://raw.githubusercontent.com/ao-data/ao-bin-dumps/master/formatted/items.json"
    for attempt in range(3):
        try:
            async with http_session.get(url, timeout=60) as r:
                if r.status == 200:
                    data = await r.json(content_type=None)
                    allowed = ["weapon","armor","plate","leather","cloth","bag","cape","potion","meal","mount","tool","offhand"]
                    items_data = {i["UniqueName"]: i for i in data if i.get("UniqueName","").startswith(("T4_","T5_","T6_","T7_","T8_")) and any(x in i.get("UniqueName","").lower() for x in allowed)}
                    logger.info(f"✅ БД завантажена: {len(items_data)} предметів.")
                    is_db_ready = True
                    return
        except Exception as e:
            logger.error(f"Attempt {attempt+1} failed to download DB: {e}")
            await asyncio.sleep(5)
    is_db_ready = False

async def scan_logic(d, f_c=None, t_c=None):
    global http_session
    if not items_data or not http_session or is_shutting_down: return []
    
    pre_res = []
    b_l, p_l = d.get("buy_limit", 0), d.get("profit_limit", 4000)
    ext, check_liq = d.get("extra", False), d.get("check_liq", False)
    
    i_list = list(items_data.keys())
    # Формуємо список міст для запиту
    fetch_cities = []
    if f_c: fetch_cities.append(f_c)
    if t_c: fetch_cities.append(t_c)
    if not fetch_cities: fetch_cities = CITIES

    for i in range(0, len(i_list), 50):
        if is_shutting_down: break
        chunk = i_list[i:i+50]
        url = f"https://europe.albion-online-data.com/api/v2/stats/prices/{','.join(chunk)}?locations={','.join(fetch_cities)}"
        
        try:
            async with http_session.get(url, timeout=20) as resp:
                if resp.status == 429:
                    await asyncio.sleep(5); continue
                if resp.status != 200: continue
                data = await resp.json()
        except Exception: continue

        now = datetime.now(timezone.utc)
        grouped = {}
        for e in data:
            k = f"{e['item_id']}|{e['quality']}"
            grouped.setdefault(k, {})[e['city']] = e

        for k, c_d in grouped.items():
            i_id, q = k.split("|")
            # "Звідки" не може бути Black Market
            srcs = [f_c] if f_c else [c for c in c_d if c != "Black Market"]
            
            for sc in srcs:
                if sc not in c_d: continue
                buy = c_d[sc].get('sell_price_min', 0)
                if buy <= 500 or buy > b_l: continue
                
                bd_str = c_d[sc]['sell_price_min_date']
                try:
                    b_dt = datetime.fromisoformat(bd_str.split(".")[0].replace("Z","")).replace(tzinfo=timezone.utc)
                    if (now-b_dt).total_seconds()/60 > 180: continue
                except: continue

                # "Куди" може бути будь-яке місто (включаючи BM)
                targets = [t_c] if t_c else [c for c in c_d if c != sc]
                for tc in targets:
                    if tc not in c_d: continue
                    is_bm = (tc == "Black Market")
                    sell = c_d[tc].get('buy_price_max' if is_bm else 'sell_price_min', 0)
                    if sell <= buy or (sell/buy) > 10: continue

                    sd_str = c_d[tc].get('buy_price_max_date' if is_bm else 'sell_price_min_date')
                    try:
                        s_dt = datetime.fromisoformat(sd_str.split(".")[0].replace("Z","")).replace(tzinfo=timezone.utc)
                        if (now-s_dt).total_seconds()/60 > 180: continue
                    except: continue

                    p_n = int(sell*0.895 - buy)
                    if p_n >= p_l:
                        if ext and ((now-b_dt).total_seconds()/60 > 30 or (now-s_dt).total_seconds()/60 > 30): continue
                        pre_res.append({'id':i_id,'q':int(q),'from':sc,'to':tc,'buy':buy,'sell':sell,
                                        'p_p':int(sell*0.935-buy),'p_n':p_n,'bd':bd_str,'sd':sd_str})

    if pre_res:
        pre_res.sort(key=lambda x: x['p_n'], reverse=True)
        final = []
        # Обробляємо топ результатів для отримання історії (якщо увімкнено попит або потрібна сер. ціна)
        for item in pre_res[:20]:
            vol, avg = await get_item_stats(item['id'].split("@")[0], item['to'])
            item['vol'] = vol
            item['avg'] = avg
            
            if check_liq:
                profit_margin = item['p_n'] / item['buy']
                if vol > 0 or profit_margin <= 0.15:
                    final.append(item)
            else:
                final.append(item)
        return final[:15]
    return []
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
        dt = datetime.fromisoformat(s.split(".")[0].replace("Z","")).replace(tzinfo=timezone.utc)
        m = int((datetime.now(timezone.utc)-dt).total_seconds()/60)
        return f"{m}м" if m < 60 else f"{m//60}г"
    except: return "??"

async def disp_res(msg, res, d):
    show_liq = d.get("check_liq")
    messages, full_text = [], ""
    
    for idx, r in enumerate(res, 1):
        b_id = r['id'].split("@")[0]
        enc = r['id'].split("@")[1] if "@" in r['id'] else "0"
        tier = b_id.split('_')[0][1:]
        name = items_data.get(b_id, {}).get("LocalizedNames", {}).get("RU-RU", b_id)
        name = re.sub(r'\s*\([^)]*\)', '', html.escape(name.upper()))
        
        tbd, tsd = fmt_t(r.get('bd')), fmt_t(r.get('sd'))
        p_p_str, p_n_str = f"{r['p_p']:,}", f"{r['p_n']:,}"
        
        liq_part = f"Попит: {r.get('vol', 0)} шт/д" if show_liq else ""
        avg_price = f"Сер. ціна: {r.get('avg', 0):,}"
        
        item_block = (
            f"{idx}) {get_item_icon(b_id)} <b>{name}</b> [{tier}.{enc}]\n"
            f"✨ {QUALITY_NAMES.get(r['q'], 'Обычное')}\n"
            f"📥 {CITY_EMOJIS[r['from']]} {r['buy']:,} | 🕒 {tbd}\n"
            f"📤 {CITY_EMOJIS[r['to']]} {r['sell']:,} | 🕒 {tsd}\n"
            f"<pre>"
            f"Прибуток:\n"
            f"👑 {p_p_str.ljust(12)} {liq_part}\n"
            f"💀 {p_n_str.ljust(12)} {avg_price}\n"
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
        kb.append([KeyboardButton(text="🧮 Калькулятор"), KeyboardButton(text="❓ Допомога")])
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

@dp.message(Command("start"), StateFilter('*'))
async def cmd_start(m, state: FSMContext):
    await state.clear()
    await m.answer("👋 <b>Вітаю!</b>\nНалаштуй <b>Бюджет</b> та <b>Режим</b> для початку.", parse_mode=ParseMode.HTML, reply_markup=get_main_kb({}))

@dp.message(Command("help"), StateFilter('*'))
@dp.message(F.text == "❓ Допомога", StateFilter('*'))
async def cmd_help(m, state: FSMContext):
    await m.answer("📖 <b>Допомога:</b>\n• Бюджет — макс. ціна покупки.\n• Шлях — пошук між містами.\n• Блек Маркет доступний тільки для продажу.", parse_mode=ParseMode.HTML)

@dp.message(F.text == "🚀 Запустити сканер", StateFilter('*'))
async def main_search(m, state: FSMContext):
    d = await state.get_data()
    if not is_db_ready: return await m.answer("⏳ БД ще вантажиться...")
    await bot.send_chat_action(m.chat.id, ChatAction.TYPING)
    s_msg = await m.answer("🔍 Сканую ринок..."); res = await scan_logic(d, d.get('f_c'), d.get('t_c'))
    await s_msg.delete()
    if not res: await m.answer("📭 Нічого не знайдено за вашим бюджетом.")
    else:
        await state.update_data(has_searched=True)
        await disp_res(m, res, d)
    await m.answer("✅ Сканування завершено.", reply_markup=get_main_kb(await state.get_data()))

@dp.message(F.text.startswith("Режим:") | (F.text == "🗺 Режим"), StateFilter('*'))
async def choose_mode(m, state):
    await m.answer("Оберіть режим пошуку:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎲 Всі міста", callback_data="set_mode_all")],
        [InlineKeyboardButton(text="📍 Шлях (Точково)", callback_data="set_mode_custom")]
    ]))

@dp.callback_query(F.data.startswith("set_mode_"))
async def set_mode_cb(cb, state: FSMContext):
    mode = cb.data.split("_")[2]
    if mode == "all":
        await state.update_data(mode="all", f_c=None, t_c=None)
        await cb.message.answer("🌍 Режим 'Всі міста' активовано!", reply_markup=get_main_kb(await state.get_data()))
    else:
        await state.set_state(BotState.picking_from)
        # При виборі "Звідки" ховаємо Black Market
        btns = [[InlineKeyboardButton(text=f"{CITY_EMOJIS[c]} {c}", callback_data=f"city_{c}")] for c in CITIES if c != "Black Market"]
        await cb.message.edit_text("Оберіть місто КУПІВЛІ:", reply_markup=InlineKeyboardMarkup(inline_keyboard=btns))
    await cb.answer()

@dp.callback_query(F.data.startswith("city_"))
async def city_pick(cb, state: FSMContext):
    curr = await state.get_state(); city = cb.data.split("_")[1]
    if "picking_from" in str(curr):
        await state.update_data(f_c=city); await state.set_state(BotState.picking_to)
        # При виборі "Куди" показуємо всі міста
        btns = [[InlineKeyboardButton(text=f"{CITY_EMOJIS[c]} {c}", callback_data=f"city_{c}")] for c in CITIES if c != city]
        await cb.message.edit_text(f"Купівля в: {city}\nТепер оберіть місто ПРОДАЖУ:", reply_markup=InlineKeyboardMarkup(inline_keyboard=btns))
    elif "picking_to" in str(curr):
        await state.update_data(t_c=city, mode="custom"); await state.set_state(None)
        await cb.message.answer(f"📍 Маршрут встановлено!", reply_markup=get_main_kb(await state.get_data()))
    await cb.answer()

@dp.message(F.text == "🧮 Калькулятор", StateFilter('*'))
async def calc_start(m, state: FSMContext):
    await state.set_state(BotState.calc_count)
    await m.answer("📦 Кількість предметів:", reply_markup=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="❌ Скасувати")]], resize_keyboard=True))

@dp.message(F.text == "🔄 Скинути", StateFilter('*'))
async def reset_req(m, state: FSMContext):
    await state.set_state(BotState.confirm_reset)
    await m.answer("⚠️ <b>Скинути всі налаштування?</b>", parse_mode=ParseMode.HTML, 
                   reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Так", callback_data="res_y"), InlineKeyboardButton(text="Ні", callback_data="res_n")]]))

@dp.callback_query(F.data.startswith("res_"), StateFilter(BotState.confirm_reset))
async def res_proc(cb, state: FSMContext):
    if "res_y" in cb.data:
        await state.clear(); await cb.message.edit_text("🔄 Дані скинуто.")
        await cb.message.answer("Почнемо спочатку?", reply_markup=get_main_kb({}))
    else: await cb.message.edit_text("Скасовано.")
    await state.set_state(None); await cb.answer()

@dp.message(StateFilter(BotState.waiting_for_buy_limit, BotState.waiting_for_profit_limit, BotState.calc_count, BotState.calc_buy, BotState.calc_sell))
async def numeric_handler(m, state: FSMContext):
    if m.text == "❌ Скасувати":
        await state.set_state(None); return await m.answer("Скасовано", reply_markup=get_main_kb(await state.get_data()))
    try:
        v = int(m.text.replace(" ","")); curr = await state.get_state()
        if "buy_limit" in str(curr):
            await state.update_data(buy_limit=v); await state.set_state(None)
            await m.answer(f"✅ Бюджет: {v:,}", reply_markup=get_main_kb(await state.get_data()))
        elif "calc_count" in str(curr):
            await state.update_data(c=v); await state.set_state(BotState.calc_buy); await m.answer("📥 Ціна КУПІВЛІ:")
        elif "calc_buy" in str(curr):
            await state.update_data(b=v); await state.set_state(BotState.calc_sell); await m.answer("📤 Ціна ПРОДАЖУ:")
        elif "calc_sell" in str(curr):
            d = await state.get_data(); await state.set_state(None)
            pp, pn = int((v*0.935)-d['b'])*d['c'], int((v*0.895)-d['b'])*d['c']
            await m.answer(f"📊 <b>Результат:</b>\nКорона: {pp:,}\nЧереп: {pn:,}", parse_mode=ParseMode.HTML, reply_markup=get_main_kb(d))
    except: await m.answer("🔢 Введіть ціле число.")

@dp.message(F.text.regexp(r"⚡ 30хв:|📊 Попит:"), StateFilter('*'))
async def toggles(m, state: FSMContext):
    d = await state.get_data()
    if "30хв" in m.text:
        val = not d.get("extra", False); await state.update_data(extra=val)
    else:
        val = not d.get("check_liq", False); await state.update_data(check_liq=val)
    await m.answer("Оновлено!", reply_markup=get_main_kb(await state.get_data()))

@dp.callback_query(F.data.startswith("set_limit_"))
async def set_limit_cb(cb, state: FSMContext):
    t = cb.data.split("_")[2]
    await state.set_state(BotState.waiting_for_buy_limit if t=="buy" else BotState.waiting_for_profit_limit)
    await cb.message.answer(f"Введіть {'бюджет' if t=='buy' else 'мін. прибуток'}:")
    await cb.answer()

async def main():
    global http_session
    http_session = aiohttp.ClientSession(headers=HEADERS)
    await bot.delete_webhook(drop_pending_updates=True)
    asyncio.create_task(download_items())
    try: await dp.start_polling(bot)
    finally:
        await http_session.close()
        await bot.close()

if __name__ == "__main__":
    asyncio.run(main())
