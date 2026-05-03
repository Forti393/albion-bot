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

items_data = {}; is_db_ready = False; http_session = None 
scan_semaphore = asyncio.Semaphore(3); active_scans_lock = asyncio.Lock() 
user_cooldowns = {}; active_scans = set(); history_cache = {}
CACHE_TTL = 3600 

CITIES = ["Bridgewatch", "Martlock", "Lymhurst", "Thetford", "Fort Sterling", "Caerleon", "Brecilien", "Black Market"]
CITY_EMOJIS = {"Lymhurst":"🟢","Martlock":"🔵","Caerleon":"⚫","Thetford":"🟣","Bridgewatch":"🟠","Fort Sterling":"⚪","Brecilien":"🌸","Black Market":"💀"}
QUALITY_NAMES = {1:"Обычное", 2:"Хорошее", 3:"Выдающееся", 4:"Отличное", 5:"Шедевр"}
TRASH = ["Знаток ","Мастер ","Великий мастер ","Старейшина ","Ученик ","Новичок "]
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

class BotState(StatesGroup):
    waiting_for_buy_limit = State(); waiting_for_profit_limit = State()
    picking_from = State(); picking_to = State()
    calc_count = State(); calc_buy = State(); calc_sell = State()

# ================= СЛУЖБОВІ ФУНКЦІЇ =================
async def safe_delete(msg):
    try: await msg.delete()
    except: pass

async def cleanup_cooldowns():
    while True:
        await asyncio.sleep(600)
        try:
            now = datetime.now(UTC)
            expired = [uid for uid, dt in user_cooldowns.items() if (now - dt).total_seconds() > 3600]
            for uid in expired: del user_cooldowns[uid]
        except: pass

async def set_bot_commands():
    cmds = [types.BotCommand(command="start", description="🚀 Головне меню"), types.BotCommand(command="help", description="📖 Допомога")]
    await bot.set_my_commands(cmds)

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
    cache_key, now = f"{item_id}|{city}", datetime.now(UTC)
    if cache_key in history_cache and (now - history_cache[cache_key]['time']).total_seconds() < CACHE_TTL:
        return history_cache[cache_key]['volume']
    url = f"https://europe.albion-online-data.com/api/v2/stats/history/{item_id}?locations={city}&time-series=1"
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
    global items_data, is_db_ready
    try:
        async with http_session.get("https://raw.githubusercontent.com/ao-data/ao-bin-dumps/master/formatted/items.json", timeout=60) as r:
            if r.status == 200:
                data = await r.json(content_type=None)
                allowed = ["weapon","armor","plate","leather","cloth","bag","cape","potion","meal","mount","tool","offhand"]
                items_data = {i["UniqueName"]: i for i in data if i.get("UniqueName","").startswith(("T4_","T5_","T6_","T7_","T8_")) and any(x in i.get("UniqueName","").lower() for x in allowed)}
                logger.info("✅ БД завантажена")
    except: logger.exception("Помилка БД")
    finally: is_db_ready = True
async def scan_logic(d, f_c=None, t_c=None):
    if not items_data: return [] 
    pre_res = []; b_l, p_l = d.get("buy_limit", 0), d.get("profit_limit", 4000)
    ext, check_liq = d.get("extra", False), d.get("check_liq", False)
    i_list = list(items_data.keys()); cities = [f_c, t_c] if f_c and t_c else CITIES
    
    for i in range(0, len(i_list), 50):
        url = f"https://europe.albion-online-data.com/api/v2/stats/prices/{','.join(i_list[i:i+50])}?locations={','.join(cities)}"
        data = None
        for attempt in range(3):
            try:
                async with http_session.get(url, timeout=20) as resp:
                    if resp.status == 429: await asyncio.sleep(1); continue
                    if resp.status == 200: data = await resp.json(); break
            except: await asyncio.sleep(0.5)
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

    if check_liq and pre_res:
        pre_res.sort(key=lambda x: x['p_n'], reverse=True)
        res = []
        for item in pre_res[:30]:
            vol = await get_item_liquidity(item['id'].split("@")[0], item['to'])
            if vol > 0: item['vol'] = vol; res.append(item)
        return res
    return pre_res

# ================= МЕНЮ ТА КНОПКИ =================
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
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎲 Всі міста", callback_data="set_mode_all")],
        [InlineKeyboardButton(text="📍 Конкретний шлях", callback_data="set_mode_custom")]
    ])

def fmt_t(s):
    try:
        dt = datetime.fromisoformat(s.split(".")[0].replace("Z","")).replace(tzinfo=UTC)
        m = int((datetime.now(UTC)-dt).total_seconds()/60)
        return f"{m}м" if m < 60 else f"{m//60}г"
    except: return "???"

