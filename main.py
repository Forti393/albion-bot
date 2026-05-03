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
http_session = None # Глобальна сесія для швидких запитів
scan_semaphore = asyncio.Semaphore(3) 
active_scans_lock = asyncio.Lock() 
user_cooldowns = {}; active_scans = set() 

CITIES = ["Bridgewatch", "Martlock", "Lymhurst", "Thetford", "Fort Sterling", "Caerleon", "Brecilien", "Black Market"]
CITY_EMOJIS = {"Lymhurst":"🟢","Martlock":"🔵","Caerleon":"⚫","Thetford":"🟣","Bridgewatch":"🟠","Fort Sterling":"⚪","Brecilien":"🌸","Black Market":"💀"}
QUALITY_NAMES = {1:"Обычное", 2:"Хорошее", 3:"Выдающееся", 4:"Отличное", 5:"Шедевр"}
TRASH = ["Знаток ","Мастер ","Великий мастер ","Старейшина ","Ученик ","Новичок "]
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

class BotState(StatesGroup):
    waiting_for_buy_limit = State(); waiting_for_profit_limit = State()
    picking_from = State(); picking_to = State()
    calc_count = State(); calc_buy = State(); calc_sell = State()

# ================= ФОНОВІ ТА ДОПОМІЖНІ ФУНКЦІЇ =================
async def cleanup_cooldowns():
    while True:
        await asyncio.sleep(600)
        try:
            now = datetime.now(UTC) 
            expired = [uid for uid, dt in user_cooldowns.items() if (now - dt).total_seconds() > 3600]
            for uid in expired: del user_cooldowns[uid]
            if expired: logger.info(f"Очищено {len(expired)} старих кулдаунів")
        except Exception: logger.exception("Критична помилка cleanup_cooldowns:")

async def safe_delete(msg):
    try: await msg.delete()
    except Exception: pass

async def set_bot_commands():
    commands = [
        types.BotCommand(command="start", description="🚀 Головне меню"),
        types.BotCommand(command="help", description="📖 Як користуватися ботом")
    ]
    await bot.set_my_commands(commands)

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
def get_start_kb():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="❓ Допомога"), KeyboardButton(text="💰 Налаштувати бюджет")]
    ], resize_keyboard=True)

def get_main_kb(d):
    m = d.get("mode")
    if not m:
        return ReplyKeyboardMarkup(keyboard=[
            [KeyboardButton(text="❓ Допомога"), KeyboardButton(text="💰 Налаштувати бюджет")],
            [KeyboardButton(text="🗺 Обрати режим")]
        ], resize_keyboard=True)

    m_l = "🌍 Охоплення: Всі міста" if m == "all" else "📍 Маршрут: Шлях"
    e_l = "🚫 Вимкнути фільтр 30хв" if d.get("extra") else "⚡ Свіжі ціни (30хв)"
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="🚀 Запустити сканер")],
        [KeyboardButton(text=m_l), KeyboardButton(text=e_l)],
        [KeyboardButton(text="🧮 Калькулятор"), KeyboardButton(text="💰 Налаштувати бюджет")],
        [KeyboardButton(text="🔄 Перезавантаження"), KeyboardButton(text="❓ Допомога")]
    ], resize_keyboard=True)

def get_mode_inline(): 
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎲 Всі міста", callback_data="set_mode_all")],
        [InlineKeyboardButton(text="📍 Конкретний шлях", callback_data="set_mode_custom")]
    ])

# ================= ЛОГІКА СКАНУВАННЯ =================
async def download_items():
    global items_data, is_db_ready, http_session
    start_time = datetime.now()
    try:
        async with http_session.get("https://raw.githubusercontent.com/ao-data/ao-bin-dumps/master/formatted/items.json", timeout=60) as r:
            if r.status == 200:
                data = await r.json(content_type=None)
                allowed = ["weapon","armor","plate","leather","cloth","bag","cape","potion","meal","mount","tool","shapeshifter","offhand"]
                items_data = {i["UniqueName"]: i for i in data if i.get("UniqueName","").startswith(("T4_","T5_","T6_","T7_","T8_")) and any(x in i.get("UniqueName","").lower() for x in allowed)}
                logger.info(f"✅ БД завантажена: {len(items_data)} шт. за {(datetime.now()-start_time).total_seconds():.2f}с")
    except Exception: logger.exception("Помилка БД:")
    finally: is_db_ready = True

