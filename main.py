import os, json, aiohttp, asyncio, re, logging, time, signal, random, html
from datetime import datetime, timezone
from typing import List, Optional, Tuple
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
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0")) 
TOKEN = os.environ.get("BOT_TOKEN")

if not TOKEN:
    logger.error("🚨 BOT_TOKEN відсутній!")
    exit(1)

bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# Глобальні змінні
items_data = {}; is_db_ready = False
http_session: Optional[aiohttp.ClientSession] = None 
scan_semaphore = asyncio.Semaphore(5) 
history_cache = {} # Кеш для попиту та середніх цін
is_shutting_down = False

# Константи міст
CITIES = ["Bridgewatch", "Martlock", "Lymhurst", "Thetford", "Fort Sterling", "Caerleon", "Brecilien", "Black Market"]
CITY_EMOJIS = {
    "Lymhurst":"🟢", "Martlock":"🔵", "Caerleon":"🔴", "Thetford":"🟣", 
    "Bridgewatch":"🟠", "Fort Sterling":"⚪", "Brecilien":"🌸", "Black Market":"⚫"
}
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
    except: pass

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
    except: return "??"

async def get_item_history(item_id: str, city: str) -> Tuple[int, int]:
    """Повертає (кількість_продажів, середня_ціна)"""
    global http_session
    if not http_session or http_session.closed or is_shutting_down: return 0, 0
    
    cache_key, now = f"{item_id}|{city}", datetime.now(timezone.utc)
    if cache_key in history_cache and (now - history_cache[cache_key]['time']).total_seconds() < 3600:
        return history_cache[cache_key]['vol'], history_cache[cache_key]['avg']

    url = f"https://europe.albion-online-data.com/api/v2/stats/history/{item_id}?locations={city}&time-series=1"
    async with scan_semaphore:
        try:
            async with http_session.get(url, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data and data[0].get('data'):
                        last_entry = data[0]['data'][-1]
                        vol = last_entry.get('item_count', 0)
                        avg = int(last_entry.get('avg_price', 0))
                        history_cache[cache_key] = {'vol': vol, 'avg': avg, 'time': now}
                        return vol, avg
        except: pass
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
    except: is_db_ready = True

async def scan_logic(d, f_c=None, t_c=None):
    global http_session
    if not items_data or not http_session or is_shutting_down: return []
    pre_res = []; b_l, p_l = d.get("buy_limit", 0), d.get("profit_limit", 4000)
    ext, check_liq = d.get("extra", False), d.get("check_liq", False)
    
    # Визначаємо список міст. Блекмаркет виключений з джерел закупівлі за замовчуванням
    i_list = list(items_data.keys())
    search_cities = [f_c, t_c] if f_c and t_c else CITIES

    for i in range(0, len(i_list), 50):
        if is_shutting_down: break
        url = f"https://europe.albion-online-data.com/api/v2/stats/prices/{','.join(i_list[i:i+50])}?locations={','.join(search_cities)}"
        data = None
        async with scan_semaphore:
            for attempt in range(3):
                try:
                    async with http_session.get(url, timeout=20) as resp:
                        if resp.status == 200: data = await resp.json(); break
                        if resp.status == 429: await asyncio.sleep(2); continue
                except: await asyncio.sleep(1)
        
        if not data: continue
        now = datetime.now(timezone.utc)
        grouped = {}
        for e in data:
            k = f"{e['item_id']}|{e['quality']}"
            grouped.setdefault(k, {})[e['city']] = e

        for k, c_d in grouped.items():
            i_id, q = k.split("|")
            # "Звідки": якщо режим "Усі міста", ігноруємо Блекмаркет як джерело
            srcs = [f_c] if f_c else [c for c in c_d if c != "Black Market"]
            
            for sc in srcs:
                if sc not in c_d: continue
                buy = c_d[sc].get('sell_price_min', 0)
                if buy <= 500 or buy > b_l: continue
                
                bd_str = c_d[sc]['sell_price_min_date']
                b_dt = datetime.fromisoformat(bd_str.split(".")[0].replace("Z","")).replace(tzinfo=timezone.utc)
                if (now-b_dt).total_seconds()/60 > 180: continue

                # "Куди": включає Блекмаркет
                targets = [t_c] if t_c else [c for c in c_d if c != sc]
                for tc in targets:
                    if tc not in c_d: continue
                    is_bm = (tc == "Black Market")
                    sk = 'buy_price_max_date' if is_bm else 'sell_price_min_date'
                    sell = c_d[tc].get('buy_price_max' if is_bm else 'sell_price_min', 0)
                    
                    if sell <= buy or (sell/buy) > 10: continue
                    
                    sd_str = c_d[tc].get(sk)
                    s_dt = datetime.fromisoformat(sd_str.split(".")[0].replace("Z","")).replace(tzinfo=timezone.utc)
                    if (now-s_dt).total_seconds()/60 > 180: continue
                    
                    p_n = int(sell * 0.895 - buy)
                    if p_n >= p_l:
                        if ext and ((now-b_dt).total_seconds()/60 > 30 or (now-s_dt).total_seconds()/60 > 30): continue
                        pre_res.append({'id':i_id,'q':int(q),'from':sc,'to':tc,'buy':buy,'sell':sell,
                                        'p_p':int(sell*0.935-buy),'p_n':p_n,'bd':bd_str,'sd':sd_str})

    # Обробка попиту та середньої ціни
    final_res = []
    # Сортуємо спочатку, щоб перевіряти ліквідність лише для топових за профітом
    pre_res.sort(key=lambda x: x['p_n'], reverse=True)
    
    for item in pre_res[:25]:
        vol, avg = await get_item_history(item['id'].split("@")[0], item['to'])
        profit_margin = item['p_n'] / item['buy']
        
        # Фільтр попиту: 0 продажів допустимі лише при адекватному профіті (<15%)
        if check_liq:
            if vol > 0 or profit_margin <= 0.15:
                item['vol'], item['avg'] = vol, avg
                final_res.append(item)
        else:
            item['vol'], item['avg'] = vol, avg
            final_res.append(item)
            
    return final_res[:15]
async def disp_res(msg, res, d):
    res.sort(key=lambda x: x['p_n'], reverse=True)
    show_liq = d.get("check_liq")
    messages, full_text = [], ""
    
    for idx, r in enumerate(res[:15], 1):
        b_id = r['id'].split("@")[0]; icon = get_item_icon(b_id)
        enc = r['id'].split("@")[1] if "@" in r['id'] else "0"; tier = b_id.split('_')[0][1:]
        name = items_data.get(b_id, {}).get("LocalizedNames", {}).get("RU-RU", b_id)
        name = re.sub(r'\s*\([^)]*\)', '', html.escape(name.upper()))
        for t in TRASH: name = name.replace(t, "")
        
        tbd, tsd = fmt_t(r.get('bd')), fmt_t(r.get('sd'))
        
        p_p_str, p_n_str = f"{r['p_p']:,}", f"{r['p_n']:,}"
        liq_part = f"Попит: {r.get('vol', 0)} шт/д"
        avg_part = f"Сер.ціна: {r.get('avg', 0):,}" if r.get('avg', 0) > 0 else ""

        item_block = (
            f"{idx}) {icon} <b>{name}</b> [{tier}.{enc}]\n"
            f"✨ {QUALITY_NAMES.get(r['q'], 'Обычное')}\n"
            f"📥 {CITY_EMOJIS[r['from']]} {r['buy']:,} | 🕒 {tbd}\n"
            f"📤 {CITY_EMOJIS[r['to']]} {r['sell']:,} | 🕒 {tsd}\n"
            f"<pre>"
            f"Прибуток:\n"
            f"👑 {p_p_str:<15} {liq_part}\n"
            f"💀 {p_n_str:<15} {avg_part}"
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
    if budget > 0 and mode: kb.append([KeyboardButton(text="🚀 Запустити сканер")])

    if searched:
        e_l = f"⚡ 30хв: {'ON' if d.get('extra') else 'OFF'}"
        liq_l = f"📊 Попит: {'ON' if d.get('check_liq') else 'OFF'}"
        kb.append([KeyboardButton(text=m_btn), KeyboardButton(text=e_l)])
        kb.append([KeyboardButton(text=liq_l), KeyboardButton(text="🧮 Калькулятор")])
        kb.append([KeyboardButton(text="💰 Бюджет"), KeyboardButton(text="🔄 Скинути")])
    else:
        kb.append([KeyboardButton(text="💰 Бюджет"), KeyboardButton(text=m_btn)])
        help_btn = [KeyboardButton(text="❓ Допомога")] if not (budget > 0 and mode) else []
        kb.append([KeyboardButton(text="🧮 Калькулятор")] + help_btn)

    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

@dp.message(Command("start"), StateFilter('*'))
async def cmd_start(m, state: FSMContext):
    await state.clear()
    welcome = (
        "👋 <b>Привіт! Я Albion Trade Bot.</b>\n\n"
        "Крок 1: Встанови <b>💰 Бюджет</b> (ліміт на покупку 1 од.).\n"
        "Крок 2: Обери <b>🗺 Режим</b> пошуку.\n\n"
        "<i>Бот автоматично ігнорує закупівлю на Блекмаркеті, але враховує його для продажу.</i>"
    )
    await m.answer(welcome, parse_mode=ParseMode.HTML, reply_markup=get_main_kb({}))

@dp.message(F.text == "🚀 Запустити сканер", StateFilter('*'))
async def main_search(m, state: FSMContext):
    d = await state.get_data()
    if not is_db_ready: return await m.answer("⏳ БД вантажиться...")
    await bot.send_chat_action(m.chat.id, ChatAction.TYPING)
    s_msg = await m.answer("🔍 Шукаю..."); res = await scan_logic(d, d.get('f_c'), d.get('t_c'))
    await safe_delete(s_msg)
    
    if not d.get("has_searched"): await state.update_data(has_searched=True)
    
    if not res: await m.answer("📭 Порожньо")
    else: await disp_res(m, res, d)
    await m.answer("✅ Готово!", reply_markup=get_main_kb(await state.get_data()))

@dp.message(F.text.startswith("Режим:") | (F.text == "🗺 Режим"), StateFilter('*'))
async def choose_mode(m, state):
    await m.answer("Оберіть режим:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎲 Всі міста", callback_data="set_mode_all")],
        [InlineKeyboardButton(text="📍 Шлях", callback_data="set_mode_custom")]
    ]))