@dp.message(F.text == "🚀 Запустити сканер", StateFilter('*'))
async def main_search(m, state: FSMContext):
    u_id, now = m.from_user.id, datetime.now(UTC); d = await state.get_data(); is_admin = (u_id == ADMIN_ID)
    if not is_db_ready: return await m.answer("⏳ База вантажиться...")
    if d.get("buy_limit", 0) <= 0: 
        return await m.answer("⚠️ Встанови бюджет!", reply_markup=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="💰 Налаштувати бюджет"), KeyboardButton(text="❌ Скасувати")]], resize_keyboard=True))
    if not d.get("mode"): return await m.answer("🗺️ Обери режим!", reply_markup=get_mode_inline())

    if not is_admin:
        async with active_scans_lock:
            if u_id in active_scans: return await m.answer("⚠️ Зачекай...")
            if u_id in user_cooldowns and (now - user_cooldowns[u_id]).total_seconds() < 25: return await m.answer("⏳ Кулдаун 25с!")
            active_scans.add(u_id)
    
    await bot.send_chat_action(chat_id=m.chat.id, action=ChatAction.TYPING)
    try:
        s_msg = await m.answer("🔍 Шукаю вигідні угоди...", reply_markup=ReplyKeyboardRemove())
        res = await scan_logic(d, d.get('f_c'), d.get('t_c'))
        await safe_delete(s_msg)
        if not res: await m.answer("📭 Нічого не знайдено.", reply_markup=get_main_kb(d))
        else:
            await disp_res(m, res, d.get("check_liq"))
            await m.answer(f"✅ Знайдено: {len(res)}", reply_markup=get_main_kb(d))
    finally: active_scans.discard(u_id); user_cooldowns[u_id] = datetime.now(UTC)

async def disp_res(msg, res, show_vol):
    res.sort(key=lambda x: x['p_n'], reverse=True)
    messages, full_text = [], ""
    for idx, r in enumerate(res[:15], 1):
        b_id = r['id'].split("@")[0]; icon = get_item_icon(b_id)
        enc = r['id'].split("@")[1] if "@" in r['id'] else "0"
        tier = b_id.split('_')[0][1:]
        name = items_data.get(b_id, {}).get("LocalizedNames", {}).get("RU-RU", b_id)
        name = re.sub(r'\s*\([^)]*\)', '', name)
        for t in TRASH: name = name.replace(t, "")
        
        liq = f"📦 Попит: <b>{r.get('vol', '?')} шт/д</b>\n" if show_vol else ""
        item_text = (f"{idx}) {icon} <b>{name.upper()}</b> 🔸 <b>[{tier}.{enc}]</b>\n"
                     f"✨ {QUALITY_NAMES.get(r['q'], 'Обычное')}\n"
                     f"📥 {CITY_EMOJIS[r['from']]} {r['buy']:,} ({fmt_t(r['bd'])})\n"
                     f"📤 {CITY_EMOJIS[r['to']]} {r['sell']:,} ({fmt_t(r['sd'])})\n"
                     f"{liq}💵 Пр: 👑 <b>{r['p_p']:,}</b> | 💀 <b>{r['p_n']:,}</b>\n\n")
        
        if len(full_text) + len(item_text) > 3900: messages.append(full_text); full_text = item_text
        else: full_text += item_text
    if full_text: messages.append(full_text)
    for t in messages: await msg.answer(t, parse_mode=ParseMode.HTML)

@dp.message(F.text == "❓ Допомога", StateFilter('*'))
@dp.message(Command("help"), StateFilter('*'))
async def cmd_help(m, state: FSMContext):
    text = (
        "📖 <b>Як користуватися ботом:</b>\n\n"
        "🚀 <b>Запустити сканер</b> — знаходить вигідні угоди\n"
        "💰 <b>Налаштувати бюджет</b> — встанови ліміти\n"
        "🗺 <b>Режим</b> — всі міста або маршрут\n"
        "⚡ <b>Свіжі ціни</b> — угоди до 30 хв\n"
        "📊 <b>Попит ON</b> — фільтр живих товарів\n"
        "🧮 <b>Калькулятор</b> — рахує прибуток\n"
        "🔄 <b>Перезавантаження</b> — скинути дані"
    )
    await m.answer(text, parse_mode=ParseMode.HTML, reply_markup=get_main_kb(await state.get_data()))

@dp.message(F.text == "❌ Скасувати", StateFilter('*'))
async def cancel_limit(m, state: FSMContext): await state.set_state(None); await m.answer("🚫 Скасовано.", reply_markup=get_main_kb(await state.get_data()))

@dp.message(F.text.in_(["📊 Попит: ON", "📊 Попит: OFF"]), StateFilter('*'))
async def toggle_liq(m, state: FSMContext):
    d = await state.get_data(); val = not d.get("check_liq", False); await state.update_data(check_liq=val)
    await m.answer(f"{'✅ Попит УВІМКНЕНО' if val else '❌ Попит ВИМКНЕНО'}", reply_markup=get_main_kb(await state.get_data()))