def fmt_t(s):
    if not s or s.startswith("0001"): return "???"
    try:
        dt = datetime.fromisoformat(s.split(".")[0].replace("Z","")).replace(tzinfo=UTC)
        m = int((datetime.now(UTC)-dt).total_seconds()/60)
        return f"{m}м" if m < 60 else f"{m//60}г"
    except: return "???"

async def scan_logic(d, f_c=None, t_c=None):
    global http_session
    if not items_data: return [] 
    res = []; b_l = d.get("buy_limit", 0); p_l = d.get("profit_limit", 4000); ext = d.get("extra", False)
    i_list = list(items_data.keys()); cities = [f_c, t_c] if f_c and t_c else CITIES
    
    for i in range(0, len(i_list), 50):
        url = f"https://europe.albion-online-data.com/api/v2/stats/prices/{','.join(i_list[i:i+50])}?locations={','.join(cities)}"
        data = None
        
        for attempt in range(3):
            try:
                async with http_session.get(url, timeout=20) as resp:
                    if resp.status == 429: await asyncio.sleep(1); continue
                    if resp.status != 200: logger.warning(f"API статус {resp.status}"); break
                    data = await resp.json()
                    break
            except Exception: logger.exception("Мережева помилка API:"); await asyncio.sleep(1)
            
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
                        res.append({'id':i_id,'q':int(q),'from':sc,'to':tc,'buy':buy,'sell':sell,'p_p':int(sell*0.935-buy),'p_n':p_n,'bd':c_d[sc]['sell_price_min_date'],'sd':c_d[tc].get(sk)})
        if i % 300 == 0: await asyncio.sleep(0.2)
    return res
# ================= ОБРОБНИКИ ПОДІЙ =================
@dp.message(F.text == "🚀 Запустити сканер", StateFilter('*'))
async def main_search(m, state: FSMContext):
    u_id, now = m.from_user.id, datetime.now(UTC)
    is_admin = (u_id == ADMIN_ID)
    d = await state.get_data()
    
    if not is_db_ready or not items_data: return await m.answer("⏳ База ще вантажиться, зачекай пару секунд...")
    
    b = d.get("buy_limit", 0)
    if b <= 0: 
        req_kb = ReplyKeyboardMarkup(keyboard=[
            [KeyboardButton(text="💰 Налаштувати бюджет")],
            [KeyboardButton(text="❌ Скасувати")]
        ], resize_keyboard=True)
        return await m.answer("⚠️ Спочатку потрібно встановити бюджет для сканування!", reply_markup=req_kb)
        
    mode = d.get("mode")
    if not mode: return await m.answer("🗺️ Обери режим!", reply_markup=get_mode_inline())

    if is_admin:
        logger.info(f"Адмін запустив скан (Маршрут: {d.get('f_c', 'All')} -> {d.get('t_c', 'All')})")
        s_msg = await m.answer("⚡ <b>Адмін-сканування...</b>", parse_mode=ParseMode.HTML, reply_markup=ReplyKeyboardRemove())
        res = await scan_logic(d, d.get('f_c'), d.get('t_c'))
        await safe_delete(s_msg)
        if not res: await m.answer("📭 Нічого не знайдено.\nАбо ринок порожній під твої ліміти, або сервери Альбіону тимчасово не відповідають.", reply_markup=get_main_kb(d))
        else: await disp_res(m, res); await m.answer(f"✅ Угод: {len(res)}", reply_markup=get_main_kb(d))
    else:
        async with active_scans_lock:
            if u_id in active_scans: return await m.answer("⚠️ Твій попередній запит ще обробляється!")
            if u_id in user_cooldowns and (now - user_cooldowns[u_id]).total_seconds() < 25:
                return await m.answer(f"⏳ Зачекай {int(25-(now-user_cooldowns[u_id]).total_seconds())} сек.")
            active_scans.add(u_id)
            logger.info(f"Активних сканів: {len(active_scans)}")
            
        async with scan_semaphore:
            user_cooldowns[u_id] = now
            try:
                s_msg = await m.answer("🔍 Сканую Європу...", reply_markup=ReplyKeyboardRemove())
                res = await scan_logic(d, d.get('f_c'), d.get('t_c'))
                await safe_delete(s_msg)
                if not res: await m.answer("📭 Нічого не знайдено.\nАбо ринок порожній під твої ліміти, або сервери Альбіону тимчасово не відповідають.", reply_markup=get_main_kb(d))
                else: await disp_res(m, res); await m.answer(f"✅ Готово! Знайдено: {len(res)}", reply_markup=get_main_kb(d))
            finally:
                active_scans.discard(u_id)