@dp.callback_query(F.data.startswith("set_mode_"))
async def set_mode_cb(cb, state: FSMContext):
    m = cb.data.split("_")[2]; await state.update_data(mode=m); await cb.answer()
    d = await state.get_data()
    if m == "all": 
        await cb.message.answer("🌍 Режим: Всі міста!", reply_markup=get_main_kb(d))
        if d.get("buy_limit", 0) > 0: await cb.message.answer("✨ Вдалого пошуку!")
    else: 
        await state.set_state(BotState.picking_from)
        # При виборі "Звідки" - Блекмаркета немає
        await cb.message.answer("Звідки:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"{CITY_EMOJIS[c]} {c}", callback_data=f"city_{c}")] for c in CITIES if c != "Black Market"
        ]))

@dp.callback_query(F.data.startswith("city_"))
async def city_pick(cb, state: FSMContext):
    await cb.answer(); curr = await state.get_state(); c = cb.data.split("_")[1]
    if curr and "picking_from" in str(curr):
        await state.update_data(f_c=c); await state.set_state(BotState.picking_to)
        # При виборі "Куди" - Блекмаркет є
        await cb.message.edit_text(f"З: {c}. Куди:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"{CITY_EMOJIS[ci]} {ci}", callback_data=f"city_{ci}")] for ci in CITIES if ci != c
        ]))
    elif curr and "picking_to" in str(curr):
        await state.update_data(t_c=c, mode="custom"); await state.set_state(None)
        d = await state.get_data()
        await cb.message.answer(f"📍 Шлях до {c} активовано!", reply_markup=get_main_kb(d))
        if d.get("buy_limit", 0) > 0: await cb.message.answer("✨ Вдалого пошуку!")

