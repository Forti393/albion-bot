import os, json, aiohttp, asyncio, re, logging, time, signal, random, html
from datetime import datetime, timezone, timedelta
from typing import List, Optional
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

items_data = {}; is_db_ready = False
http_session: Optional[aiohttp.ClientSession] = None 
scan_semaphore = asyncio.Semaphore(5) 
history_cache = {}
is_shutting_down = False

CACHE_TTL = 3600 
CITIES = ["Bridgewatch", "Martlock", "Lymhurst", "Thetford", "Fort Sterling", "Caerleon", "Brecilien", "Black Market"]
CITY_EMOJIS = {"Lymhurst":"🟢","Martlock":"🔵","Caerleon":"🔴","Thetford":"🟣","Bridgewatch":"🟠","Fort Sterling":"⚪","Brecilien":"🌸","Black Market":"⚫"}
QUALITY_NAMES = {1:"Обычное", 2:"Хорошее", 3:"Выдающееся", 4:"Отличное", 5:"Шедевр"}
TRASH = ["Знаток ","Мастер ","Великий мастер ","Старейшина ","Ученик ","Новичок "]
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

# Параметри за замовчуванням (тепер великий бюджет та менший прибуток)
DEFAULT_BUY_LIMIT = 10**9       # без обмежень
DEFAULT_PROFIT_LIMIT = 1000     # мінімальний прибуток 1000 срібла
DEFAULT_FRESH_MINUTES = 360     # 6 годин (було 180)

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
                            except: is_recent = False

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
                        return res_vol, res_p
        except Exception: pass
    
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
    except Exception: pass
async def scan_logic(d, f_c=None, t_c=None):
    if not items_data or not http_session or is_shutting_down: return []
    pre_res = []
    # Якщо бюджет не заданий – без обмежень, інакше використовуємо вказаний
    b_l = d.get("buy_limit") or DEFAULT_BUY_LIMIT
    p_l = d.get("profit_limit", DEFAULT_PROFIT_LIMIT)
    ext = d.get("extra", False)
    check_liq_limit = d.get("check_liq", False)
    fresh_minutes = DEFAULT_FRESH_MINUTES if not ext else 30  # в режимі "30хв" свіжість 30 хв
    
    i_list = list(items_data.keys())
    cities = [f_c, t_c] if f_c and t_c else CITIES
    for i in range(0, len(i_list), 50):
        if is_shutting_down: break
        url = f"https://europe.albion-online-data.com/api/v2/stats/prices/{','.join(i_list[i:i+50])}?locations={','.join(cities)}"
        data = None
        async with scan_semaphore:
            try:
                async with http_session.get(url, timeout=15) as resp:
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
                # Видалено перевірку buy <= 500, залишаємо лише некоректні значення та перевищення бюджету
                if buy <= 0 or buy > b_l: continue
                
                try:
                    b_dt = datetime.fromisoformat(c_d[sc]['sell_price_min_date'].split(".")[0]).replace(tzinfo=timezone.utc)
                    if (now - b_dt).total_seconds()/60 > fresh_minutes: continue
                except: continue
                
                targets = [t_c] if t_c else [c for c in c_d if c != sc]
                for tc in targets:
                    if tc not in c_d: continue
                    is_bm = (tc == "Black Market")
                    sell = c_d[tc].get('buy_price_max' if is_bm else 'sell_price_min', 0)
                    if sell <= buy: continue
                    
                    try:
                        sk = 'buy_price_max_date' if is_bm else 'sell_price_min_date'
                        s_dt = datetime.fromisoformat(c_d[tc][sk].split(".")[0]).replace(tzinfo=timezone.utc)
                        if (now - s_dt).total_seconds()/60 > fresh_minutes: continue
                    except: continue

                    tax = 0.91 if is_bm else 0.895
                    p_n = int(sell * tax - buy)
                    p_p = int(sell * (tax + 0.04) - buy)
                    
                    if p_n >= p_l:
                        pre_res.append({'id':i_id,'q':int(q),'from':sc,'to':tc,'buy':buy,'sell':sell,
                                        'p_p':p_p,'p_n':p_n,'bd':c_d[sc]['sell_price_min_date'],'sd':c_d[tc][sk]})

    pre_res.sort(key=lambda x: x['p_n'], reverse=True)
    
    final_list = []
    for item in pre_res[:150]:
        vol, avg_p = await get_item_liquidity(item['id'], item['to'], item['q'])
        
        if avg_p > 0 and item['sell'] > (avg_p * 3):
            continue

        min_v = 10 if check_liq_limit else 1
        if vol < min_v: continue
            
        item['vol'], item['avg_p'] = vol, avg_p
        item['score'] = int((item['p_n'] * max(vol, 1)) / max(item['buy'], 1))
        final_list.append(item)
        if len(final_list) >= 15: break

    final_list.sort(key=lambda x: x.get('score', 0), reverse=True)
    return final_list

