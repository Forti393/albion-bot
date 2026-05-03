import os, json, aiohttp, asyncio, re, logging, time, statistics, html
from datetime import datetime, timezone
from typing import List, Optional
from aiogram import Bot, Dispatcher, types, F
from aiogram.enums import ParseMode, ChatAction
from aiogram.filters import Command, StateFilter
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

# ================= ЛОГУВАННЯ =================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ================= КОНФІГУРАЦІЯ =================
TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
if not TOKEN:
    logger.error("🚨 КРИТИЧНО: BOT_TOKEN відсутній!")
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
CITIES = ["Bridgewatch", "Martlock", "Lymhurst", "Thetford", "Fort Sterling", "Caerleon", "Brecilien", "Black Market"]
CITY_EMOJIS = {"Lymhurst":"🟢","Martlock":"🔵","Caerleon":"⚫","Thetford":"🟣","Bridgewatch":"🟠","Fort Sterling":"⚪","Brecilien":"🌸","Black Market":"💀"}
QUALITY_NAMES = {1:"Обычное", 2:"Хорошее", 3:"Выдающееся", 4:"Отличное", 5:"Шедевр"}
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

class BotState(StatesGroup):
    waiting_for_buy_limit = State()
    picking_from = State(); picking_to = State()
    calc_count = State(); calc_buy = State(); calc_sell = State()

# ================= СЛУЖБОВІ ФУНКЦІЇ =================
def fmt_t(s):
    try:
        dt = datetime.fromisoformat(s.split(".")[0].replace("Z","")).replace(tzinfo=timezone.utc)
        m = int((datetime.now(timezone.utc)-dt).total_seconds()/60)
        return f"{m}м" if m < 60 else f"{m//60}г"
    except: return "??"

