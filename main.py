import os, json, aiohttp, asyncio, re, logging, time
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

# КЕШ ІСТОРІЇ (щоб не перевантажувати API)
history_cache = {} # { "item_id|city": {"volume": 10, "time": datetime} }
CACHE_TTL = 3600 # 1 година

CITIES = ["Bridgewatch", "Martlock", "Lymhurst", "Thetford", "Fort Sterling", "Caerleon", "Brecilien", "Black Market"]
CITY_EMOJIS = {"Lymhurst":"🟢","Martlock":"🔵","Caerleon":"⚫","Thetford":"🟣","Bridgewatch":"🟠","Fort Sterling":"⚪","Brecilien":"🌸","Black Market":"💀"}
QUALITY_NAMES = {1:"Обычное", 2:"Хорошее", 3:"Выдающееся", 4:"Отличное", 5:"Шедевр"}
TRASH = ["Знаток ","Мастер ","Великий мастер ","Старейшина ","Ученик ","Новичок "]
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

class BotState(StatesGroup):
    waiting_for_buy_limit = State(); waiting_for_profit_limit = State()
    picking_from = State(); picking_to = State()
    calc_count = State(); calc_buy = State(); calc_sell = State()

# ================= АНАЛІЗ ЛІКВІДНОСТІ =================
async def get_item_liquidity(item_id, city):
    """ Отримує об'єм продажів предмета за добу """
    global history_cache
    cache_key = f"{item_id}|{city}"
    now = datetime.now(UTC)
    
    if cache_key in history_cache:
        if (now - history_cache[cache_key]['time']).total_seconds() < CACHE_TTL:
            return history_cache[cache_key]['volume']
            
    url = f"https://europe.albion-online-data.com/api/v2/stats/history/{item_id}?locations={city}&time-series=1"
    try:
        async with http_session.get(url, timeout=10) as resp:
            if resp.status != 200: return 0
            data = await resp.json()
            if not data or not data[0].get('data'): return 0
            
            volume = data[0]['data'][-1].get('item_count', 0)
            history_cache[cache_key] = {'volume': volume, 'time': now}
            return volume
    except:
        return 0

# ================= ФОНОВІ ФУНКЦІЇ =================
async def download_items():
    global items_data, is_db_ready, http_session
    try:
        async with http_session.get("https://raw.githubusercontent.com/ao-data/ao-bin-dumps/master/formatted/items.json", timeout=60) as r:
            if r.status == 200:
                data = await r.json(content_type=None)
                allowed = ["weapon","armor","plate","leather","cloth","bag","cape","potion","meal","mount","tool","shapeshifter","offhand"]
                items_data = {i["UniqueName"]: i for i in data if i.get("UniqueName","").startswith(("T4_","T5_","T6_","T7_","T8_")) and any(x in i.get("UniqueName","").lower() for x in allowed)}
                logger.info(f"✅ БД завантажена: {len(items_data)} шт.")
    except: logger.exception("Помилка БД:")
    finally: is_db_ready = True

def fmt_t(s):
    if not s or s.startswith("0001"): return "???"
    try:
        dt = datetime.fromisoformat(s.split(".")[0].replace("Z","")).replace(tzinfo=UTC)
        m = int((datetime.now(UTC)-dt).total_seconds()/60)
        return f"{m}м" if m < 60 else f"{m//60}г"
    except: return "???"

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