@dp.message(F.text == "🧮 Калькулятор", StateFilter('*'))
async def calc_start(m, state: FSMContext):
    await state.set_state(BotState.calc_count)
    await m.answer("📦 Введи кількість предметів:", reply_markup=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="❌ Скасувати")]], resize_keyboard=True))

@dp.message(F.text == "🔄 Скинути", StateFilter('*'))
async def reset_confirm(m, state: FSMContext):
    await state.set_state(BotState.confirm_reset)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Так", callback_data="reset_yes"), InlineKeyboardButton(text="❌ Ні", callback_data="reset_no")]
    ])
    await m.answer("⚠️ <b>Це скине всі введені дані!</b>\nПродовжити?", parse_mode=ParseMode.HTML, reply_markup=kb)

@dp.callback_query(F.data.startswith("reset_"), StateFilter(BotState.confirm_reset))
async def reset_action(cb, state: FSMContext):
    if cb.data == "reset_yes":
        await state.clear(); await cb.message.edit_text("🔄 Дані скинуто.")
        await cb.message.answer("Почнемо спочатку?", reply_markup=get_main_kb({}))
    else: await cb.message.edit_text("🚫 Скасовано.")
    await state.set_state(None); await cb.answer()

@dp.message(StateFilter(BotState.calc_count, BotState.calc_buy, BotState.calc_sell, BotState.waiting_for_buy_limit, BotState.waiting_for_profit_limit))
async def numeric_handler(m, state: FSMContext):
    if m.text == "❌ Скасувати":
        await state.set_state(None); return await m.answer("Скасовано", reply_markup=get_main_kb(await state.get_data()))
    try:
        v = int(m.text.replace(" ","")); curr = await state.get_state()
        if "buy_limit" in str(curr):
            await state.update_data(buy_limit=v); await state.set_state(None)
            d = await state.get_data(); await m.answer(f"✅ Бюджет: {v:,}", reply_markup=get_main_kb(d))
            if d.get("mode"): await m.answer("✨ Вдалого пошуку!")
        elif "profit_limit" in str(curr):
            await state.update_data(profit_limit=v); await state.set_state(None)
            await m.answer(f"✅ Профіт: {v:,}", reply_markup=get_main_kb(await state.get_data()))
        elif "calc_count" in str(curr):
            await state.update_data(c=v); await state.set_state(BotState.calc_buy); await m.answer("📥 Ціна КУПІВЛІ:")
        elif "calc_buy" in str(curr):
            await state.update_data(b=v); await state.set_state(BotState.calc_sell); await m.answer("📤 Ціна ПРОДАЖУ:")
        elif "calc_sell" in str(curr):
            d = await state.get_data(); await state.set_state(None)
            p_p, p_n = int((v*0.935)-d['b'])*d['c'], int((v*0.895)-d['b'])*d['c']
            await m.answer(f"📊 ({d['c']} шт):\n👑 Пр: <b>{p_p:,}</b>\n💀 Пр: <b>{p_n:,}</b>", reply_markup=get_main_kb(d), parse_mode=ParseMode.HTML)
    except: await m.answer("❌ Тільки числа!")

