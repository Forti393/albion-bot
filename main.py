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

# ================= ЛОГУВАННЯ =================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ================= НАЛАШТУВАННЯ ТА ЗАМКИ =================
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0")) 
bot = Bot(token=os.environ.get("BOT_TOKEN"))
dp = Dispatcher(storage=MemoryStorage())

items_data = {}; is_db_ready = False
http_session: Optional[aiohttp.ClientSession] = None 
scan_semaphore = asyncio.Semaphore(5) 
active_scans_lock = asyncio.Lock() 
user_cooldowns = {}; active_scans = set(); history_cache = {}
is_shutting_down = False

CITIES = ["Bridgewatch", "Martlock", "Lymhurst", "Thetford", "Fort Sterling", "Caerleon", "Brecilien", "Black Market"]
CITY_EMOJIS = {"Lymhurst":"🟢","Martlock":"🔵","Caerleon":"⚫","Thetford":"🟣","Bridgewatch":"🟠","Fort Sterling":"⚪","Brecilien":"🌸","Black Market":"💀"}
QUALITY_NAMES = {1:"Обычное", 2:"Хорошее", 3:"Выдающееся", 4:"Отличное", 5:"Шедевр"}
TRASH = ["Знаток ","Мастер ","Великий мастер ","Старейшина ","Ученик ","Новичок "]
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

class BotState(StatesGroup):
    waiting_for_buy_limit = State(); waiting_for_profit_limit = State()
    picking_from = State(); picking_to = State()
    calc_count = State(); calc_buy = State(); calc_sell = State()

# ================= ДОПОМІЖНІ ФУНКЦІЇ =================
async def safe_delete(msg):
    try: await msg.delete()
    except: pass

async def cleanup_cooldowns():
    while True:
        await asyncio.sleep(600)
        try:
            now = datetime.now(timezone.utc)
            expired = [uid for uid, dt in user_cooldowns.items() if (now - dt).total_seconds() > 3600]
            for uid in expired: del user_cooldowns[uid]
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
        if not s or s.startswith("0001"): return "??"
        dt = datetime.fromisoformat(s.split(".")[0].replace("Z","")).replace(tzinfo=timezone.utc)
        m = int((datetime.now(timezone.utc)-dt).total_seconds()/60)
        return f"{m}м" if m < 60 else f"{m//60}г"
    except: return "??"

async def get_item_liquidity(item_id, city):
    global http_session
    cache_key, now = f"{item_id}|{city}", datetime.now(timezone.utc)
    if cache_key in history_cache and (now - history_cache[cache_key]['time']).total_seconds() < 3600:
        return history_cache[cache_key]['volume']
    
    url = f"https://europe.albion-online-data.com/api/v2/stats/history/{item_id}?locations={city}&time-series=1"
    async with scan_semaphore:
        try:
            async with http_session.get(url, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data and data[0].get('data'):
                        vol = data[0]['data'][-1].get('item_count', 0)
                        history_cache[cache_key] = {'volume': vol, 'time': now}
                        return vol
        except: pass
    return 0

async def download_items():
    global items_data, is_db_ready, http_session
    try:
        async with http_session.get("https://raw.githubusercontent.com/ao-data/ao-bin-dumps/master/formatted/items.json", timeout=60) as r:
            if r.status == 200:
                data = await r.json(content_type=None)
                allowed = ["weapon","armor","plate","leather","cloth","bag","cape","potion","meal","mount","tool","offhand"]
                items_data = {i["UniqueName"]: i for i in data if i.get("UniqueName","").startswith(("T4_","T5_","T6_","T7_","T8_")) and any(x in i.get("UniqueName","").lower() for x in allowed)}
                logger.info(f"✅ БД завантажена: {len(items_data)} шт.")
    except Exception: logger.exception("Помилка БД:")
    finally: is_db_ready = True
def get_main_kb(d):
    m = d.get("mode")
    m_l = "🌍 Всі міста" if m == "all" else "📍 Шлях"
    e_l = "⚡ 30хв: ON" if d.get("extra") else "⚡ 30хв: OFF"
    liq_l = "📊 Попит: ON" if d.get("check_liq") else "📊 Попит: OFF"
    if not m: return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="❓ Допомога"), KeyboardButton(text="💰 Бюджет")], [KeyboardButton(text="🗺 Режим")]], resize_keyboard=True)
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="🚀 Запустити сканер")], [KeyboardButton(text=m_l), KeyboardButton(text=e_l)], [KeyboardButton(text=liq_l), KeyboardButton(text="🧮 Калькулятор")], [KeyboardButton(text="💰 Бюджет"), KeyboardButton(text="🔄 Скинути")]], resize_keyboard=True)

