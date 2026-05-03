import os, json, aiohttp, asyncio, re, logging, time, statistics, html
from datetime import datetime, UTC, timedelta
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
http_session = None 
scan_semaphore = asyncio.Semaphore(3) 
active_scans_lock = asyncio.Lock() 
user_cooldowns = {}; active_scans = set() 
history_cache = {} # Кеш для попиту

CITIES = ["Bridgewatch", "Martlock", "Lymhurst", "Thetford", "Fort Sterling", "Caerleon", "Brecilien", "Black Market"]
CITY_EMOJIS = {"Lymhurst":"🟢","Martlock":"🔵","Caerleon":"⚫","Thetford":"🟣","Bridgewatch":"🟠","Fort Sterling":"⚪","Brecilien":"🌸","Black Market":"💀"}
QUALITY_NAMES = {1:"Обычное", 2:"Хорошее", 3:"Выдающееся", 4:"Отличное", 5:"Шедевр"}
TRASH = ["Знаток ","Мастер ","Великий мастер ","Старейшина ","Ученик ","Новичок "]
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

class BotState(StatesGroup):
    waiting_for_buy_limit = State(); waiting_for_profit_limit = State()
    picking_from = State(); picking_to = State()
    calc_count = State(); calc_buy = State(); calc_sell = State()

# ================= НОВІ АНАЛІТИЧНІ ФУНКЦІЇ =================
async def get_item_liquidity(item_id, city):
    global http_session
    cache_key, now = f"{item_id}|{city}", datetime.now(UTC)
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
    vol, avg7 = liq_data.get('vol', 0), liq_data.get('avg', 0)
    tier_match = re.search(r'T(\d)', item_id)
    tier = int(tier_match.group(1)) if tier_match else 4
    min_vol = 5 if tier <= 5 else 2
    
    if vol >= min_vol: return True
    if vol >= 1 and avg7 > 0 and sell_price <= (avg7 * 2.2): return True
    return False

# ================= ДОПОМІЖНІ ФУНКЦІЇ =================
async def cleanup_cooldowns():
    while True:
        await asyncio.sleep(600)
        try:
            now = datetime.now(UTC) 
            expired = [uid for uid, dt in user_cooldowns.items() if (now - dt).total_seconds() > 3600]
            for uid in expired: del user_cooldowns[uid]
        except Exception: logger.exception("Критична помилка cleanup_cooldowns:")

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
# ================= КЛАВІАТУРИ =================
def get_main_kb(d):
    m = d.get("mode")
    m_l = "🌍 Всі міста" if m == "all" else "📍 Шлях"
    e_l = "🚫 30хв: OFF" if d.get("extra") else "⚡ 30хв: ON"
    liq_l = "📊 Попит: ON" if d.get("check_liq") else "📊 Попит: OFF"
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="🚀 Запустити сканер")],
        [KeyboardButton(text=m_l), KeyboardButton(text=e_l)],
        [KeyboardButton(text=liq_l), KeyboardButton(text="🧮 Калькулятор")],
        [KeyboardButton(text="💰 Налаштувати бюджет"), KeyboardButton(text="❓ Допомога")],
        [KeyboardButton(text="🔄 Перезавантаження")]
    ], resize_keyboard=True)

async def scan_logic(d, f_c=None, t_c=None):
    global http_session
    if not items_data: return [] 
    res = []; b_l = d.get("buy_limit", 0); p_l = d.get("profit_limit", 3000); ext = d.get("extra", False); check_liq = d.get("check_liq", False)
    i_list = list(items_data.keys()); cities = [f_c, t_c] if f_c and t_c else CITIES
    
    for i in range(0, len(i_list), 50):
        url = f"https://europe.albion-online-data.com/api/v2/stats/prices/{','.join(i_list[i:i+50])}?locations={','.join(cities)}"
        data = None
        for attempt in range(3):
            try:
                async with http_session.get(url, timeout=20) as resp:
                    if resp.status == 200: data = await resp.json(); break
            except: await asyncio.sleep(1)
        if not data: continue
        
        now = datetime.now(UTC)
        grouped = {}
        for e in data:
            k = f"{e['item_id']}|{e['quality']}"; grouped.setdefault(k, {})[e['city']] = e
        for k, c_d in grouped.items():
            i_id, q = k.split("|"); srcs = [f_c] if f_c else [c for c in c_d if c!="Black Market"]
            for sc in srcs:
                if sc not in c_d: continue
                buy = c_d[sc].get('sell_price_min', 0)
                if buy <= 500 or buy > b_l: continue
                targets = [t_c] if t_c else [c for c in c_d if c!=sc]
                for tc in targets:
                    if tc not in c_d: continue
                    sell = c_d[tc].get('buy_price_max' if tc=="Black Market" else 'sell_price_min', 0)
                    p_n = int(sell*0.895-buy)
                    if p_n < p_l: continue
                    
                    bd_str, sd_str = c_d[sc]['sell_price_min_date'], c_d[tc].get('sell_price_min_date' if tc!="Black Market" else 'buy_price_max_date')
                    b_dt = datetime.fromisoformat(bd_str.split(".")[0].replace("Z","")).replace(tzinfo=UTC)
                    s_dt = datetime.fromisoformat(sd_str.split(".")[0].replace("Z","")).replace(tzinfo=UTC)
                    
                    if (now-b_dt).total_seconds()/60 > 180 or (now-s_dt).total_seconds()/60 > 180: continue
                    if ext and ((now-b_dt).total_seconds()/60 > 30 or (now-s_dt).total_seconds()/60 > 30): continue
                    
                    pre_res = {'id':i_id,'q':int(q),'from':sc,'to':tc,'buy':buy,'sell':sell,'p_p':int(sell*0.935-buy),'p_n':p_n,'bd':bd_str,'sd':sd_str}
                    
                    if check_liq:
                        liq = await get_item_liquidity(i_id, tc)
                        if passes_smart_filter(i_id, sell, liq):
                            pre_res.update({'vol': liq['vol'], 'avg': liq['avg']})
                            if liq['avg'] > 0 and sell > liq['avg'] * 2: pre_res['risk'] = True
                            res.append(pre_res)
                    else:
                        res.append(pre_res)
        if i % 300 == 0: await asyncio.sleep(0.2)
    return res

