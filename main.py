import os
import json
import aiohttp
import asyncio
from datetime import datetime, timedelta, UTC
from aiogram import Bot, Dispatcher, types, F
from aiogram.enums import ParseMode
from aiogram.filters import Command, StateFilter
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

# ================= НАЛАШТУВАННЯ =================
ADMIN_ID = 1052964898  # ⚠️ ВПИШИ СВІЙ TELEGRAM ID СЮДИ

MARKET_BASE_URL = "https://europe.albion-online-data.com"
MARKET_PATH = "/api/v2/stats/prices/{}?locations={}"
ITEMS_URL = "https://raw.githubusercontent.com/ao-data/ao-bin-dumps/master/formatted/items.json"

CITIES = ["Bridgewatch", "Martlock", "Lymhurst", "Thetford", "Fort Sterling", "Caerleon", "Brecilien", "Black Market"]
CITY_EMOJIS = {
    "Lymhurst": "🟢", "Martlock": "🔵", "Caerleon": "⚫",
    "Thetford": "🟣", "Bridgewatch": "🟠", "Fort Sterling": "⚪",
    "Brecilien": "🌸", "Black Market": "💀" 
}

QUALITY_NAMES = {1: "Обычное", 2: "Хорошее", 3: "Выдающееся", 4: "Отличное", 5: "Шедевр"}
TRASH_WORDS = ["Знаток ", "Мастер ", "Великий мастер ", "Старейшина ", "Ученик ", "Новичок "]

class BotState(StatesGroup):
    waiting_for_buy_limit = State()
    waiting_for_profit_limit = State()
    picking_from = State()
    picking_to = State()
    calc_count = State()
    calc_buy = State()
    calc_sell = State()

# ================= ГЛОБАЛЬНІ ЗМІННІ =================
bot = Bot(token=os.environ.get("BOT_TOKEN"))
dp = Dispatcher()
items_data = {}

max_buy_limit = 0 
min_profit_limit = 4000  
extra_filter_active = False 
current_mode = None  
is_db_ready = False  

# ================= КЛАВІАТУРИ =================
def get_start_kb():
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="❓ Допомога"), KeyboardButton(text="⚙️ Ліміт")]], resize_keyboard=True)

def get_main_kb():
    mode_label = "Всі" if current_mode == "all" else ("Шлях" if current_mode == "custom" else "Не обрано")
    extra_label = "🚫 Екстра відміна" if extra_filter_active else "⚡ Екстра тестування"
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🔍 Пошук")],
            [KeyboardButton(text=f"🗺️ Режими ({mode_label})"), KeyboardButton(text=extra_label)],
            [KeyboardButton(text="🧮 Калькулятор"), KeyboardButton(text="⚙️ Ліміт")],
            [KeyboardButton(text="🔄 Перезавантаження")]
        ],
        resize_keyboard=True
    )

def get_limits_inline():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"💰 Ліміт купівлі ({max_buy_limit:,})", callback_data="set_limit_buy")],
        [InlineKeyboardButton(text=f"📈 Мін. прибуток ({min_profit_limit:,})", callback_data="set_limit_profit")]
    ])

def get_mode_inline():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎲 Рандом міста (Всі)", callback_data="set_mode_all")],
        [InlineKeyboardButton(text="📍 На вибір (Шлях)", callback_data="set_mode_custom")]
    ])

def get_city_inline(exclude_city=None):
    buttons = [[InlineKeyboardButton(text=f"{CITY_EMOJIS[c]} {c}", callback_data=f"city_{c}")] for c in CITIES if c != "Black Market" and c != exclude_city]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# ================= ЛОГІКА ДАНИХ =================
async def download_items():
    global items_data, is_db_ready
    is_db_ready = False
    try:
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(ITEMS_URL) as resp:
                if resp.status == 200:
                    raw_data = await resp.json(content_type=None)
                    allowed = ["weapon", "armor", "plate", "leather", "cloth", "bag", "cape", "potion", "meal", "mount", "relic", "artefact", "tool", "shapeshifter"]
                    items_data = {
                        i["UniqueName"]: i for i in raw_data 
                        if i.get("UniqueName", "").startswith(("T4_", "T5_", "T6_", "T7_", "T8_")) and any(x in i.get("UniqueName", "").lower() for x in allowed)
                    }
    except Exception: pass
    finally: is_db_ready = True  