@dp.message(F.text == "❓ Допомога", StateFilter('*'))
@dp.message(Command("help"), StateFilter('*'))
async def cmd_help(m, state: FSMContext):
    text = (
        "📖 <b>Як користуватися ботом:</b>\n\n"
        "🚀 <b>Запустити сканер</b> — знаходить вигідні угоди на переторговку.\n"
        "💰 <b>Налаштувати бюджет</b> — встановити ліміти покупки і мінімум прибутку.\n"
        "🌍 <b>Охоплення / Маршрут</b> — вибрати всі міста або конкретний шлях.\n"
        "⚡ <b>Свіжі ціни</b> — показує тільки угоди з цінами до 30 хвилин.\n"
        "🧮 <b>Калькулятор</b> — розраховує прибуток вручну.\n"
        "🔄 <b>Перезавантаження</b> — очистити налаштування."
    )
    d = await state.get_data()
    await m.answer(text, parse_mode=ParseMode.HTML, reply_markup=get_main_kb(d))

@dp.message(F.text.in_(["🌍 Охоплення: Всі міста", "📍 Маршрут: Шлях", "🗺 Обрати режим"]), StateFilter('*'))
async def toggle_mode_menu(m, state: FSMContext):
    d = await state.get_data()
    current_mode = d.get("mode")
    if current_mode == "all": mode_display = "🌍 Всі міста"
    elif current_mode == "custom": mode_display = f"📍 {d.get('f_c')} ➔ {d.get('t_c')}"
    else: mode_display = "❌ Не встановлено"
    
    info_text = f"<b>Поточний режим:</b> {mode_display}\n\nОбери новий режим:"
    await m.answer(info_text, reply_markup=get_mode_inline(), parse_mode=ParseMode.HTML)

@dp.message(F.text == "💰 Налаштувати бюджет", StateFilter('*'))
async def limit_menu(m, state: FSMContext):
    await state.set_state(None)
    d = await state.get_data()
    b, p = d.get("buy_limit", 0), d.get("profit_limit", 4000)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"💰 Бюджет ({b:,})", callback_data="set_limit_buy")],
        [InlineKeyboardButton(text=f"📈 Профіт ({p:,})", callback_data="set_limit_profit")]
    ])
    await m.answer("⚙️ Налаштування бюджету:", reply_markup=kb)

@dp.callback_query(F.data.startswith("set_limit_"), StateFilter('*'))
async def set_limit_cb(cb, state: FSMContext):
    try:
        await cb.answer()
        t = cb.data.split("_")[2]
        cancel_kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="❌ Скасувати")]], resize_keyboard=True)
        
        if t == "buy":
            await state.set_state(BotState.waiting_for_buy_limit)
            await cb.message.answer("💰 Введи макс. ціну покупки (лише цифри):", reply_markup=cancel_kb)
        else:
            await state.set_state(BotState.waiting_for_profit_limit)
            await cb.message.answer("📈 Введи мін. чистий профіт:", reply_markup=cancel_kb)
        await safe_delete(cb.message)
    except Exception: logger.exception("Помилка в set_limit_cb:")

@dp.message(F.text == "🧮 Калькулятор", StateFilter('*'))
async def calc_start(m, state: FSMContext):
    await state.set_state(BotState.calc_count)
    cancel_kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="❌ Скасувати")]], resize_keyboard=True)
    await m.answer("📦 Введи кількість предметів:", reply_markup=cancel_kb)

@dp.message(StateFilter(BotState.calc_count, BotState.calc_buy, BotState.calc_sell))
async def h_calc(m, state: FSMContext):
    if m.text == "❌ Скасувати": return
    await bot.send_chat_action(chat_id=m.chat.id, action=ChatAction.TYPING) 
    t0 = time.monotonic() 
    try:
        v = int(m.text.replace(" ", "").replace(",", ""))
        if v <= 0: 
            await state.set_state(None)
            return await m.answer("❌ Введи позитивне число (більше 0)! Спробуй ще раз з меню.")
        
        curr = await state.get_state()
        if "calc_count" in str(curr):
            await state.update_data(c=v); await state.set_state(BotState.calc_buy); await m.answer("💰 Введи ціну КУПІВЛІ (за 1 шт):")
        elif "calc_buy" in str(curr):
            await state.update_data(b=v); await state.set_state(BotState.calc_sell); await m.answer("📤 Введи ціну ПРОДАЖУ (за 1 шт):")
        else:
            d = await state.get_data(); cnt, buy = d.get('c', 1), d.get('b', 0)
            if cnt <= 0 or buy <= 0: 
                await state.set_state(None)
                return await m.answer("❌ Помилка у попередніх введеннях! Запусти калькулятор наново.")
                
            await state.set_state(None)
            p_prem, p_norm = int((v * 0.935) - buy) * cnt, int((v * 0.895) - buy) * cnt
            text = (f"📊 <b>Результат для {cnt} шт:</b>\n──────────────────\n👑 З Преміумом: <code>{p_prem:,}</code>\n💀 Без према: <code>{p_norm:,}</code>\n──────────────────")
            await m.answer(text, reply_markup=get_main_kb(d), parse_mode=ParseMode.HTML)
    except: 
        await state.set_state(None)
        await m.answer("❌ Будь ласка, введи тільки число! Запусти калькулятор наново.")
    finally:
        logger.info(f"Обробка калькулятора (h_calc) зайняла {time.monotonic()-t0:.3f}с для {m.from_user.id}")