async def disp_res(msg, res, check_liq):
    res.sort(key=lambda x: x['p_n'], reverse=True)
    full_text = ""; messages = []
    for idx, r in enumerate(res[:15], 1):
        b_id = r['id'].split("@")[0]; tier = b_id.split('_')[0][1:]
        name = items_data.get(b_id, {}).get("LocalizedNames", {}).get("RU-RU", b_id)
        for t in TRASH: name = name.replace(t, "")
        risk_mark = "⚠️ [РИЗИК] " if r.get('risk') else ""
        liq_txt = f"          📦 <b>{r.get('vol',0)} шт/д</b> | 📊 <b>{r.get('avg',0):,} мед</b>\n" if check_liq else ""
        
        item_text = (f"{idx}) {risk_mark}{get_item_icon(b_id)} <b>{name.upper()} [{tier}.{r['id'].split('@')[1] if '@' in r['id'] else '0'}]</b>\n"
                     f"✨ Качество: <b>{QUALITY_NAMES.get(r['q'], 'Обычное')}</b>\n"
                     f"📥 {CITY_EMOJIS[r['from']]} {r['buy']:,} | 📤 {CITY_EMOJIS[r['to']]} {r['sell']:,}\n"
                     f"<pre>{liq_txt}"
                     f"          👑 <b>{r['p_p']:,}</b> | 💀 <b>{r['p_n']:,}</b>\n"
                     f"          ⏳ {fmt_t(r['bd'])} / {fmt_t(r['sd'])}</pre>\n\n")
        
        if len(full_text) + len(item_text) > 3900: messages.append(full_text); full_text = item_text
        else: full_text += item_text
    if full_text: messages.append(full_text)
    for text in messages: await msg.answer(text, parse_mode=ParseMode.HTML)

# --- Обробники Текстів (оновлені під твої кнопки) ---
@dp.message(F.text.regexp(r"📊 Попит:"))
async def toggle_liq(m, state: FSMContext):
    d = await state.get_data(); val = not d.get("check_liq", False); await state.update_data(check_liq=val)
    await m.answer(f"📊 Аналіз попиту: {'УВІМКНЕНО' if val else 'ВИМКНЕНО'}", reply_markup=get_main_kb(await state.get_data()))

@dp.message(F.text == "🚀 Запустити сканер", StateFilter('*'))
async def main_search(m, state: FSMContext):
    u_id, now = m.from_user.id, datetime.now(UTC)
    d = await state.get_data()
    if d.get("buy_limit", 0) <= 0: return await m.answer("⚠️ Спочатку налаштуй бюджет!")
    
    async with active_scans_lock:
        if u_id in active_scans: return await m.answer("⏳ Чекай, скан іде...")
        if u_id != ADMIN_ID and u_id in user_cooldowns and (now - user_cooldowns[u_id]).total_seconds() < 25:
            return await m.answer("⏳ Зачекай...")
        active_scans.add(u_id)
        
    try:
        await m.answer("🔍 Шукаю вигідні лоти...", reply_markup=ReplyKeyboardRemove())
        res = await scan_logic(d, d.get('f_c'), d.get('t_c'))
        if not res: await m.answer("📭 Порожньо", reply_markup=get_main_kb(d))
        else: await disp_res(m, res, d.get('check_liq')); await m.answer(f"✅ Знайдено: {len(res)}", reply_markup=get_main_kb(d))
    finally: active_scans.discard(u_id); user_cooldowns[u_id] = now

# --- Решта твого коду (download_items, main, тощо) залишається без змін ---