def get_dt(date_str):
    if not date_str or date_str.startswith("0001"): return datetime(1970, 1, 1, tzinfo=UTC)
    try: return datetime.fromisoformat(date_str.split(".")[0].replace("Z", "")).replace(tzinfo=UTC)
    except: return datetime(1970, 1, 1, tzinfo=UTC)

def format_time(date_str):
    dt = get_dt(date_str)
    if dt.year == 1970: return "???"
    m = int((datetime.now(UTC) - dt).total_seconds() / 60)
    return f"{m}м" if m < 60 else f"{m//60}г"

def get_item_prefix(unique_name, localized_name):
    un, ln = unique_name.lower(), localized_name.lower()
    if any(x in un for x in ["shoes", "boots"]) or any(x in ln for x in ["ботинки", "сапоги"]): return "🥾"
    if any(x in un for x in ["armor", "jacket", "robe"]) or any(x in ln for x in ["куртка", "доспех", "роба"]): return "🧥"
    if "cape" in un or "плащ" in ln: return "🧣"
    if any(x in un for x in ["head", "helmet", "hood"]) or any(x in ln for x in ["шлем", "капюшон"]): return "👒"
    if any(x in un for x in ["weapon", "sword", "bow", "staff", "axe", "mace", "dagger", "spear", "hammer"]): return "🗡️"
    if any(x in un for x in ["shield", "orb", "book", "torch"]) or "щит" in ln: return "🛡️"
    if "bag" in un or "сумка" in ln: return "🎒"
    if "mount" in un or any(x in ln for x in ["конь", "бык", "олень"]): return "🐴"
    if "potion" in un or "зелье" in ln: return "🧪"
    if any(x in un for x in ["meal", "food"]) or any(x in ln for x in ["жаркое", "пирог", "салат"]): return "🍲"
    if "glove" in un or "перчатки" in ln: return "🧤"
    return "📦"

def is_menu_command(text):
    return text and any(x in text for x in ["🔍", "🗺️", "⚡", "🚫", "🧮", "⚙️", "🔄", "❓"])

def to_int(text):
    try: return int(text.replace(" ", "").replace(",", ""))
    except ValueError: return None

async def scan_logic(from_city=None, to_city=None):
    results = []
    item_list = list(items_data.keys())
    search_cities = [from_city, to_city] if from_city and to_city else CITIES
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=45)) as session:
        for i in range(0, len(item_list), 50):
            url = f"{MARKET_BASE_URL}{MARKET_PATH.format(','.join(item_list[i:i+50]), ','.join(search_cities))}"
            try:
                async with session.get(url) as resp:
                    data = await resp.json() if resp.status == 200 else []
            except: continue 
            grouped = {f"{e['item_id']}|{e['quality']}": {} for e in data}
            for e in data: grouped[f"{e['item_id']}|{e['quality']}"][e['city']] = e
            now = datetime.now(UTC)
            for k, city_data in grouped.items():
                i_id, qual = k.split("|")
                sources = [from_city] if from_city else [c for c in city_data if c != "Black Market"]
                for f_city in sources:
                    if f_city not in city_data: continue
                    buy = city_data[f_city].get('sell_price_min', 0)
                    if buy <= 100 or buy > max_buy_limit: continue
                    buy_age = (now - get_dt(city_data[f_city]['sell_price_min_date'])).total_seconds() / 60
                    if buy_age > 180: continue 
                    targets = [to_city] if to_city else [c for c in city_data if c != f_city]
                    for t_city in targets:
                        if t_city not in city_data: continue
                        sd_key = 'buy_price_max_date' if t_city == "Black Market" else 'sell_price_min_date'
                        sell = city_data[t_city].get('buy_price_max' if t_city == "Black Market" else 'sell_price_min', 0)
                        if sell <= buy or (sell/buy) > 10: continue
                        sell_age = (now - get_dt(city_data[t_city].get(sd_key))).total_seconds() / 60
                        if sell_age > 180: continue 
                        if extra_filter_active and (buy_age > 30 or sell_age > 30): continue
                        p_p, p_n = int(sell * 0.935 - buy), int(sell * 0.895 - buy)
                        if p_n >= min_profit_limit:
                            results.append({'id':i_id,'q':int(qual),'from':f_city,'to':t_city,'buy':buy,'sell':sell,'p_p':p_p,'p_n':p_n,'bd':city_data[f_city]['sell_price_min_date'],'sd':city_data[t_city].get(sd_key)})
    return results
