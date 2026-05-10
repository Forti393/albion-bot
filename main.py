import os, json, aiohttp, asyncio, re, logging, html, time as time_module
from datetime import datetime, timezone
from typing import List, Optional, Dict
from aiogram import Bot, Dispatcher, types, F
from aiogram.enums import ParseMode, ChatAction
from aiogram.filters import Command, StateFilter
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

# Налаштування логування
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- КОНФІГУРАЦІЯ ---
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
TOKEN = os.environ.get("BOT_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

gemini_client = None
AVAILABLE_GEMINI_MODELS = []
try:
    from google import genai
    if GEMINI_API_KEY:
        gemini_client = genai.Client(api_key=GEMINI_API_KEY)
        models_response = gemini_client.models.list()
        AVAILABLE_GEMINI_MODELS = [m.name for m in models_response if "gemini" in m.name.lower()]
except Exception as e:
    logger.error(f"Помилка Gemini: {e}")

bot = Bot(token=TOKEN) if TOKEN else None
dp = Dispatcher(storage=MemoryStorage())

# Глобальні змінні
items_data = {}
is_db_ready = False
http_session: Optional[aiohttp.ClientSession] = None
scan_semaphore = asyncio.Semaphore(5)
history_cache = {}
history_fallback_cache = {}
price_cache = {}
price_cache_time = 0.0
last_scan_time: Dict[int, float] = {}

CITIES = ["Bridgewatch", "Martlock", "Lymhurst", "Thetford", "Fort Sterling", "Caerleon", "Brecilien", "Black Market"]
CITY_EMOJIS = {"Lymhurst":"🟢","Martlock":"🔵","Caerleon":"🔴","Thetford":"🟣","Bridgewatch":"🟠","Fort Sterling":"⚪","Brecilien":"🌸","Black Market":"⚫"}
QUALITY_NAMES = {1:"Обычное", 2:"Хорошее", 3:"Выдающееся", 4:"Отличное", 5:"Шедевр"}
TRASH = ["Знаток ","Мастер ","Великий мастер ","Старейшина ","Ученик ","Новичок "]
HEADERS = {"User-Agent": "Mozilla/5.0"}
RATIO_OPTIONS = [1.5, 2.0, 2.5, 3.0]
AVG_MULTIPLIER_OPTIONS = [1.2, 1.5, 1.8, 2.0, 2.5, 3.0, 4.0]

class BotState(StatesGroup):
    waiting_for_buy_limit = State()
    waiting_for_profit_limit = State()
    waiting_for_max_ratio = State()
    waiting_for_avg_mult = State()
    picking_from = State()
    picking_to = State()
    picking_origin = State()
    calc_count = State()
    calc_buy = State()
    calc_sell = State()
    settings_menu = State()

def is_blacklisted(unique_name):
    un = unique_name.upper()
    return any(x in un for x in ["OFF_BOOK", "NONTRADABLE", "QUEST", "UNIQUE"])

def get_item_icon(un):
    un = un.lower()
    if any(x in un for x in ["hood","cowl","helmet","cap"]): return "🪖"
    if any(x in un for x in ["armor","jacket","robe","garb"]): return "🧥"
    if any(x in un for x in ["shoes","boots","sandals"]): return "🥾"
    if any(x in un for x in ["sword","axe","bow","staff","hammer","mace","dagger","spear","glove"]): return "⚔️"
    if "bag" in un: return "🎒"
    if "cape" in un: return "🧣"
    if "mount" in un: return "🐴"
    return "📦"

def fmt_t(s):
    try:
        dt = datetime.fromisoformat(s.split(".")[0].replace("Z","")).replace(tzinfo=timezone.utc)
        m = int((datetime.now(timezone.utc)-dt).total_seconds()/60)
        return f"{m}м" if m<60 else f"{m//60}г"
    except: return "??"

async def download_items():
    global items_data, is_db_ready, http_session
    url = "https://raw.githubusercontent.com/ao-data/ao-bin-dumps/master/formatted/items.json"
    allowed_types = ["WEAPON","ARMOR","PLATE","LEATHER","CLOTH","BAG","CAPE","POTION","MEAL","MOUNT","TOOL","OFFHAND"]
    try:
        async with http_session.get(url, timeout=60) as r:
            if r.status == 200:
                data = await r.json(content_type=None)
                new_items = {str(i.get("UniqueName")).upper(): i for i in data if i.get("UniqueName") and i.get("UniqueName")[1] in "45678" and any(x in i.get("UniqueName").upper() for x in allowed_types) and not is_blacklisted(i.get("UniqueName"))}
                items_data = new_items
                is_db_ready = True
                logger.info(f"Базу предметів завантажено: {len(items_data)} позицій")
    except Exception as e:
        logger.error(f"Помилка завантаження бази: {e}")
async def fetch_prices_with_cache(item_ids, cities):
    global price_cache, price_cache_time
    now = time_module.time()
    if price_cache and (now - price_cache_time) < 60: return price_cache.get('data', [])
    all_data = []
    for i in range(0, len(item_ids), 50):
        chunk = item_ids[i:i+50]
        url = f"https://europe.albion-online-data.com/api/v2/stats/prices/{','.join(chunk)}?locations={','.join(cities)}"
        async with scan_semaphore:
            try:
                async with http_session.get(url, timeout=15) as resp:
                    if resp.status == 200: all_data.extend(await resp.json())
            except: continue
    price_cache = {'data': all_data, 'time': now}; price_cache_time = now
    return all_data

async def get_item_liquidity_fallback(item_id, city, quality):
    cache_key = f"{item_id}|{city}|{quality}"
    if cache_key in history_cache: return history_cache[cache_key]['vol'], history_cache[cache_key]['avg'], "24г"
    url = f"https://europe.albion-online-data.com/api/v2/stats/history/{item_id}?locations={city}&time-series=1&qualities={quality}"
    async with scan_semaphore:
        try:
            async with http_session.get(url, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data and data[0].get('data'):
                        d = data[0]['data'][-1]
                        vol, avg = d.get('item_count', 0), d.get('avg_price', 0)
                        history_cache[cache_key] = {'vol': vol, 'avg': avg}
                        return vol, avg, "24г"
        except: pass
    return 0, 0, None

async def scan_logic(d, f_c=None, t_c=None):
    if not items_data or not http_session: return []
    b_l, p_l = d.get("buy_limit", 0), d.get("profit_limit", 4000)
    max_ratio, max_avg_mult = float(d.get("max_ratio", 2.0)), float(d.get("max_avg_mult", 4.0))
    allow_zero_avg = d.get("allow_zero_avg", False)

    i_list = list(items_data.keys())
    q_cities = CITIES if not f_c else list(set([f_c] + CITIES))
    data = await fetch_prices_with_cache(i_list, q_cities)
    if not data: return []

    grouped = {}
    for e in data:
        k = f"{e['item_id']}|{e['quality']}"
        grouped.setdefault(k, {})[e['city']] = e

    pre_res = []
    now = datetime.now(timezone.utc)
    for k, c_d in grouped.items():
        i_id, q = k.split("|")
        srcs = [f_c] if f_c else [c for c in c_d if c != "Black Market"]
        for sc in srcs:
            if sc not in c_d: continue
            buy = c_d[sc].get('sell_price_min', 0)
            if buy <= 1000 or (b_l > 0 and buy > b_l): continue
            try:
                if (now - datetime.fromisoformat(c_d[sc]['sell_price_min_date'].split(".")[0]).replace(tzinfo=timezone.utc)).total_seconds() > 18000: continue
            except: continue

            targets = [t_c] if t_c else [c for c in CITIES if c != sc]
            for tc in targets:
                if tc not in c_d: continue
                is_bm = (tc == "Black Market")
                s_order, s_direct = c_d[tc].get('sell_price_min', 0), c_d[tc].get('buy_price_max', 0)
                sell_ref = s_order if s_order > 0 else s_direct
                if sell_ref <= buy or sell_ref > buy * max_ratio: continue

                tax = 0.91 if is_bm else 0.895
                p_n = int(s_order * tax - buy) if s_order > 0 else -999999
                p_d = int(s_direct * tax - buy) if s_direct > 0 else -999999
                
                if max(p_n, p_d) < p_l: continue
                pre_res.append({'id': i_id, 'q': int(q), 'from': sc, 'to': tc, 'buy': buy, 'sell': s_order, 'direct': s_direct, 'p_n': p_n, 'p_d': p_d, 'bd': c_d[sc]['sell_price_min_date'], 'sd': c_d[tc]['sell_price_min_date'] if not is_bm else c_d[tc]['buy_price_max_date']})

    if d.get("mode") == "origin" and f_c:
        dest_stats = {}
        for r in pre_res: dest_stats[r['to']] = dest_stats.get(r['to'], 0) + r['p_n']
        if dest_stats:
            best = max(dest_stats, key=dest_stats.get)
            pre_res = [r for r in pre_res if r['to'] == best]

    final, seen = [], set()
    for item in sorted(pre_res, key=lambda x: x['p_n'], reverse=True)[:100]:
        vol, avg_p, per = await get_item_liquidity_fallback(item['id'], item['to'], item['q'])
        if (avg_p > 0 and item['sell'] > avg_p * max_avg_mult) or (avg_p == 0 and not allow_zero_avg): continue
        u_key = f"{item['id']}{item['q']}{item['from']}{item['to']}{item['buy']}"
        if u_key not in seen:
            item.update({'vol': vol, 'avg_p': avg_p, 'per': per})
            final.append(item); seen.add(u_key)
    return final[:15]
async def disp_res(msg, res, d):
    if not res: return await msg.answer("📭 Нічого не знайдено.")
    full_text = f"🔎 <b>Топ {len(res)} пропозицій:</b>\n\n"
    for idx, r in enumerate(res, 1):
        name = items_data.get(r['id'].split("@")[0], {}).get("LocalizedNames", {}).get("RU-RU", r['id'])
        name = re.sub(r'\s*\([^)]*\)', '', html.escape(name.upper()))
        for t in TRASH: name = name.replace(t, "")
        icon = get_item_icon(r['id'])
        is_bm = r['to'] == "Black Market"
        
        profit_info = f"📦 Ордер: +{r['p_n']:,}\n⚡ Викуп: " + (f"+{r['p_d']:,}" if r['p_d'] > -1000 else "---") if is_bm else f"💰 Профіт: +{r['p_n']:,}"
        
        block = (
            f"{idx}) {icon} <b>{name}</b> [{r['id'].split('_')[0][1:]}]\n"
            f"✨ {QUALITY_NAMES.get(r['q'])} | {CITY_EMOJIS[r['from']]} ➔ {CITY_EMOJIS[r['to']]}\n"
            f"📥 Купівля: {r['buy']:,} ({fmt_t(r['bd'])})\n"
            f"📤 Продаж: {r['sell']:,} ({fmt_t(r['sd'])})\n"
            f"<pre>{profit_info}\n"
            f"🐢 Попит: {r['vol']} шт/д | СЦ: {r['avg_p']:,}</pre>\n"
            f"───────────────────\n"
        )
        if len(full_text) + len(block) > 4000:
            await msg.answer(full_text, parse_mode=ParseMode.HTML); full_text = block
        else: full_text += block
    await msg.answer(full_text, parse_mode=ParseMode.HTML)

def get_main_kb(d):
    mode, budget = d.get("mode"), d.get("buy_limit", 0)
    m_txt = f"Режим: {mode}" if mode else "🗺 Режим"
    kb = [[KeyboardButton(text="🚀 Запустити сканер")]] if budget > 0 and mode else []
    kb += [[KeyboardButton(text="💰 Бюджет"), KeyboardButton(text=m_txt)], [KeyboardButton(text="⚙️ Налаштування"), KeyboardButton(text="🧮 Калькулятор")], [KeyboardButton(text="🔄 Скинути")]]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

@dp.message(Command("start"))
async def start(m, state: FSMContext):
    await state.clear(); await m.answer("👋 Привіт! Встанови бюджет та режим.", reply_markup=get_main_kb({}))

@dp.message(F.text == "🚀 Запустити сканер")
async def run_scan(m, state: FSMContext):
    d = await state.get_data()
    if not is_db_ready: await download_items()
    await bot.send_chat_action(m.chat.id, ChatAction.TYPING)
    s_msg = await m.answer("🔍 Аналізую ринок...")
    res = await scan_logic(d, d.get('f_c'), d.get('t_c'))
    try: await s_msg.delete()
    except: pass
    await disp_res(m, res, d)

@dp.message(F.text == "💰 Бюджет")
async def set_budget(m, state: FSMContext):
    await state.set_state(BotState.waiting_for_buy_limit)
    await m.answer("Введіть суму бюджету:")

@dp.message(StateFilter(BotState.waiting_for_buy_limit))
async def save_budget(m, state: FSMContext):
    try:
        val = int(m.text.replace(" ",""))
        await state.update_data(buy_limit=val); await state.set_state(None)
        await m.answer(f"✅ Бюджет: {val:,}", reply_markup=get_main_kb(await state.get_data()))
    except: await m.answer("❌ Введіть число.")

@dp.message(F.text.contains("Режим"))
async def set_mode(m):
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🌍 Всі міста", callback_data="m_all")],[InlineKeyboardButton(text="📍 З міста", callback_data="m_org")]])
    await m.answer("Оберіть режим:", reply_markup=kb)

@dp.callback_query(F.data.startswith("m_"))
async def mode_cb(cb, state: FSMContext):
    if cb.data == "m_all":
        await state.update_data(mode="all", f_c=None, t_c=None); await cb.message.answer("🌍 Режим: Всі міста", reply_markup=get_main_kb(await state.get_data()))
    else:
        await state.set_state(BotState.picking_origin)
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=c, callback_data=f"org_{c}")] for c in CITIES if c != "Black Market"])
        await cb.message.answer("З якого міста виїжджаємо?", reply_markup=kb)
    await cb.answer()

@dp.callback_query(F.data.startswith("org_"))
async def org_cb(cb, state: FSMContext):
    city = cb.data.split("_")[1]
    await state.update_data(mode="origin", f_c=city, t_c=None)
    await cb.message.answer(f"📍 Режим: З міста {city}", reply_markup=get_main_kb(await state.get_data()))
    await cb.answer()

async def main():
    global http_session
    http_session = aiohttp.ClientSession(headers=HEADERS)
    await download_items()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