@dp.message(F.text == "❌ Скасувати", StateFilter('*'))
async def cancel_limit(m, state: FSMContext):
    d = await state.get_data()
    await state.set_state(None)
    await m.answer("🚫 Дію скасовано. Повернення до меню.", reply_markup=get_main_kb(d))

@dp.message(StateFilter(BotState.waiting_for_buy_limit, BotState.waiting_for_profit_limit))
async def h_limits(m, state: FSMContext):
    await bot.send_chat_action(chat_id=m.chat.id, action=ChatAction.TYPING)
    try:
        v = int(m.text.replace(" ","").replace(",",""))
        if v <= 0: 
            await state.set_state(None)
            return await m.answer("❌ Число повинно бути більше 0! Спробуй наново.")
        
        curr = await state.get_state()
        if "buy" in str(curr): await state.update_data(buy_limit=v)
        else: await state.update_data(profit_limit=v)
        
        d = await state.get_data()
        await state.set_state(None)
        await m.answer(f"✅ Збережено: {v:,}", reply_markup=get_main_kb(d))
    except: 
        await state.set_state(None)
        await m.answer("❌ Помилка! Введи тільки ціле число. Спробуй наново.")

@dp.callback_query(F.data.startswith("set_mode_"), StateFilter('*'))
async def set_mode_cb(cb, state: FSMContext):
    try:
        await cb.answer()
        m = cb.data.split("_")[2]
        await state.update_data(mode=m)
        d = await state.get_data()
        
        if m == "all":
            await safe_delete(cb.message)
            await cb.message.answer("🌍 Режим: <b>Всі міста</b> встановлено!\n\n🚀 Тисни <b>\"Запустити сканер\"</b>", reply_markup=get_main_kb(d), parse_mode=ParseMode.HTML)
        else:
            await state.set_state(BotState.picking_from)
            await cb.message.edit_text("📍 Звідки веземо товар?", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=f"{CITY_EMOJIS[c]} {c}", callback_data=f"city_{c}")] for c in CITIES if c!="Black Market"]))
    except Exception: logger.exception("Помилка в set_mode_cb:")

@dp.callback_query(StateFilter(BotState.picking_from), F.data.startswith("city_"))
async def from_cb(cb, state: FSMContext):
    try:
        await cb.answer(); c = cb.data.split("_")[1]; await state.update_data(f_c=c); await state.set_state(BotState.picking_to)
        await cb.message.edit_text(f"✅ Звідки: {c}\n📍 Тепер обери куди (Пункт Б):", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=f"{CITY_EMOJIS[ci]} {ci}", callback_data=f"city_{ci}")] for ci in CITIES if ci!=c and ci!="Black Market"]))
    except Exception: logger.exception("Помилка в from_cb:")

@dp.callback_query(StateFilter(BotState.picking_to), F.data.startswith("city_"))
async def to_cb(cb, state: FSMContext):
    try:
        await cb.answer(); t = cb.data.split("_")[1]; await state.update_data(t_c=t, mode="custom"); d = await state.get_data(); await state.set_state(None)
        await safe_delete(cb.message)
        await cb.message.answer(f"🚀 Маршрут <b>{d['f_c']} ➔ {t}</b> встановлено!\n\n🚀 Тисни <b>\"Запустити сканер\"</b>", reply_markup=get_main_kb(d), parse_mode=ParseMode.HTML)
    except Exception: logger.exception("Помилка в to_cb:")