async def scan_logic(d, f_c=None, t_c=None):
    if not items_data: return [] 
    res = []; b_l = d.get("buy_limit", 0); p_l = d.get("profit_limit", 4000); ext = d.get("extra", False)
    check_liq = d.get("check_liq", False)
    
    i_list = list(items_data.keys()); cities = [f_c, t_c] if f_c and t_c else CITIES
    
    pre_res = []
    for i in range(0, len(i_list), 50):
        url = f"https://europe.albion-online-data.com/api/v2/stats/prices/{','.join(i_list[i:i+50])}?locations={','.join(cities)}"
        try:
            async with http_session.get(url, timeout=20) as resp:
                data = await resp.json() if resp.status == 200 else None
        except: data = None
            
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
                b_dt = datetime.fromisoformat(c_d[sc]['sell_price_min_date'].split(".")[0].replace("Z","")).replace(tzinfo=UTC)
                if (now-b_dt).total_seconds()/60 > 180: continue
                targets = [t_c] if t_c else [c for c in c_d if c!=sc]
                for tc in targets:
                    if tc not in c_d: continue
                    sk = 'buy_price_max_date' if tc=="Black Market" else 'sell_price_min_date'
                    sell = c_d[tc].get('buy_price_max' if tc=="Black Market" else 'sell_price_min', 0)
                    if sell <= buy or (sell/buy) > 10: continue
                    s_dt = datetime.fromisoformat(c_d[tc].get(sk).split(".")[0].replace("Z","")).replace(tzinfo=UTC)
                    if (now-s_dt).total_seconds()/60 > 180: continue
                    p_n = int(sell*0.895-buy)
                    if p_n >= p_l:
                        if ext and ((now-b_dt).total_seconds()/60 > 30 or (now-s_dt).total_seconds()/60 > 30): continue
                        pre_res.append({'id':i_id,'q':int(q),'from':sc,'to':tc,'buy':buy,'sell':sell,'p_p':int(sell*0.935-buy),'p_n':p_n,'bd':c_d[sc]['sell_price_min_date'],'sd':c_d[tc].get(sk)})
        if i % 300 == 0: await asyncio.sleep(0.1)

    # ГІБРИДНА ПЕРЕВІРКА ЛІКВІДНОСТІ (Тільки ТОП-30)
    if check_liq and pre_res:
        pre_res.sort(key=lambda x: x['p_n'], reverse=True)
        for item in pre_res[:30]:
            vol = await get_item_liquidity(item['id'].split("@")[0], item['to'])
            if vol >= 3: # Мінімум 3 продажі на добу
                item['vol'] = vol
                res.append(item)
        return res
    
    return pre_res
# ================= КЛАВІАТУРИ =================
def get_start_kb():
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="❓ Допомога"), KeyboardButton(text="💰 Налаштувати бюджет")]], resize_keyboard=True)

def get_main_kb(d):
    m = d.get("mode")
    if not m:
        return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="❓ Допомога"), KeyboardButton(text="💰 Налаштувати бюджет")], [KeyboardButton(text="🗺 Обрати режим")]], resize_keyboard=True)
    m_l = "🌍 Охоплення: Всі міста" if m == "all" else "📍 Маршрут: Шлях"
    e_l = "🚫 Вимкнути фільтр 30хв" if d.get("extra") else "⚡ Свіжі ціни (30хв)"
    liq_l = "📊 Попит: ON" if d.get("check_liq") else "📊 Попит: OFF"
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="🚀 Запустити сканер")],
        [KeyboardButton(text=m_l), KeyboardButton(text=e_l)],
        [KeyboardButton(text=liq_l), KeyboardButton(text="🧮 Калькулятор")],
        [KeyboardButton(text="💰 Налаштувати бюджет"), KeyboardButton(text="🔄 Перезавантаження")],
        [KeyboardButton(text="❓ Допомога")]
    ], resize_keyboard=True)

def get_mode_inline(): 
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🎲 Всі міста", callback_data="set_mode_all")], [InlineKeyboardButton(text="📍 Конкретний шлях", callback_data="set_mode_custom")]])

# ================= ОБРОБНИКИ =================
@dp.message(F.text == "🚀 Запустити сканер", StateFilter('*'))
async def main_search(m, state: FSMContext):
    u_id, now = m.from_user.id, datetime.now(UTC); d = await state.get_data()
    if not is_db_ready: return await m.answer("⏳ База вантажиться...")
    b = d.get("buy_limit", 0)
    if b <= 0: 
        req_kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="💰 Налаштувати бюджет")], [KeyboardButton(text="❌ Скасувати")]], resize_keyboard=True)
        return await m.answer("⚠️ Встанови бюджет!", reply_markup=req_kb)
    if not d.get("mode"): return await m.answer("🗺️ Обери режим!", reply_markup=get_mode_inline())

    async with active_scans_lock:
        if u_id in active_scans: return await m.answer("⚠️ Зачекай...")
        if u_id in user_cooldowns and (now - user_cooldowns[u_id]).total_seconds() < 15: return await m.answer("⏳ Кулдаун!")
        active_scans.add(u_id)
            
    try:
        s_msg = await m.answer("🔍 Шукаю вигідні та живі угоди...", reply_markup=ReplyKeyboardRemove())
        res = await scan_logic(d, d.get('f_c'), d.get('t_c'))
        await s_msg.delete()
        if not res: await m.answer("📭 Нічого не знайдено за твоїми лімітами.", reply_markup=get_main_kb(d))
        else:
            await disp_res(m, res, d.get("check_liq"))
            await m.answer(f"✅ Знайдено: {len(res)}", reply_markup=get_main_kb(d))
    finally:
        active_scans.discard(u_id); user_cooldowns[u_id] = datetime.now(UTC)

