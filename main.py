import os, json, aiohttp, asyncio, re, logging, signal, html, time as time_module
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Dict
from aiogram import Bot, Dispatcher, types, F
from aiogram.enums import ParseMode, ChatAction
from aiogram.filters import Command, StateFilter
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- CONFIG ---
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
TOKEN = os.environ.get("BOT_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

try:
    from google import genai
    gemini_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None
except:
    gemini_client = None

bot = Bot(token=TOKEN) if TOKEN else None
dp = Dispatcher(storage=MemoryStorage())

items_data = {}
is_db_ready = False
http_session: Optional[aiohttp.ClientSession] = None
scan_semaphore = asyncio.Semaphore(5)
history_cache = {}
price_cache = {}
last_scan_time: Dict[int, float] = {}

CITIES = ["Bridgewatch", "Martlock", "Lymhurst", "Thetford", "Fort Sterling", "Caerleon", "Brecilien", "Black Market"]
CITY_EMOJIS = {"Lymhurst":"🟢","Martlock":"🔵","Caerleon":"🔴","Thetford":"🟣","Bridgewatch":"🟠","Fort Sterling":"⚪","Brecilien":"🌸","Black Market":"⚫"}
QUALITY_NAMES = {1:"Обычное", 2:"Хорошее", 3:"Выдающееся", 4:"Отличное", 5:"Шедевр"}
TRASH = ["Знаток ","Мастер ","Великий мастер ","Старейшина ","Ученик ","Новичок "]
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
    if any(x in un for x in ["OFF_BOOK", "NONTRADABLE", "QUEST"]): return True
    return False

async def download_items():
    global items_data, is_db_ready, http_session
    url = "https://raw.githubusercontent.com/ao-data/ao-bin-dumps/master/formatted/items.json"
    allowed_types = ["WEAPON","ARMOR","PLATE","LEATHER","CLOTH","BAG","CAPE","POTION","MEAL","MOUNT","TOOL","OFFHAND"]
    async with http_session.get(url, timeout=60) as r:
        if r.status == 200:
            data = await r.json(content_type=None)
            new_items = {}
            for i in data:
                uid = str(i.get("UniqueName", "")).upper()
                if not uid.startswith("T") or len(uid) < 3: continue
                if uid[1] not in "45678": continue
                if not any(x in uid for x in allowed_types): continue
                if is_blacklisted(uid): continue
                new_items[uid] = i
            items_data = new_items
            is_db_ready = True
            logger.info(f"Базу предметів завантажено: {len(items_data)} позицій")
async def scan_logic(d, f_c=None, t_c=None):
    if not items_data or not http_session: return []
    
    b_l = d.get("buy_limit", 0)
    p_l = d.get("profit_limit", 4000)
    max_ratio = float(d.get("max_ratio", 2.0))
    max_avg_mult = float(d.get("max_avg_mult", 4.0))
    allow_zero_avg = d.get("allow_zero_avg", False)
    
    # Отримуємо ціни
    i_list = list(items_data.keys())
    # Формуємо список міст для запиту
    query_cities = CITIES if not f_c and not t_c else (list(set([f_c, t_c] + CITIES)) if f_c else CITIES)
    
    # Кешування цін на 60 сек
    now_ts = time_module.time()
    prices = await fetch_prices_with_cache(i_list, query_cities)
    if not prices: return []

    grouped = {}
    for e in prices:
        k = f"{e['item_id']}|{e['quality']}"
        grouped.setdefault(k, {})[e['city']] = e

    pre_res = []
    now_dt = datetime.now(timezone.utc)

    for k, c_d in grouped.items():
        i_id, q = k.split("|")
        # Звідки веземо
        sources = [f_c] if f_c else [c for c in c_d if c != "Black Market"]
        
        for sc in sources:
            if sc not in c_d or sc == "Black Market": continue
            buy_price = c_d[sc].get('sell_price_min', 0)
            if buy_price <= 1000 or (b_l > 0 and buy_price > b_l): continue
            
            # Куди веземо
            targets = [t_c] if t_c else [c for c in CITIES if c != sc]
            for tc in targets:
                if tc not in c_d: continue
                is_bm = (tc == "Black Market")
                
                # Логіка цін продажу
                s_order_price = c_d[tc].get('sell_price_min', 0)
                s_direct_price = c_d[tc].get('buy_price_max', 0)
                
                # Вибираємо кращу ціну для базового розрахунку
                sell_price = s_order_price if s_order_price > 0 else s_direct_price
                if sell_price <= buy_price or sell_price > buy_price * max_ratio: continue

                # Податки (BM: 9%, Інші: 10.5% для ордерів)
                tax = 0.91 if is_bm else 0.895
                profit_order = int(s_order_price * tax - buy_price)
                profit_direct = int(s_direct_price * tax - buy_price) if s_direct_price > 0 else -999999
                
                # Головний профіт для фільтрації
                main_profit = max(profit_order, profit_direct)
                if main_profit < p_l: continue

                pre_res.append({
                    'id': i_id, 'q': int(q), 'from': sc, 'to': tc, 
                    'buy': buy_price, 'sell': s_order_price, 'direct': s_direct_price,
                    'p_n': profit_order, 'p_direct': profit_direct,
                    'bd': c_d[sc]['sell_price_min_date'], 'sd': c_d[tc]['sell_price_min_date']
                })

    # --- Smart Origin Mode ---
    if d.get("mode") == "origin" and f_c:
        city_stats = {}
        for r in pre_res:
            city_stats[r['to']] = city_stats.get(r['to'], 0) + r['p_n']
        if city_stats:
            best_destination = max(city_stats, key=city_stats.get)
            pre_res = [r for r in pre_res if r['to'] == best_destination]

    # Збагачення ліквідністю та фільтрація
    final = []
    # Сет для унікальності саме в поточному результаті (Назва+Якість+Міста)
    seen_in_this_scan = set()

    for item in pre_res[:100]:
        vol, avg_p, per = await get_item_liquidity_fallback(item['id'], item['to'], item['q'])
        
        # Перевірка на антифейк
        if avg_p > 0 and item['sell'] > (avg_p * max_avg_mult): continue
        if avg_p == 0 and not allow_zero_avg: continue
        
        item['vol'] = vol; item['avg_p'] = avg_p; item['period'] = per
        
        unique_key = f"{item['id']}{item['q']}{item['from']}{item['to']}{item['buy']}"
        if unique_key not in seen_in_this_scan:
            final.append(item)
            seen_in_this_scan.add(unique_key)

    final.sort(key=lambda x: x['p_n'], reverse=True)
    return final[:15]
async def disp_res(msg, res, d):
    if not res: return await msg.answer("📭 Нічого не знайдено.")
    
    messages = []
    current_msg = f"🔎 Знайдено <b>{len(res)}</b> позицій:\n\n"
    
    for idx, r in enumerate(res, 1):
        name = items_data.get(r['id'].split("@")[0], {}).get("LocalizedNames", {}).get("RU-RU", r['id'])
        name = re.sub(r'\s*\([^)]*\)', '', name).upper()
        icon = get_item_icon(r['id'])
        is_bm = r['to'] == "Black Market"
        
        # Формування рядка прибутку для BM
        if is_bm:
            profit_str = f"📦 Ордер:  +{r['p_n']:,}\n⚡ Викуп:  " + (f"+{r['p_direct']:,}" if r['p_direct'] > -1000 else "---")
        else:
            profit_str = f"💰 Профіт: +{r['p_n']:,}"

        block = (
            f"{idx}) {icon} <b>{name}</b>\n"
            f"✨ {QUALITY_NAMES.get(r['q'], 'Обычное')} | {CITY_EMOJIS[r['from']]} ➔ {CITY_EMOJIS[r['to']]}\n"
            f"📥 Купівля: {r['buy']:,} ({fmt_t(r['bd'])})\n"
            f"📤 Продаж: {r['sell']:,} ({fmt_t(r['sd'])})\n"
            f"<pre>{profit_str}\n"
            f"📈 Попит: {r['vol']} шт/д | СЦ: {r['avg_p']:,}</pre>\n"
            f"───────────────────\n"
        )
        
        if len(current_msg) + len(block) > 3800:
            messages.append(current_msg)
            current_msg = block
        else:
            current_msg += block
            
    messages.append(current_msg)
    for t in messages: await msg.answer(t, parse_mode=ParseMode.HTML)

# --- Допоміжні функції (потрібно додати в кінець) ---
async def fetch_prices_with_cache(ids, cities):
    # Тут залишається твоя функція fetch_prices_with_cache
    pass 

# ... Решта твоїх хендлерів кнопок ...

if __name__=="__main__":
    asyncio.run(main())
