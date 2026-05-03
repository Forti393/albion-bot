import os, json, aiohttp, asyncio, re, logging, time, statistics
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
user_cooldowns = {}; active_scans = set(); history_cache = {}

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
async def cleanup_cooldowns():
    while True:
        await asyncio.sleep(600)
        try:
            now = datetime.now(UTC) 
            expired = [uid for uid, dt in user_cooldowns.items() if (now - dt).total_seconds() > 3600]
            for uid in expired: del user_cooldowns[uid]
        except Exception: pass

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
    vol = liq_data.get('vol', 0); avg7 = liq_data.get('avg', 0)
    tier = int(re.search(r'T(\d)', item_id).group(1)) if re.search(r'T(\d)', item_id) else 4
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
                logger.info(f"✅ БД завантажена: {len(items_data)} шт.")
    except Exception: logger.exception("Помилка БД:")
    finally: is_db_ready = True
def get_start_kb():
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="❓ Допомога"), KeyboardButton(text="💰 Налаштувати бюджет")]], resize_keyboard=True)

def get_main_kb(d):
    m = d.get("mode"); m_l = "🌍 Охоплення: Всі міста" if m == "all" else "📍 Маршрут: Шлях"
    e_l = "🚫 Вимкнути фільтр 30хв" if d.get("extra") else "⚡ Свіжі ціни (30хв)"
    liq_l = "📊 Попит: ON" if d.get("check_liq") else "📊 Попит: OFF"
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="🚀 Запустити сканер")],
        [KeyboardButton(text=m_l), KeyboardButton(text=e_l)],
        [KeyboardButton(text=liq_l), KeyboardButton(text="🧮 Калькулятор")],
        [KeyboardButton(text="💰 Налаштувати бюджет"), KeyboardButton(text="🔄 Перезавантаження")],
        [KeyboardButton(text="❓ Допомога")]
    ], resize_keyboard=True)

async def scan_logic(d, f_c=None, t_c=None):
    global http_session
    if not items_data: return [] 
    res = []; b_l = d.get("buy_limit", 0); p_l = d.get("profit_limit", 4000); ext = d.get("extra", False); check_liq = d.get("check_liq", False)
    i_list = list(items_data.keys()); cities = [f_c, t_c] if f_c and t_c else CITIES
    
    for i in range(0, len(i_list), 50):
        url = f"https://europe.albion-online-data.com/api/v2/stats/prices/{','.join(i_list[i:i+50])}?locations={','.join(cities)}"
        try:
            async with http_session.get(url, timeout=20) as resp:
                if resp.status != 200: continue
                data = await resp.json()
        except Exception: continue
            
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
                bd_str = c_d[sc]['sell_price_min_date']
                b_dt = datetime.fromisoformat(bd_str.split(".")[0].replace("Z","")).replace(tzinfo=UTC)
                if (now-b_dt).total_seconds()/60 > 180: continue
                targets = [t_c] if t_c else [c for c in c_d if c!=sc]
                for tc in targets:
                    if tc not in c_d: continue
                    sk = 'buy_price_max_date' if tc=="Black Market" else 'sell_price_min_date'
                    sell = c_d[tc].get('buy_price_max' if tc=="Black Market" else 'sell_price_min', 0)
                    if sell <= buy or (sell/buy) > 10: continue
                    sd_str = c_d[tc].get(sk)
                    s_dt = datetime.fromisoformat(sd_str.split(".")[0].replace("Z","")).replace(tzinfo=UTC)
                    if (now-s_dt).total_seconds()/60 > 180: continue
                    p_n = int(sell*0.895-buy)
                    if p_n >= p_l:
                        if ext and ((now-b_dt).total_seconds()/60 > 30 or (now-s_dt).total_seconds()/60 > 30): continue
                        if p_n < 3000: continue
                        pre_item = {'id':i_id,'q':int(q),'from':sc,'to':tc,'buy':buy,'sell':sell,'p_p':int(sell*0.935-buy),'p_n':p_n,'bd':bd_str,'sd':sd_str}
                        
                        if check_liq:
                            liq = await get_item_liquidity(i_id.split("@")[0], tc)
                            passed, _ = passes_smart_filter(i_id, sell, liq)
                            if passed:
                                pre_item.update({'vol': liq['vol'], 'avg': liq['avg']})
                                if liq['avg'] > 0 and sell > liq['avg'] * 2: pre_item['is_risk'] = True
                                res.append(pre_item)
                        else: res.append(pre_item)
        if i % 300 == 0: await asyncio.sleep(0.2)
    return res