@dp.message(F.text.in_(["⚡ Свіжі ціни (30хв)", "🚫 Вимкнути фільтр 30хв"]), StateFilter('*'))
async def toggle_extra(m, state: FSMContext):
    d = await state.get_data(); val = not d.get("extra", False); await state.update_data(extra=val); d = await state.get_data()
    status_msg = "✅ Фільтр 30хв <b>увімкнутий</b> — показуватимуться тільки свіжі ціни (до 30 хв)." if val else "❌ Фільтр 30хв <b>відключений</b> — показуватимуться всі доступні угоди."
    await m.answer(status_msg, parse_mode=ParseMode.HTML, reply_markup=get_main_kb(d))

async def disp_res(msg, res):
    if not res: return
    res.sort(key=lambda x: x['p_n'], reverse=True)
    full_text = ""; messages = []
    
    for idx, r in enumerate(res[:15], 1):
        b_id = r['id'].split("@")[0]; enc = r['id'].split("@")[1] if "@" in r['id'] else "0"; tier = b_id.split('_')[0][1:]
        name = items_data.get(b_id, {}).get("LocalizedNames", {}).get("RU-RU", b_id)
        name = re.sub(r'\s*\([^)]*\)', '', name)
        for t in TRASH: name = name.replace(t, "")
        icon = get_item_icon(b_id)
        
        item_text = (f"{idx}) {icon} <b>{name.upper()}</b> 🔸 <b>[{tier}.{enc}]</b>\n✨ Качество: <b>{QUALITY_NAMES.get(r['q'], 'Обычное')}</b>\n──────────────────\n"
                     f"📥 <b>КУПІВЛЯ:</b> {CITY_EMOJIS[r['from']]} {r['from']}\n💰 Цена: <code>{r['buy']:,}</code> (⏳ <b>{fmt_t(r['bd'])}</b>)\n\n"
                     f"📤 <b>ПРОДАЖ:</b> {CITY_EMOJIS[r['to']]} {r['to']}\n💰 Цена: <code>{r['sell']:,}</code> (⏳ <b>{fmt_t(r['sd'])}</b>)\n──────────────────\n"
                     f"💵 <b>ПРИБУТОК:</b>\n👑 Преміум: <code>+{r['p_p']:,}</code>\n💀 Без према: <code>+{r['p_n']:,}</code>\n──────────────────\n\n")
        
        if len(full_text) + len(item_text) > 3900: messages.append(full_text); full_text = item_text
        else: full_text += item_text
            
    if full_text: messages.append(full_text)
    for text in messages: await msg.answer(text, parse_mode=ParseMode.HTML)

@dp.message(F.text == "🔄 Перезавантаження", StateFilter('*'))
async def btn_res(m, state: FSMContext):
    if m.from_user.id == ADMIN_ID:
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📥 Оновити БД", callback_data="adm_upd")],[InlineKeyboardButton(text="✅ Скинути все", callback_data="conf_res"), InlineKeyboardButton(text="❌ Ні", callback_data="cancel_res")]])
        await m.answer("🛠 Адмін-панель:", reply_markup=kb)
    else:
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Скинути", callback_data="conf_res"), InlineKeyboardButton(text="❌ Ні", callback_data="cancel_res")]])
        await m.answer("⚠️ Скинути твої дані?", reply_markup=kb)

@dp.callback_query(F.data == "adm_upd")
async def adm_upd(cb):
    if cb.from_user.id == ADMIN_ID: asyncio.create_task(download_items()); await cb.message.answer("✅ База оновлюється...")
    await cb.answer()

@dp.callback_query(F.data == "conf_res")
async def conf_res(cb, state: FSMContext): 
    await state.clear(); await cb.answer(); await cb.message.answer("🔄 Все скинуто!", reply_markup=get_start_kb())

@dp.callback_query(F.data == "cancel_res")
async def cancel_res(cb): await cb.answer(); await safe_delete(cb.message)

@dp.message(Command("start"), StateFilter('*'))
async def cmd_start(m, state: FSMContext): await state.clear(); await m.answer("👋 Бот готовий!", reply_markup=get_start_kb())

async def main():
    global http_session
    
    if not os.environ.get("BOT_TOKEN"):
        logger.error("🚨 BOT_TOKEN не встановлено! Бот не може бути запущений.")
        return
        
    if ADMIN_ID == 0: logger.warning("⚠️ ADMIN_ID не встановлена! Функції адміна будуть недоступні.")
        
    await set_bot_commands()
    
    http_session = aiohttp.ClientSession(headers=HEADERS)
    
    await bot.delete_webhook(drop_pending_updates=True)
    asyncio.create_task(download_items())
    asyncio.create_task(cleanup_cooldowns())
    
    try:
        await dp.start_polling(bot)
    finally:
        await http_session.close()

if __name__ == "__main__": asyncio.run(main())