async def disp_res(msg, res, d):
    messages, full_text = [], ""
    for idx, r in enumerate(res, 1):
        id_parts = r['id'].split("@")
        b_id = id_parts[0]; tier = b_id.split('_')[0][1:]
        enc = id_parts[1] if len(id_parts) > 1 else "0"
        icon = get_item_icon(b_id)
        name = items_data.get(b_id, {}).get("LocalizedNames", {}).get("RU-RU", b_id)
        name = re.sub(r'\s*\([^)]*\)', '', html.escape(name.upper()))
        for t in TRASH: name = name.replace(t, "")
        
        tbd, tsd = fmt_t(r.get('bd')), fmt_t(r.get('sd'))
        
        liq = r.get('vol', 0)
        if liq > 100: lbl = "🔥"
        elif liq > 30: lbl = "⚡"
        elif liq > 5: lbl = "✅"
        else: lbl = "🐢"

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
            messages.append(full_text); full_text = item_block
        else: full_text += item_block
    if full_text: messages.append(full_text)
    for t in messages: await msg.answer(t, parse_mode=ParseMode.HTML)

def get_main_kb(d):
    mode = d.get("mode")
    budget = d.get("buy_limit")
    searched = d.get("has_searched", False)
    m_btn = "Режим: 🌍 Всі міста" if mode == "all" else ("Режим: 📍 Шлях" if mode == "custom" else "🗺 Режим")
    kb = []
    # Кнопка сканування доступна, якщо вибрано хоча б режим (режим тепер є за замовчуванням)
    if mode: kb.append([KeyboardButton(text="🚀 Запустити сканер")])
    if searched:
        kb.append([KeyboardButton(text=m_btn), KeyboardButton(text=f"⚡ 30хв: {'ON' if d.get('extra') else 'OFF'}")])
        kb.append([KeyboardButton(text=f"📊 Попит Ліміт: {'ON' if d.get('check_liq') else 'OFF'}"), KeyboardButton(text="🧮 Калькулятор")])
        kb.append([KeyboardButton(text="💰 Бюджет"), KeyboardButton(text="🔄 Скинути")])
    else:
        kb.append([KeyboardButton(text="💰 Бюджет"), KeyboardButton(text=m_btn)])
        kb.append([KeyboardButton(text="🧮 Калькулятор"), KeyboardButton(text="❓ Допомога")])
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

@dp.message(Command("start"), StateFilter('*'))
async def cmd_start(m, state: FSMContext):
    await state.clear()
    # Встановлюємо зручні параметри за замовчуванням
    await state.update_data(buy_limit=DEFAULT_BUY_LIMIT, profit_limit=DEFAULT_PROFIT_LIMIT, mode="all")
    await m.answer("👋 <b>Привіт! Я Albion Trade Bot.</b>\n"
                   "Стандартні налаштування: бюджет без обмежень, прибуток від 1000 срібла.\n"
                   "Можете змінити кнопками нижче.",
                   parse_mode=ParseMode.HTML, reply_markup=get_main_kb({"mode":"all"}))