@dp.message(F.text == "🚀 Запустити сканер", StateFilter('*'))
async def main_search(m, state: FSMContext):
    u_id = m.from_user.id; d = await state.get_data(); now = datetime.now(UTC)
    if not is_db_ready: return await m.answer("⏳ БД вантажиться...")
    if d.get("buy_limit", 0) <= 0: return await m.answer("⚠️ Встанови бюджет!")
    if not d.get("mode"): return await m.answer("🗺️ Обери режим!", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🎲 Всі міста", callback_data="set_mode_all")],[InlineKeyboardButton(text="📍 Шлях", callback_data="set_mode_custom")]]))

    async with active_scans_lock:
        if u_id in active_scans: return await m.answer("⚠️ Запит обробляється!")
        if u_id != ADMIN_ID and u_id in user_cooldowns and (now - user_cooldowns[u_id]).total_seconds() < 25:
            return await m.answer(f"⏳ Зачекай {int(25-(now-user_cooldowns[u_id]).total_seconds())}с.")
        active_scans.add(u_id); user_cooldowns[u_id] = now
    
    try:
        s_msg = await m.answer("🔍 Шукаю вигоду...", reply_markup=ReplyKeyboardRemove())
        res = await scan_logic(d, d.get('f_c'), d.get('t_c'))
        await safe_delete(s_msg); await disp_res(m, res, d)
        await m.answer(f"✅ Знайдено: {len(res)}", reply_markup=get_main_kb(d))
    finally: active_scans.discard(u_id)

async def disp_res(msg, res, d):
    if not res: return await msg.answer("📭 Нічого не знайдено.")
    res.sort(key=lambda x: x['p_n'], reverse=True)
    for i in range(0, len(res[:15]), 5):
        chunk = res[i:i+5]; text = ""
        for idx, r in enumerate(chunk, i+1):
            b_id = r['id'].split("@")[0]; name = items_data.get(b_id, {}).get("LocalizedNames", {}).get("RU-RU", b_id).upper()
            for t in TRASH: name = name.replace(t, "")
            risk = "⚠️ [РИЗИК] " if r.get('is_risk') else ""
            liq = f"          📦 <b>{r.get('vol',0)} шт/д</b> | 📊 <b>{r.get('avg',0):,} мед</b>\n" if d.get("check_liq") else ""
            text += (f"{idx}) {risk}{get_item_icon(b_id)} <b>{name}</b> [{b_id.split('_')[0][1:]}.{r['id'].split('@')[1] if '@' in r['id'] else '0'}]\n"
                     f"✨ {QUALITY_NAMES.get(r['q'], 'Обычное')}\n📥 {CITY_EMOJIS[r['from']]} {r['buy']:,} | 📤 {CITY_EMOJIS[r['to']]} {r['sell']:,}\n"
                     f"<pre>{liq}          👑 <b>{r['p_p']:,}</b> | 💀 <b>{r['p_n']:,}</b>\n          🕒 {fmt_t(r['bd'])} / {fmt_t(r['sd'])}</pre>\n\n")
        await msg.answer(text, parse_mode=ParseMode.HTML)

def fmt_t(s):
    try:
        dt = datetime.fromisoformat(s.split(".")[0].replace("Z","")).replace(tzinfo=UTC)
        m = int((datetime.now(UTC)-dt).total_seconds()/60)
        return f"{m}м" if m < 60 else f"{m//60}г"
    except: return "??"

@dp.message(F.text.regexp(r"📊 Попит:"), StateFilter('*'))
async def toggle_liq(m, state: FSMContext):
    d = await state.get_data(); val = not d.get("check_liq", False); await state.update_data(check_liq=val)
    await m.answer(f"📊 Аналіз попиту: {'УВІМКНЕНО' if val else 'ВИМКНЕНО'}", reply_markup=get_main_kb(await state.get_data()))

# ... (інші обробники: Калькулятор, Бюджет, Режими, Перезавантаження з твого коду) ...

@dp.message(Command("start"), StateFilter('*'))
async def cmd_start(m, state: FSMContext): await state.clear(); await m.answer("👋 Бот готовий!", reply_markup=get_start_kb())

async def main():
    global http_session; http_session = aiohttp.ClientSession(headers=HEADERS)
    asyncio.create_task(download_items()); asyncio.create_task(cleanup_cooldowns())
    await bot.delete_webhook(drop_pending_updates=True); await dp.start_polling(bot)

if __name__ == "__main__": asyncio.run(main())