@dp.message(F.text.regexp(r"⚡ 30хв:|📊 Попит:"), StateFilter('*'))
async def toggles(m, state: FSMContext):
    d = await state.get_data()
    if "⚡ 30хв" in m.text:
        val = not d.get("extra", False); await state.update_data(extra=val)
        msg = f"⚡ Фільтр 30хв: {'УВІМКНЕНО' if val else 'ВИМКНЕНО'}"
    else:
        val = not d.get("check_liq", False); await state.update_data(check_liq=val)
        msg = f"📊 Попит: {'УВІМКНЕНО' if val else 'ВИМКНЕНО'}"
    await m.answer(msg, reply_markup=get_main_kb(await state.get_data()))

@dp.callback_query(F.data.startswith("set_limit_"))
async def set_limit_cb(cb, state: FSMContext):
    t = cb.data.split("_")[2]; await state.set_state(BotState.waiting_for_buy_limit if t=="buy" else BotState.waiting_for_profit_limit); await cb.answer()
    await cb.message.answer(f"Введи {'бюджет' if t=='buy' else 'мін. профіт'}:", reply_markup=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="❌ Скасувати")]], resize_keyboard=True))

async def main():
    global http_session
    http_session = aiohttp.ClientSession(headers=HEADERS)
    await bot.delete_webhook(drop_pending_updates=True)
    asyncio.create_task(download_items())
    try: await dp.start_polling(bot)
    finally:
        await http_session.close(); await bot.close()

if __name__ == "__main__":
    try: asyncio.run(main())
    except: pass