async def scan_logic(d, f_c=None, t_c=None):
    global http_session
    if not items_data or not http_session: return []
    res = []; b_l = d.get("buy_limit", 0); p_l = d.get("profit_limit", 4000)
    ext, check_liq = d.get("extra", False), d.get("check_liq", False)
    i_list = list(items_data.keys()); cities = [f_c, t_c] if f_c and t_c else CITIES
    
    for i in range(0, len(i_list), 50):
        url = f"https://europe.albion-online-data.com/api/v2/stats/prices/{','.join(i_list[i:i+50])}?locations={','.join(cities)}"
        try:
            async with http_session.get(url, timeout=20) as resp:
                if resp.status != 200: continue
                data = await resp.json()
        except: continue
        now = datetime.now(timezone.utc)
        grouped = {}
        for e in data:
            k = f"{e['item_id']}|{e['quality']}"; grouped.setdefault(k, {})[e['city']] = e
        for k, c_d in grouped.items():
            i_id, q = k.split("|"); srcs = [f_c] if f_c else [c for c in c_d if c!="Black Market"]
            for sc in srcs:
                if sc not in c_d: continue
                buy = c_d[sc].get('sell_price_min', 0)
                if buy <= 500 or buy > b_l: continue
                bd_str = c_d[sc]['sell_price_min_date']
                b_dt = datetime.fromisoformat(bd_str.split(".")[0].replace("Z","")).replace(tzinfo=timezone.utc)
                if (now-b_dt).total_seconds()/60 > 180: continue
                targets = [t_c] if t_c else [c for c in c_d if c!=sc]
                for tc in targets:
                    if tc not in c_d: continue
                    sk = 'buy_price_max_date' if tc=="Black Market" else 'sell_price_min_date'
                    sell = c_d[tc].get('buy_price_max' if tc=="Black Market" else 'sell_price_min', 0)
                    if sell <= buy or (sell/buy) > 10: continue
                    sd_str = c_d[tc].get(sk)
                    s_dt = datetime.fromisoformat(sd_str.split(".")[0].replace("Z","")).replace(tzinfo=timezone.utc)
                    if (now-s_dt).total_seconds()/60 > 180: continue
                    p_n = int(sell*0.895-buy)
                    if p_n >= p_l:
                        if ext and ((now-b_dt).total_seconds()/60 > 30 or (now-s_dt).total_seconds()/60 > 30): continue
                        item_data = {'id':i_id,'q':int(q),'from':sc,'to':tc,'buy':buy,'sell':sell,'p_p':int(sell*0.935-buy),'p_n':p_n,'bd':bd_str,'sd':sd_str}
                        
                        # Кнопка попит тепер просто додає цифру, без фільтрів
                        if check_liq:
                            item_data['vol'] = await get_item_liquidity(i_id.split("@")[0], tc)
                        res.append(item_data)
        if i % 300 == 0: await asyncio.sleep(0.1)
    return res