@dp.message(F.text == "💰 Налаштувати бюджет", StateFilter('*'))
async def limit_menu(m, state: FSMContext):
    d = await state.get_data(); b, p = d.get("buy_limit", 0), d.get("profit_limit", 4000)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💰 Бюджет", callback_data="set_limit_buy"), InlineKeyboardButton(text="📈 Профіт", callback_data="set_limit_profit")]])
    await m.answer(f"⚙️ Бюджет: {b:,}\n📈 Профіт: {p:,}", reply_markup=kb)

@dp.callback_query(F.data.startswith("set_limit_"))
async def set_limit_cb(cb, state: FSMContext):
    t = cb.data.split("_")[2]; await state.set_state(BotState.waiting_for_buy_limit if t=="buy" else BotState.waiting_for_profit_limit)
    await cb.message.answer(f"Введи {'бюджет' if t=='buy' else 'мін. профіт'}:", reply_markup=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="❌ Скасувати")]], resize_keyboard=True))

@dp.message(StateFilter(BotState.waiting_for_buy_limit, BotState.waiting_for_profit_limit))
async def h_limits(m, state: FSMContext):
    try:
        v = int(m.text.replace(" ","")); curr = await state.get_state()
        if v <= 0: return await m.answer("❌ Введи число > 0")
        if "buy" in str(curr): await state.update_data(buy_limit=v)
        else: await state.update_data(profit_limit=v)
        await state.set_state(None); await m.answer(f"✅ Збережено: {v:,}", reply_markup=get_main_kb(await state.get_data()))
    except: await m.answer("❌ Тільки цифри!")

@dp.callback_query(F.data.startswith("set_mode_"))
async def set_mode_cb(cb, state: FSMContext):
    m = cb.data.split("_")[2]; await state.update_data(mode=m)
    if m == "all": await cb.message.answer("🌍 Всі міста!", reply_markup=get_main_kb(await state.get_data()))
    else:
        await state.set_state(BotState.picking_from)
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=f"{CITY_EMOJIS[c]} {c}", callback_data=f"city_{c}")] for c in CITIES if c!="Black Market"])
        await cb.message.answer("Звідки:", reply_markup=kb)

@dp.callback_query(StateFilter(BotState.picking_from, BotState.picking_to), F.data.startswith("city_"))
async def city_pick(cb, state: FSMContext):
    c = cb.data.split("_")[1]; curr = await state.get_state()
    if "from" in str(curr):
        await state.update_data(f_c=c); await state.set_state(BotState.picking_to)
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=f"{CITY_EMOJIS[ci]} {ci}", callback_data=f"city_{ci}")] for ci in CITIES if ci!=c and ci!="Black Market"])
        await cb.message.edit_text(f"З: {c}. Куди:", reply_markup=kb)
    else:
        await state.update_data(t_c=c, mode="custom"); await state.set_state(None); await cb.message.answer("✅ Готово!", reply_markup=get_main_kb(await state.get_data()))

@dp.message(F.text == "🧮 Калькулятор", StateFilter('*'))
async def calc_start(m, state: FSMContext):
    await state.set_state(BotState.calc_count); await m.answer("📦 Кількість:", reply_markup=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="❌ Скасувати")]], resize_keyboard=True))

@dp.message(StateFilter(BotState.calc_count, BotState.calc_buy, BotState.calc_sell))
async def h_calc(m, state: FSMContext):
    try:
        v = int(m.text.replace(" ","")); curr = await state.get_state()
        if "count" in str(curr): await state.update_data(c=v); await state.set_state(BotState.calc_buy); await m.answer("Купівля:")
        elif "buy" in str(curr): await state.update_data(b=v); await state.set_state(BotState.calc_sell); await m.answer("Продаж:")
        else:
            d = await state.get_data(); await state.set_state(None); p_p, p_n = int((v*0.935)-d['b'])*d['c'], int((v*0.895)-d['b'])*d['c']
            await m.answer(f"📊 Пр: 👑 {p_p:,} | 💀 {p_n:,}", reply_markup=get_main_kb(d))
    except: await m.answer("❌ Тільки цифри!")

@dp.message(F.text == "🔄 Перезавантаження", StateFilter('*'))
async def btn_res(m, state: FSMContext): await m.answer("Скинути все?", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Так", callback_data="conf_res"), InlineKeyboardButton(text="❌ Ні", callback_data="cancel_res")]]))

@dp.callback_query(F.data == "conf_res")
async def conf_res(cb, state: FSMContext): await state.clear(); await cb.message.answer("🔄 Скинуто!", reply_markup=get_start_kb())

@dp.callback_query(F.data == "cancel_res")
async def cancel_res(cb): await cb.message.delete()

@dp.message(Command("start"), StateFilter('*'))
async def cmd_start(m, state: FSMContext): await state.clear(); await m.answer("👋 Бот готовий!", reply_markup=get_start_kb())

async def main():
    global http_session; http_session = aiohttp.ClientSession(headers=HEADERS)
    await set_bot_commands(); asyncio.create_task(download_items()); asyncio.create_task(cleanup_cooldowns())
    await dp.start_polling(bot); await http_session.close()

if __name__ == "__main__": asyncio.run(main())