async def get_item_liquidity(item_id, city):
    global http_session
    cache_key, now = f"{item_id}|{city}", datetime.now(timezone.utc)
    if cache_key in history_cache and (now - history_cache[cache_key]['time']).total_seconds() < 3600:
        return history_cache[cache_key]['data']
    
    url = f"https://europe.albion-online-data.com/api/v2/stats/history/{item_id}?locations={city}&time-series=24"
    async with scan_semaphore:
        try:
            async with http_session.get(url, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data and data[0].get('data'):
                        h_d = data[0]['data']
                        vol = h_d[-1].get('item_count', 0)
                        prices = [d['avg_price'] for d in h_d[-7:] if d['avg_price'] > 0]
                        avg = int(statistics.median(prices)) if prices else 0
                        res = {"vol": vol, "avg": avg}
                        history_cache[cache_key] = {'data': res, 'time': now}
                        return res
        except: pass
    return {"vol": 0, "avg": 0}

def passes_smart_filter(item_id, sell_price, liq_data):
    vol = liq_data.get('vol', 0)
    avg7 = liq_data.get('avg', 0)
    tier_match = re.search(r'T(\d)', item_id)
    tier = int(tier_match.group(1)) if tier_match else 4
    
    min_vol = 5 if tier <= 5 else 2
    if vol >= min_vol: return True, "active"
    if vol >= 1 and avg7 > 0 and sell_price <= (avg7 * 2.2): return True, "stable"
    return False, "risky"

async def download_items():
    global items_data, is_db_ready, http_session
    try:
        async with http_session.get("https://raw.githubusercontent.com/ao-data/ao-bin-dumps/master/formatted/items.json", timeout=60) as r:
            if r.status == 200:
                data = await r.json(content_type=None)
                allowed = ["weapon","armor","plate","leather","cloth","bag","cape","potion","meal","mount","tool","offhand"]
                items_data = {i["UniqueName"]: i for i in data if i.get("UniqueName","").startswith(("T4_","T5_","T6_","T7_","T8_")) and any(x in i.get("UniqueName","").lower() for x in allowed)}
                is_db_ready = True
    except: is_db_ready = True
async def scan_logic(d, f_c=None, t_c=None):
    global http_session
    if not items_data or is_shutting_down: return []
    pre_res = []; b_l = d.get("buy_limit", 0); p_l = d.get("profit_limit", 3000)
    ext, check_liq = d.get("extra", False), d.get("check_liq", False)
    i_list = list(items_data.keys()); cities = [f_c, t_c] if f_c and t_c else CITIES
    
    for i in range(0, len(i_list), 50):
        url = f"https://europe.albion-online-data.com/api/v2/stats/prices/{','.join(i_list[i:i+50])}?locations={','.join(cities)}"
        try:
            async with http_session.get(url, timeout=20) as resp:
                if resp.status == 200: data = await resp.json()
                else: continue
        except: continue

        now = datetime.now(timezone.utc)
        grouped = {}
        for e in data:
            k = f"{e['item_id']}|{e['quality']}"; grouped.setdefault(k, {})[e['city']] = e
            
        for k, c_d in grouped.items():
            i_id, q = k.split("|")
            srcs = [f_c] if f_c else [c for c in c_d if c!="Black Market"]
            for sc in srcs:
                if sc not in c_d: continue
                buy = c_d[sc].get('sell_price_min', 0)
                if buy <= 500 or buy > b_l: continue
                targets = [t_c] if t_c else [c for c in c_d if c!=sc]
                for tc in targets:
                    if tc not in c_d: continue
                    sell = c_d[tc].get('buy_price_max' if tc=="Black Market" else 'sell_price_min', 0)
                    p_n = int(sell*0.895 - buy)
                    
                    if p_n < 3000: continue # Фільтр "сміття"
                    
                    bd_str, sd_str = c_d[sc]['sell_price_min_date'], c_d[tc]['sell_price_min_date']
                    b_dt = datetime.fromisoformat(bd_str.split(".")[0].replace("Z","")).replace(tzinfo=timezone.utc)
                    s_dt = datetime.fromisoformat(sd_str.split(".")[0].replace("Z","")).replace(tzinfo=timezone.utc)
                    if (now-b_dt).total_seconds()/60 > 180 or (now-s_dt).total_seconds()/60 > 180: continue
                    if ext and ((now-b_dt).total_seconds()/60 > 30 or (now-s_dt).total_seconds()/60 > 30): continue

                    pre_res.append({'id':i_id,'q':int(q),'from':sc,'to':tc,'buy':buy,'sell':sell,
                                    'p_p':int(sell*0.935-buy),'p_n':p_n,'bd':bd_str,'sd':sd_str})

    if check_liq and pre_res:
        res = []
        for item in pre_res[:25]:
            liq = await get_item_liquidity(item['id'].split("@")[0], item['to'])
            passed, _ = passes_smart_filter(item['id'], item['sell'], liq)
            if passed:
                item['vol'], item['avg'] = liq['vol'], liq['avg']
                if liq['avg'] > 0 and item['sell'] > liq['avg'] * 2: item['is_risk'] = True
                res.append(item)
        return res
    return pre_res

async def disp_res(msg, res, d):
    res.sort(key=lambda x: x['p_n'], reverse=True)
    messages, full_text = [], ""
    for idx, r in enumerate(res[:15], 1):
        b_id, enc = r['id'].split("@")[0], r['id'].split("@")[1] if "@" in r['id'] else "0"
        name = items_data.get(b_id, {}).get("LocalizedNames", {}).get("RU-RU", b_id).upper()
        risk = "⚠️ [РИЗИК] " if r.get('is_risk') else ""
        liq = f"          📦 <b>{r.get('vol',0)} шт/д</b> | 📊 <b>{r.get('avg',0):,} мед</b>\n" if d.get("check_liq") else ""

        item_block = (
            f"{idx}) {risk}<b>{name}</b> [{b_id.split('_')[0][1:]}.{enc}]\n"
            f"✨ {QUALITY_NAMES.get(r['q'], 'Обычное')}\n"
            f"📥 {CITY_EMOJIS[r['from']]} {r['buy']:,} | 📤 {CITY_EMOJIS[r['to']]} {r['sell']:,}\n"
            f"<pre>"
            f"{liq}"
            f"          👑 <b>{r['p_p']:,}</b> | 💀 <b>{r['p_n']:,}</b>\n"
            f"          🕒 {fmt_t(r['bd'])} / {fmt_t(r['sd'])}"
            f"</pre>\n\n"
        )
        if len(full_text) + len(item_block) > 3900: messages.append(full_text); full_text = item_block
        else: full_text += item_block
    if not res: await msg.answer("📭 Порожньо.")
    else:
        if full_text: messages.append(full_text)
        for t in messages: await msg.answer(t, parse_mode=ParseMode.HTML)

def get_main_kb(d):
    m = d.get("mode")
    m_l = "🌍 Всі міста" if m == "all" else "📍 Шлях"
    e_l = f"⚡ 30хв: {'ON' if d.get('extra') else 'OFF'}"
    liq_l = f"📊 Попит: {'ON' if d.get('check_liq') else 'OFF'}"
    if not m: return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="❓ Допомога"), KeyboardButton(text="💰 Бюджет")], [KeyboardButton(text="🗺 Режим")]], resize_keyboard=True)
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="🚀 Сканер")], [KeyboardButton(text=m_l), KeyboardButton(text=e_l)], [KeyboardButton(text=liq_l), KeyboardButton(text="🧮 Кальк")], [KeyboardButton(text="💰 Бюджет"), KeyboardButton(text="🔄 Скинути")]], resize_keyboard=True)

@dp.message(F.text == "🚀 Сканер", StateFilter('*'))
async def main_search(m, state: FSMContext):
    d = await state.get_data()
    if not is_db_ready: return await m.answer("⏳ БД вантажиться...")
    if d.get("buy_limit", 0) <= 0: return await m.answer("⚠️ Встанови бюджет!")
    await bot.send_chat_action(m.chat.id, ChatAction.TYPING)
    res = await scan_logic(d, d.get('f_c'), d.get('t_c'))
    await disp_res(m, res, d)

