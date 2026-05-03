import os, asyncio, aiohttp, logging, re, html, random
from datetime import datetime, timezone
from typing import List, Optional
from aiogram.fsm.state import State, StatesGroup

# --- Конфігурація ---
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0")) 
TOKEN = os.environ.get("BOT_TOKEN")
CITIES = ["Bridgewatch", "Martlock", "Lymhurst", "Thetford", "Fort Sterling", "Caerleon", "Brecilien", "Black Market"]
CITY_EMOJIS = {"Lymhurst":"🟢","Martlock":"🔵","Caerleon":"⚫","Thetford":"🟣","Bridgewatch":"🟠","Fort Sterling":"⚪","Brecilien":"🌸","Black Market":"💀"}
QUALITY_NAMES = {1:"Обычное", 2:"Хорошее", 3:"Выдающееся", 4:"Отличное", 5:"Шедевр"}
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
TRASH = ["Знаток ","Мастер ","Великий мастер ","Старейшина ","Ученик ","Новичок "]

# --- Глобальні змінні ---
items_data = {}; is_db_ready = False
http_session: Optional[aiohttp.ClientSession] = None 
scan_semaphore = asyncio.Semaphore(5) 
history_cache = {}

class BotState(StatesGroup):
    waiting_for_buy_limit = State(); waiting_for_profit_limit = State()
    picking_from = State(); picking_to = State()
    calc_count = State(); calc_buy = State(); calc_sell = State()

def fmt_t(s):
    try:
        if not s: return "??"
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
                    vol = data[0]['data'][-1].get('item_count', 0) if data and data[0].get('data') else 0
                    history_cache[cache_key] = {'volume': vol, 'time': now}
                    return vol
        except: pass
    return 0

async def scan_logic(d, f_c=None, t_c=None):
    if not items_data or not http_session: return []
    pre_res = []; b_l, p_l = d.get("buy_limit", 0), d.get("profit_limit", 4000)
    ext, check_liq = d.get("extra", False), d.get("check_liq", False)
    i_list = list(items_data.keys()); cities = [f_c, t_c] if f_c and t_c else CITIES
    
    for i in range(0, len(i_list), 50):
        url = f"https://europe.albion-online-data.com/api/v2/stats/prices/{','.join(i_list[i:i+50])}?locations={','.join(cities)}"
        data = None
        async with scan_semaphore:
            try:
                async with http_session.get(url, timeout=20) as resp:
                    if resp.status == 200: data = await resp.json()
            except: continue
        if not data: continue

        grouped = {}
        for e in data: grouped.setdefault(f"{e['item_id']}|{e['quality']}", {})[e['city']] = e
        
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
                    if sell <= buy or (sell/buy) > 10: continue
                    
                    p_n = int(sell*0.895-buy)
                    if p_n >= p_l:
                        pre_res.append({'id':i_id,'q':int(q),'from':sc,'to':tc,'buy':buy,'sell':sell,
                                        'p_p':int(sell*0.935-buy),'p_n':p_n,
                                        'bd':c_d[sc]['sell_price_min_date'],
                                        'sd':c_d[tc]['buy_price_max_date'] if tc=="Black Market" else c_d[tc]['sell_price_min_date']})
    
    final_res = []
    if check_liq:
        pre_res.sort(key=lambda x: x['p_n'], reverse=True)
        for item in pre_res[:20]:
            vol = await get_item_liquidity(item['id'].split("@")[0], item['to'])
            # Нова логіка фільтру: якщо 0 продажів, пропускаємо, КРІМ випадків з адекватною ціною (наприклад, профіт > 20%)
            is_price_ok = (item['p_n'] / item['buy']) > 0.2
            if vol > 0 or is_price_ok:
                item['vol'] = vol
                final_res.append(item)
        return final_res
    return pre_res
import asyncio, logging
from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode, ChatAction
from aiogram.filters import Command, StateFilter
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from core import * # Імпорт ядра

bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())

def get_item_icon(unique_name):
    un = unique_name.lower()
    if any(x in un for x in ["hood", "cowl", "helmet", "cap"]): return "🪖"
    if any(x in un for x in ["armor", "jacket", "robe", "garb"]): return "🧥"
    if any(x in un for x in ["shoes", "boots", "sandals"]): return "🥾"
    if any(x in un for x in ["sword", "axe", "bow", "staff", "hammer", "mace", "dagger", "spear", "glove"]): return "⚔️"
    return "📦"

async def disp_res(msg, res, d):
    res.sort(key=lambda x: x['p_n'], reverse=True)
    show_liq = d.get("check_liq")
    messages, full_text = [], ""
    
    for idx, r in enumerate(res[:15], 1):
        b_id = r['id'].split("@")[0]
        icon = get_item_icon(b_id)
        tier = b_id.split('_')[0][1:]
        enc = r['id'].split("@")[1] if "@" in r['id'] else "0"
        name = items_data.get(b_id, {}).get("LocalizedNames", {}).get("RU-RU", b_id)
        name = re.sub(r'\s*\([^)]*\)', '', name).upper()
        for t in TRASH: name = name.replace(t, "")
        
        tbd, tsd = fmt_t(r.get('bd')), fmt_t(r.get('sd'))
        
        # Формування блоку з часом напроти ціни
        liq_line = f"📊 Попит: <b>{r.get('vol', 0)} шт/д</b>\n" if show_liq else ""
        item_block = (
            f"{idx}) {icon} <b>{name}</b> [{tier}.{enc}]\n"
            f"✨ {QUALITY_NAMES.get(r['q'], 'Обычное')}\n"
            f"📥 {CITY_EMOJIS[r['from']]} {r['buy']:,} | 🕒 {tbd}\n"
            f"📤 {CITY_EMOJIS[r['to']]} {r['sell']:,} | 🕒 {tsd}\n"
            f"{liq_line}"
            f"💰 Пр: 👑 <b>{r['p_p']:,}</b> | 💀 <b>{r['p_n']:,}</b>\n\n"
        )
        if len(full_text) + len(item_block) > 3900:
            messages.append(full_text); full_text = item_block
        else: full_text += item_block
    
    if full_text: messages.append(full_text)
    for t in messages: await msg.answer(t, parse_mode=ParseMode.HTML)

# --- Клавіатури ---
def get_main_kb(d):
    m_l = "🌍 Всі міста" if d.get("mode") == "all" else "📍 Шлях"
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="🚀 Запустити сканер")],
        [KeyboardButton(text=m_l), KeyboardButton(text=f"⚡ 30хв: {'ON' if d.get('extra') else 'OFF'}")],
        [KeyboardButton(text=f"📊 Попит: {'ON' if d.get('check_liq') else 'OFF'}"), KeyboardButton(text="🧮 Калькулятор")],
        [KeyboardButton(text="💰 Бюджет"), KeyboardButton(text="🔄 Скинути")]
    ], resize_keyboard=True)

# --- Обробники (спрощено для прикладу) ---
@dp.message(F.text == "🚀 Запустити сканер", StateFilter('*'))
async def main_search(m, state: FSMContext):
    d = await state.get_data()
    if d.get("buy_limit", 0) <= 0: return await m.answer("⚠️ Встанови бюджет!")
    s_msg = await m.answer("🔍 Шукаю...")
    res = await scan_logic(d, d.get('f_c'), d.get('t_c'))
    await s_msg.delete()
    if not res: await m.answer("📭 Нічого не знайдено за вашим запитом")
    else: await disp_res(m, res, d)

# ... (інші хендлери скидання, бюджету та калькулятора залишаються без змін) ...

async def main():
    global http_session
    async with aiohttp.ClientSession(headers=HEADERS) as session:
        globals()['http_session'] = session
        # Тут завантаження БД та запуск полінгу
        await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