@dp.message(F.text.in_(["📊 Попит: ON", "📊 Попит: OFF"]), StateFilter('*'))
async def toggle_liq(m, state: FSMContext):
    d = await state.get_data(); val = not d.get("check_liq", False)
    await state.update_data(check_liq=val); d = await state.get_data()
    txt = "✅ <b>Аналіз попиту УВІМКНУТО</b>. Бот приховає мертві товари." if val else "❌ <b>Аналіз попиту ВИМКНУТО</b>. Показую все за ціною."
    await m.answer(txt, parse_mode=ParseMode.HTML, reply_markup=get_main_kb(d))

async def disp_res(msg, res, show_vol):
    res.sort(key=lambda x: x['p_n'], reverse=True)
    messages = []; full_text = ""
    for idx, r in enumerate(res[:15], 1):
        b_id = r['id'].split("@")[0]; tier = b_id.split('_')[0][1:]; icon = get_item_icon(b_id)
        name = items_data.get(b_id, {}).get("LocalizedNames", {}).get("RU-RU", b_id)
        name = re.sub(r'\s*\([^)]*\)', '', name)
        for t in TRASH: name = name.replace(t, "")
        
        liq_txt = f"📦 Попит: <b>{r.get('vol', '?')} шт/день</b>\n" if show_vol else ""
        
        item_text = (f"{idx}) {icon} <b>{name.upper()}</b> [{tier}.{r['id'].split('@')[1] if '@' in r['id'] else '0'}]\n"
                     f"✨ {QUALITY_NAMES.get(r['q'], 'Обычное')}\n"
                     f"📥 {CITY_EMOJIS[r['from']]} {r['buy']:,} ({fmt_t(r['bd'])})\n"
                     f"📤 {CITY_EMOJIS[r['to']]} {r['sell']:,} ({fmt_t(r['sd'])})\n"
                     f"{liq_txt}💵 Пр: 👑 <b>{r['p_p']:,}</b> | 💀 <b>{r['p_n']:,}</b>\n\n")
        
        if len(full_text) + len(item_text) > 3900: messages.append(full_text); full_text = item_text
        else: full_text += item_text
    if full_text: messages.append(full_text)
    for t in messages: await msg.answer(t, parse_mode=ParseMode.HTML)

# ІНШІ СТАНДАРТНІ ОБРОБНИКИ (СКОРОЧЕНО)
@dp.message(F.text == "❌ Скасувати", StateFilter('*'))
async def cancel_limit(m, state: FSMContext):
    await state.set_state(None); await m.answer("🚫 Скасовано.", reply_markup=get_main_kb(await state.get_data()))

@dp.message(F.text == "💰 Налаштувати бюджет", StateFilter('*'))
async def limit_menu(m, state: FSMContext):
    d = await state.get_data(); b, p = d.get("buy_limit", 0), d.get("profit_limit", 4000)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=f"💰 Бюджет ({b:,})", callback_data="set_limit_buy")], [InlineKeyboardButton(text=f"📈 Профіт ({p:,})", callback_data="set_limit_profit")]])
    await m.answer("⚙️ Бюджет:", reply_markup=kb)

@dp.callback_query(F.data.startswith("set_limit_"))
async def set_limit_cb(cb, state: FSMContext):
    t = cb.data.split("_")[2]; await state.set_state(BotState.waiting_for_buy_limit if t=="buy" else BotState.waiting_for_profit_limit)
    await cb.message.answer(f"Введи {'бюджет' if t=='buy' else 'мінімальний профіт'}:", reply_markup=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="❌ Скасувати")]], resize_keyboard=True))

@dp.message(StateFilter(BotState.waiting_for_buy_limit, BotState.waiting_for_profit_limit))
async def h_limits(m, state: FSMContext):
    try:
        v = int(m.text.replace(" ","")); curr = await state.get_state()
        if "buy" in str(curr): await state.update_data(buy_limit=v)
        else: await state.update_data(profit_limit=v)
        await state.set_state(None); await m.answer(f"✅ Збережено: {v:,}", reply_markup=get_main_kb(await state.get_data()))
    except: await m.answer("Введи число!")