@dp.message(F.text.regexp(r"⚡ 30хв:|📊 Попит:"), StateFilter('*'))
async def toggles(m, state: FSMContext):
    d = await state.get_data()
    if "30хв" in m.text: await state.update_data(extra=not d.get("extra", False))
    else: await state.update_data(check_liq=not d.get("check_liq", False))
    await m.answer("✅ Оновлено", reply_markup=get_main_kb(await state.get_data()))

@dp.message(F.text == "🧮 Кальк", StateFilter('*'))
async def calc_start(m, state: FSMContext):
    await state.set_state(BotState.calc_count); await m.answer("📦 Кількість:", reply_markup=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="❌ Скасувати")]], resize_keyboard=True))

@dp.message(StateFilter(BotState.waiting_for_buy_limit, BotState.calc_count, BotState.calc_buy, BotState.calc_sell))
async def numeric_handler(m, state: FSMContext):
    if m.text == "❌ Скасувати": await state.set_state(None); await m.answer("🚫", reply_markup=get_main_kb(await state.get_data())); return
    try:
        v = int(m.text.replace(" ","")); curr = await state.get_state()
        if "buy_limit" in str(curr): await state.update_data(buy_limit=v); await state.set_state(None); await m.answer(f"✅ Бюджет: {v:,}", reply_markup=get_main_kb(await state.get_data()))
        elif "calc_count" in str(curr): await state.update_data(c=v); await state.set_state(BotState.calc_buy); await m.answer("📥 Купівля:")
        elif "calc_buy" in str(curr): await state.update_data(b=v); await state.set_state(BotState.calc_sell); await m.answer("📤 Продаж:")
        elif "calc_sell" in str(curr):
            d = await state.get_data(); await state.set_state(None)
            p_n = int((v*0.895)-d['b'])*d['c']
            await m.answer(f"📊 Пр (чистий): <b>{p_n:,}</b>", reply_markup=get_main_kb(d), parse_mode=ParseMode.HTML)
    except: await m.answer("❌ Тільки числа")

@dp.message(F.text.contains("Допомога"), StateFilter('*')) # ФІКС: Реагує на кнопку з емодзі
@dp.message(Command("help"), StateFilter('*'))
async def cmd_help(m, state: FSMContext):
    await state.set_state(None)
    await m.answer("📖 <b>Допомога:</b>\n1. Налаштуй Бюджет.\n2. Тисни Сканер.\n⚠️ <b>РИЗИК</b> — ціна > медіани х2.\n📊 <b>Попит</b> — статистика за 7 днів.", parse_mode=ParseMode.HTML)

@dp.message(F.text == "💰 Бюджет", StateFilter('*'))
async def limit_menu(m, state: FSMContext):
    await m.answer("Налаштування:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💰 Встановити бюджет", callback_data="set_limit_buy")]]))

@dp.callback_query(F.data == "set_limit_buy")
async def set_limit_cb(cb, state: FSMContext):
    await state.set_state(BotState.waiting_for_buy_limit); await cb.answer(); await cb.message.answer("Введи бюджет:")

@dp.message(F.text == "🗺 Режим", StateFilter('*'))
async def choose_mode(m, state):
    await m.answer("Оберіть режим:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🎲 Всі міста", callback_data="set_mode_all")], [InlineKeyboardButton(text="📍 Шлях", callback_data="set_mode_custom")]]))

@dp.callback_query(F.data.startswith("set_mode_"))
async def set_mode_cb(cb, state: FSMContext):
    m = cb.data.split("_")[2]; await state.update_data(mode=m); await cb.answer()
    if m == "all": await cb.message.answer("🌍 Всі міста!", reply_markup=get_main_kb(await state.get_data()))
    else: await state.set_state(BotState.picking_from); await cb.message.answer("Звідки:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=f"{CITY_EMOJIS[c]} {c}", callback_data=f"city_{c}")] for c in CITIES if c!="Black Market"]))

@dp.callback_query(F.data.startswith("city_"))
async def city_pick(cb, state: FSMContext):
    await cb.answer(); curr = await state.get_state(); c = cb.data.split("_")[1]
    if curr and "picking_from" in str(curr):
        await state.update_data(f_c=c); await state.set_state(BotState.picking_to)
        await cb.message.edit_text(f"З: {c}. Куди:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=f"{CITY_EMOJIS[ci]} {ci}", callback_data=f"city_{ci}")] for ci in CITIES if ci!=c and ci!="Black Market"]))
    elif curr and "picking_to" in str(curr):
        await state.update_data(t_c=c, mode="custom"); await state.set_state(None)
        await cb.message.answer("✅ Готово!", reply_markup=get_main_kb(await state.get_data()))

@dp.message(F.text == "🔄 Скинути", StateFilter('*'))
async def btn_res(m, state: FSMContext):
    await state.clear(); await m.answer("🔄 Скинуто!", reply_markup=get_main_kb({}))

@dp.message(Command("start"), StateFilter('*'))
async def cmd_start(m, state: FSMContext):
    await state.clear(); await m.answer("👋 Бот готовий!", reply_markup=get_main_kb({}))

async def main():
    global http_session; http_session = aiohttp.ClientSession(headers=HEADERS)
    asyncio.create_task(download_items()); await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