# ================= ОБРОБНИКИ КНОПОК =================
@dp.message(Command("start"), StateFilter('*'))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("👋 <b>Вітаю в Albion Trader Bot!</b>\n\nТисни <b>❓ Допомога</b> або <b>⚙️ Ліміт</b>.", reply_markup=get_start_kb(), parse_mode=ParseMode.HTML)

@dp.message(F.text == "❓ Допомога", StateFilter('*'))
async def cmd_help(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("📖 1. ⚙️ Ліміт — бюджет.\n2. 🔍 Пошук — старт.\n3. 🗺️ Режими — вибір міст.", reply_markup=get_start_kb(), parse_mode=ParseMode.HTML)

@dp.message(F.text == "⚙️ Ліміт", StateFilter('*'))
async def limit_menu(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("⚙️ Обери ліміт:", reply_markup=get_limits_inline(), parse_mode=ParseMode.HTML)

@dp.message(F.text == "🔍 Пошук", StateFilter('*'))
async def main_search(message: types.Message, state: FSMContext):
    await state.clear()
    if not is_db_ready: return await message.answer("⏳ Завантаження бази...")
    if max_buy_limit <= 0: return await message.answer("Встанови ліміт!")
    if current_mode is None: return await message.answer("Обери режим!", reply_markup=get_mode_inline())
    if current_mode == "all":
        await message.answer(f"🔍 Сканую (Ліміт {max_buy_limit:,})...", reply_markup=ReplyKeyboardRemove())
        res = await scan_logic()
        await display_results(message, res)
        await message.answer("Готово!", reply_markup=get_main_kb())
    else:
        await message.answer("📍 Звідки?", reply_markup=get_city_inline())
        await state.set_state(BotState.picking_from)

@dp.message(F.text == "📱 Меню", StateFilter('*'))
async def menu_back(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Меню:", reply_markup=get_main_kb())

@dp.message(F.text.startswith("🗺️ Режими"), StateFilter('*'))
async def modes_btn(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Режими:", reply_markup=get_mode_inline())

@dp.message(F.text.contains("Екстра"), StateFilter('*'))
async def toggle_extra(message: types.Message, state: FSMContext):
    await state.clear()
    global extra_filter_active
    extra_filter_active = not extra_filter_active
    await message.answer(f"⚡ Екстра: {'УВІМК' if extra_filter_active else 'ВИМК'}", reply_markup=get_main_kb())

@dp.message(F.text == "🧮 Калькулятор", StateFilter('*'))
async def calc_init(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("📦 Кількість:", reply_markup=ReplyKeyboardRemove())
    await state.set_state(BotState.calc_count)

@dp.message(F.text == "🔄 Перезавантаження", StateFilter('*'))
async def btn_restart(message: types.Message, state: FSMContext):
    await state.clear()
    if message.from_user.id == ADMIN_ID:
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📥 Оновити БД", callback_data="admin_update")],[InlineKeyboardButton(text="🔄 Рестарт", callback_data="confirm_restart")]])
        await message.answer("🛠 Адмін панель:", reply_markup=kb)
    else:
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Так", callback_data="confirm_restart"), InlineKeyboardButton(text="❌ Ні", callback_data="cancel_restart")]])
        await message.answer("⚠️ Скинути все?", reply_markup=kb)

@dp.callback_query(F.data == "admin_update")
async def do_admin_update(callback: types.CallbackQuery):
    if callback.from_user.id == ADMIN_ID:
        await callback.message.edit_text("⏳ Оновлення...")
        asyncio.create_task(download_items())
        await callback.message.edit_text("✅ Запущено!")
    await callback.answer()

@dp.callback_query(F.data == "confirm_restart")
async def do_restart_yes(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    global max_buy_limit, current_mode; max_buy_limit = 0; current_mode = None
    await callback.message.delete()
    await callback.message.answer("🔄 Скинуто!", reply_markup=get_start_kb())
    await callback.answer()

@dp.callback_query(F.data.startswith("set_limit_"))
async def set_limit_callback(callback: types.CallbackQuery, state: FSMContext):
    l_type = callback.data.split("_")[2]
    if l_type == "buy":
        await callback.message.edit_text("💰 Ціна покупки:")
        await state.set_state(BotState.waiting_for_buy_limit)
    else:
        await callback.message.edit_text("📈 Мін. прибуток:")
        await state.set_state(BotState.waiting_for_profit_limit)
    await callback.answer()

@dp.callback_query(F.data.startswith("set_mode_"))
async def set_mode(callback: types.CallbackQuery, state: FSMContext):
    global current_mode; current_mode = callback.data.split("_")[2]
    await state.clear()
    if current_mode == "all": await callback.message.answer("✅ Всі міста!", reply_markup=get_main_kb())
    else: await callback.message.answer("📍 Звідки?", reply_markup=get_city_inline())
    await callback.answer()

@dp.callback_query(BotState.picking_from)
async def from_city(callback: types.CallbackQuery, state: FSMContext):
    await state.update_data(f_c=callback.data.split("_")[1])
    await callback.message.edit_text("📍 Куди?", reply_markup=get_city_inline(callback.data.split("_")[1]))
    await state.set_state(BotState.picking_to)

@dp.callback_query(BotState.picking_to)
async def to_city(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await callback.message.edit_text(f"🚀 {data['f_c']} ➔ {callback.data.split("_")[1]}")
    res = await scan_logic(data['f_c'], callback.data.split("_")[1])
    await display_results(callback.message, res)
    await state.clear(); await callback.message.answer("Готово!", reply_markup=get_main_kb())

@dp.message(BotState.waiting_for_buy_limit)
async def handle_buy_limit_input(message: types.Message, state: FSMContext):
    val = to_int(message.text)
    if val is not None:
        global max_buy_limit; max_buy_limit = val; await state.clear()
        if current_mode is None: await message.answer("✅ Обери режим:", reply_markup=get_mode_inline())
        else: await message.answer("✅ Збережено!", reply_markup=get_main_kb())
    else: await message.answer("❌ Введи число!")

@dp.message(BotState.waiting_for_profit_limit)
async def handle_profit_limit_input(message: types.Message, state: FSMContext):
    val = to_int(message.text)
    if val is not None:
        global min_profit_limit; min_profit_limit = val; await state.clear()
        if current_mode is None: await message.answer("✅ Обери режим:", reply_markup=get_mode_inline())
        else: await message.answer("✅ Збережено!", reply_markup=get_main_kb())
    else: await message.answer("❌ Введи число!")

@dp.message(BotState.calc_count)
async def calc_cnt(message: types.Message, state: FSMContext):
    val = to_int(message.text)
    if val is not None and val > 0:
        await state.update_data(cnt=val); await message.answer("💰 Ціна КУПІВЛІ:"); await state.set_state(BotState.calc_buy)
    else: await message.answer("❌ Введи число!")

@dp.message(BotState.calc_buy)
async def calc_b(message: types.Message, state: FSMContext):
    val = to_int(message.text)
    if val is not None:
        await state.update_data(b=val); await message.answer("📤 Ціна ПРОДАЖУ:"); await state.set_state(BotState.calc_sell)
    else: await message.answer("❌ Введи число!")

@dp.message(BotState.calc_sell)
async def calc_s(message: types.Message, state: FSMContext):
    val = to_int(message.text)
    if val is not None:
        d = await state.get_data(); cnt, b, s = d['cnt'], d['b'], val
        await message.answer(f"📊 Результат {cnt} шт:\n👑 П: {(int(s*0.935-b)*cnt):,}\n💀 Б: {(int(s*0.895-b)*cnt):,}", reply_markup=get_main_kb(), parse_mode=ParseMode.HTML)
        await state.clear()
    else: await message.answer("❌ Введи число!")

async def display_results(message, res):
    if not res: return await message.answer("Нічого не знайдено.")
    res.sort(key=lambda x: x['p_n'], reverse=True)
    for r in res[:15]:
        base_id = r['id'].split("@")[0]; enchant = r['id'].split("@")[1] if "@" in r['id'] else "0"
        name = items_data.get(base_id, {}).get("LocalizedNames", {}).get("RU-RU", base_id)
        for t in TRASH_WORDS: name = name.replace(t, "")
        await message.answer(f"📦 <b>{get_item_prefix(base_id, name)} {name} [{base_id.split('_')[0][1:]}.{enchant}] ({QUALITY_NAMES[r['q']]})</b>\n🛒 Куп: {CITY_EMOJIS[r['from']]} | <b>{r['buy']:,}</b> (⏳{format_time(r['bd'])})\n💰 Прод: {CITY_EMOJIS[r['to']]} | <b>{r['sell']:,}</b> (⏳{format_time(r['sd'])})\n👑 П: <b>{r['p_p']:,}</b> | 💀: <b>{r['p_n']:,}</b>", parse_mode=ParseMode.HTML)

async def main():
    try: await bot.delete_webhook(drop_pending_updates=True)
    except: pass
    asyncio.create_task(download_items()); await dp.start_polling(bot)

if __name__ == "__main__": asyncio.run(main())