@dp.callback_query(F.data.startswith("set_mode_"))
async def set_mode_cb(cb, state: FSMContext):
    m = cb.data.split("_")[2]; await state.update_data(mode=m)
    if m == "all": await cb.message.answer("🌍 Всі міста!", reply_markup=get_main_kb(await state.get_data()))
    else: await state.set_state(BotState.picking_from); await cb.message.answer("Обери пункт А:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=f"{CITY_EMOJIS[c]} {c}", callback_data=f"city_{c}")] for c in CITIES if c!="Black Market"]))

@dp.callback_query(StateFilter(BotState.picking_from, BotState.picking_to), F.data.startswith("city_"))
async def city_pick(cb, state: FSMContext):
    c = cb.data.split("_")[1]; curr = await state.get_state()
    if "from" in str(curr):
        await state.update_data(f_c=c); await state.set_state(BotState.picking_to)
        await cb.message.edit_text(f"Пункт А: {c}. Обери пункт Б:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=f"{CITY_EMOJIS[ci]} {ci}", callback_data=f"city_{ci}")] for ci in CITIES if ci!=c and ci!="Black Market"]))
    else:
        await state.update_data(t_c=c, mode="custom"); await state.set_state(None)
        await cb.message.answer(f"✅ Маршрут готовий!", reply_markup=get_main_kb(await state.get_data()))

@dp.message(F.text == "🧮 Калькулятор", StateFilter('*'))
async def calc_start(m, state: FSMContext):
    await state.set_state(BotState.calc_count); await m.answer("📦 Кількість:", reply_markup=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="❌ Скасувати")]], resize_keyboard=True))

@dp.message(StateFilter(BotState.calc_count, BotState.calc_buy, BotState.calc_sell))
async def h_calc(m, state: FSMContext):
    try:
        v = int(m.text.replace(" ","")); curr = await state.get_state()
        if "count" in str(curr): await state.update_data(c=v); await state.set_state(BotState.calc_buy); await m.answer("Ціна КУПІВЛІ:")
        elif "buy" in str(curr): await state.update_data(b=v); await state.set_state(BotState.calc_sell); await m.answer("Ціна ПРОДАЖУ:")
        else:
            d = await state.get_data(); await state.set_state(None)
            p_p, p_n = int((v*0.935)-d['b'])*d['c'], int((v*0.895)-d['b'])*d['c']
            await m.answer(f"📊 Пр: 👑 {p_p:,} | 💀 {p_n:,}", reply_markup=get_main_kb(d))
    except: await m.answer("Введи число!")

@dp.message(F.text.in_(["⚡ Свіжі ціни (30хв)", "🚫 Вимкнути фільтр 30хв"]), StateFilter('*'))
async def toggle_extra(m, state: FSMContext):
    d = await state.get_data(); val = not d.get("extra", False); await state.update_data(extra=val)
    await m.answer(f"Фільтр 30хв: {'Увімкнено' if val else 'Вимкнено'}", reply_markup=get_main_kb(await state.get_data()))

@dp.message(F.text == "🔄 Перезавантаження", StateFilter('*'))
async def btn_res(m, state: FSMContext):
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Скинути", callback_data="conf_res"), InlineKeyboardButton(text="❌ Ні", callback_data="cancel_res")]])
    await m.answer("Скинути дані?", reply_markup=kb)

@dp.callback_query(F.data == "conf_res")
async def conf_res(cb, state: FSMContext): await state.clear(); await cb.message.answer("🔄 Скинуто!", reply_markup=get_start_kb())

@dp.callback_query(F.data == "cancel_res")
async def cancel_res(cb): await cb.message.delete()

@dp.message(Command("start"), StateFilter('*'))
async def cmd_start(m, state: FSMContext): await state.clear(); await m.answer("👋 Привіт! Почнемо?", reply_markup=get_start_kb())

@dp.message(F.text == "❓ Допомога", StateFilter('*'))
async def cmd_help(m, state: FSMContext):
    await m.answer("1. Бюджет\n2. Режим\n3. Сканер\nПопит ON - фільтрує мертві речі.", reply_markup=get_main_kb(await state.get_data()))

async def main():
    global http_session; await set_bot_commands()
    http_session = aiohttp.ClientSession(headers=HEADERS)
    asyncio.create_task(download_items())
    await dp.start_polling(bot)
    await http_session.close()

if __name__ == "__main__": asyncio.run(main())