async def disp_res(msg, res, d):
    res.sort(key=lambda x: x['p_n'], reverse=True)
    messages, full_text = [], ""
    for idx, r in enumerate(res[:15], 1):
        b_id = r['id'].split("@")[0]; name = items_data.get(b_id, {}).get("LocalizedNames", {}).get("RU-RU", b_id).upper()
        for t in TRASH: name = name.replace(t, "")
        icon = get_item_icon(b_id); tbd, tsd = fmt_t(r['bd']), fmt_t(r['sd'])
        
        liq_line = f"📊 Попит: <b>{r.get('vol',0)} шт/д</b>\n" if d.get('check_liq') else ""

        item_block = (
            f"{idx}) {icon} <b>{name}</b> [{b_id.split('_')[0][1:]}.{r['id'].split('@')[1] if '@' in r['id'] else '0'}]\n"
            f"✨ {QUALITY_NAMES.get(r['q'], 'Обычное')}\n"
            f"📥 {CITY_EMOJIS[r['from']]} {r['buy']:,} (⏳ {tbd})\n"
            f"📤 {CITY_EMOJIS[r['to']]} {r['sell']:,} (⏳ {tsd})\n"
            f"{liq_part}"
            f"<pre>"
            f"          👑 Прибуток: <b>{r['p_p']:,}</b>\n"
            f"          💀 Прибуток: <b>{r['p_n']:,}</b>"
            f"</pre>\n\n"
        ).replace("{liq_part}", liq_line)
        
        if len(full_text) + len(item_block) > 3900: messages.append(full_text); full_text = item_block
        else: full_text += item_block
    if not res: await msg.answer("📭 Порожньо.")
    else:
        if full_text: messages.append(full_text)
        for t in messages: await msg.answer(t, parse_mode=ParseMode.HTML)

@dp.message(F.text == "🚀 Запустити сканер", StateFilter('*'))
async def main_search(m, state: FSMContext):
    u_id = m.from_user.id; d = await state.get_data(); now = datetime.now(timezone.utc)
    if not is_db_ready: return await m.answer("⏳ БД вантажиться...")
    if d.get("buy_limit", 0) <= 0: return await m.answer("⚠️ Встанови бюджет!")
    async with active_scans_lock:
        if u_id in active_scans: return await m.answer("⚠️ Запит обробляється!")
        if u_id != ADMIN_ID and u_id in user_cooldowns and (now - user_cooldowns[u_id]).total_seconds() < 25:
            return await m.answer(f"⏳ Зачекай {int(25-(now-user_cooldowns[u_id]).total_seconds())}с.")
        active_scans.add(u_id); user_cooldowns[u_id] = now
    try:
        s_msg = await m.answer("🔍 Сканую...", reply_markup=ReplyKeyboardRemove())
        res = await scan_logic(d, d.get('f_c'), d.get('t_c'))
        await safe_delete(s_msg); await disp_res(m, res, d)
        await m.answer(f"✅ Готово!", reply_markup=get_main_kb(d))
    finally: active_scans.discard(u_id)

@dp.message(F.text.regexp(r"⚡ 30хв:|📊 Попит:"), StateFilter('*'))
async def toggles(m, state: FSMContext):
    d = await state.get_data()
    if "30хв" in m.text: await state.update_data(extra=not d.get("extra", False))
    else: await state.update_data(check_liq=not d.get("check_liq", False))
    await m.answer("✅ Оновлено", reply_markup=get_main_kb(await state.get_data()))

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

@dp.message(F.text == "💰 Бюджет", StateFilter('*'))
async def limit_menu(m, state: FSMContext):
    await m.answer("Налаштування:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💰 Встановити бюджет", callback_data="set_limit_buy")]]))

@dp.callback_query(F.data == "set_limit_buy")
async def set_limit_cb(cb, state: FSMContext):
    await state.set_state(BotState.waiting_for_buy_limit); await cb.answer(); await cb.message.answer("Введи бюджет:")

@dp.message(F.text.contains("Допомога"), StateFilter('*'))
@dp.message(Command("help"), StateFilter('*'))
async def cmd_help(m, state: FSMContext):
    await m.answer("📖 <b>Допомога:</b>\n1. Бюджет.\n2. Режим.\n3. Сканер.\n⚡ 30хв — свіжі ціни.\n📊 Попит — скільки продано в місті призначення.", parse_mode=ParseMode.HTML)

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
    asyncio.create_task(download_items()); asyncio.create_task(cleanup_cooldowns())
    await bot.delete_webhook(drop_pending_updates=True); await dp.start_polling(bot)

if __name__ == "__main__": asyncio.run(main())
