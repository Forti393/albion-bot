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
ADMIN_ID = 1052964898  # ⚠️ ВПИШИ СВІЙ TELEGRAM ID СЮДИ (цифрами, без лапок)

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

# ================= КЛАВІАТУРИ =================
def get_start_kb():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="❓ Допомога"), KeyboardButton(text="⚙️ Ліміт")]], 
        resize_keyboard=True
    )

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
    global items_data
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(ITEMS_URL) as resp:
                if resp.status == 200:
                    raw_data = await resp.json(content_type=None)
                    allowed = ["weapon", "armor", "plate", "leather", "cloth", "bag", "cape", "potion", "meal", "mount", "relic", "artefact", "tool", "shapeshifter"]
                    items_data = {
                        i["UniqueName"]: i for i in raw_data 
                        if i.get("UniqueName", "").startswith(("T4_", "T5_", "T6_", "T7_", "T8_")) and any(x in i.get("UniqueName", "").lower() for x in allowed)
                    }
    except Exception as e:
        print(f"Помилка завантаження бази предметів: {e}")

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

def to_int(text):
    try: return int(text.replace(" ", "").replace(",", ""))
    except ValueError: return None

async def scan_logic(from_city=None, to_city=None):
    results = []
    item_list = list(items_data.keys())
    search_cities = [from_city, to_city] if from_city and to_city else CITIES
    async with aiohttp.ClientSession() as session:
        for i in range(0, len(item_list), 50):
            url = f"{MARKET_BASE_URL}{MARKET_PATH.format(','.join(item_list[i:i + 50]), ','.join(search_cities))}"
            async with session.get(url) as resp:
                data = await resp.json() if resp.status == 200 else []
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
                            results.append({
                                'id': i_id, 'q': int(qual), 'from': f_city, 'to': t_city,
                                'buy': buy, 'sell': sell, 'p_p': p_p, 'p_n': p_n,
                                'bd': city_data[f_city]['sell_price_min_date'], 'sd': city_data[t_city].get(sd_key)
                            })
    return results

# ================= ГОЛОВНІ КНОПКИ (ПЕРЕБИВАЮТЬ БУДЬ-ЯКИЙ СТАН) =================
@dp.message(Command("start"), StateFilter('*'))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "👋 <b>Вітаю в Albion Trader Bot!</b>\n\n"
        "Для ознайомлення з ботом натисни <b>❓ Допомога</b>.\n"
        "Якщо питань немає, тисни <b>⚙️ Ліміт</b> для початку роботи.",
        reply_markup=get_start_kb(), parse_mode=ParseMode.HTML
    )

@dp.message(F.text == "❓ Допомога", StateFilter('*'))
async def cmd_help(message: types.Message, state: FSMContext):
    await state.clear()
    help_text = (
        "📖 <b>Як користуватися ботом:</b>\n\n"
        "1️⃣ <b>⚙️ Ліміт</b> — тут ти задаєш максимальний бюджет на покупку та мінімальний чистий прибуток. Це обов'язковий крок.\n"
        "2️⃣ <b>🔍 Пошук</b> — запускає сканування ринку Альбіону і видає найвигідніші фліпи.\n"
        "3️⃣ <b>🗺️ Режими</b> — можна шукати 'Всі міста' одразу або везти товар по конкретному маршруту (Шлях).\n"
        "4️⃣ <b>⚡ Екстра</b> — жорсткий фільтр за часом. Залишає в списку ціни, оновлені не пізніше ніж 30 хвилин тому.\n"
        "5️⃣ <b>🧮 Калькулятор</b> — рахує чистий прибуток з партії товару, враховуючи всі податки гри.\n"
        "6️⃣ <b>🔄 Перезавантаження</b> — скидає всі твої налаштування, якщо бот завис або ти хочеш почати з нуля.\n\n"
        "🚀 <i>Тисни <b>⚙️ Ліміт</b>, щоб встановити бюджет і розпочати роботу.</i>"
    )
    await message.answer(help_text, reply_markup=get_start_kb(), parse_mode=ParseMode.HTML)

@dp.message(F.text == "⚙️ Ліміт", StateFilter('*'))
async def limit_menu(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("⚙️ <b>Налаштування лімітів</b>\nОбери ліміт для зміни:", reply_markup=get_limits_inline(), parse_mode=ParseMode.HTML)

@dp.message(F.text == "🔍 Пошук", StateFilter('*'))
async def main_search(message: types.Message, state: FSMContext):
    await state.clear()
    if max_buy_limit <= 0:
        return await message.answer("Спочатку встанови <b>Ліміт купівлі</b>!", parse_mode=ParseMode.HTML)
    if current_mode is None:
        return await message.answer("Спочатку обери <b>Режим пошуку</b>!", reply_markup=get_mode_inline(), parse_mode=ParseMode.HTML)
        
    if current_mode == "all":
        await message.answer(f"🔍 Сканую (Купівля: до {max_buy_limit:,} | Прибуток: від {min_profit_limit:,})...", reply_markup=ReplyKeyboardRemove())
        res = await scan_logic()
        await display_results(message, res)
        await message.answer("Завершено.", reply_markup=get_main_kb())
    else:
        await message.answer("📍 Шлях. Звідки?", reply_markup=get_city_inline())
        await state.set_state(BotState.picking_from)

@dp.message(F.text == "📱 Меню", StateFilter('*'))
async def menu_back(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Головне меню:", reply_markup=get_main_kb())

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
    await message.answer("📦 <b>Кількість предметів:</b>", reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.HTML)
    await state.set_state(BotState.calc_count)

@dp.message(F.text == "🔄 Перезавантаження", StateFilter('*'))
async def btn_restart(message: types.Message, state: FSMContext):
    await state.clear()
    if message.from_user.id == ADMIN_ID:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📥 Оновити Дані (БД)", callback_data="admin_update")],
            [InlineKeyboardButton(text="🔄 Рестарт бота", callback_data="confirm_restart")]
        ])
        await message.answer("🛠 <b>Панель Адміністратора:</b>\nЩо саме хочеш зробити?", reply_markup=kb, parse_mode=ParseMode.HTML)
    else:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Так", callback_data="confirm_restart"), InlineKeyboardButton(text="❌ Ні", callback_data="cancel_restart")]
        ])
        await message.answer("⚠️ Це скине всі налаштування лімітів та поверне до початку.\nВи впевнені?", reply_markup=kb)

# ================= CALLBACKS =================
@dp.callback_query(F.data == "admin_update")
async def do_admin_update(callback: types.CallbackQuery):
    if callback.from_user.id == ADMIN_ID:
        await callback.message.edit_text("⏳ Завантажую нові дані з сервера...")
        await download_items()
        await callback.message.edit_text("✅ Базу предметів успішно оновлено!")
    await callback.answer()

@dp.callback_query(F.data == "confirm_restart")
async def do_restart_yes(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    global max_buy_limit, min_profit_limit, current_mode, extra_filter_active
    max_buy_limit = 0
    min_profit_limit = 4000
    current_mode = None
    extra_filter_active = False
    
    await callback.message.delete()
    await callback.message.answer("🔄 <b>Прогрес скинуто!</b>\nПочни спочатку, натиснувши <b>⚙️ Ліміт</b>.", reply_markup=get_start_kb(), parse_mode=ParseMode.HTML)
    await callback.answer()

@dp.callback_query(F.data == "cancel_restart")
async def do_restart_no(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.delete()
    await callback.message.answer("❌ Перезавантаження скасовано.", reply_markup=get_main_kb())
    await callback.answer()

@dp.callback_query(F.data.startswith("set_limit_"))
async def set_limit_callback(callback: types.CallbackQuery, state: FSMContext):
    l_type = callback.data.split("_")[2]
    if l_type == "buy":
        await callback.message.edit_text("💰 Вкажи <b>максимальну ціну покупки</b> за 1 шт:", parse_mode=ParseMode.HTML)
        await state.set_state(BotState.waiting_for_buy_limit)
    elif l_type == "profit":
        await callback.message.edit_text("📈 Вкажи <b>мінімальний прибуток</b> (можна з мінусом):", parse_mode=ParseMode.HTML)
        await state.set_state(BotState.waiting_for_profit_limit)
    await callback.answer()

@dp.callback_query(F.data.startswith("set_mode_"))
async def set_mode(callback: types.CallbackQuery, state: FSMContext):
    global current_mode
    current_mode = callback.data.split("_")[2]
    await state.clear()
    if current_mode == "all":
        await callback.message.answer("✅ Режим 'Всі міста' вибрано → тисни <b>Пошук</b>", reply_markup=get_main_kb(), parse_mode=ParseMode.HTML)
    else:
        await callback.message.answer("✅ Режим 'Шлях' вибрано.\n📍 Звідки веземо?", reply_markup=get_city_inline())
        await state.set_state(BotState.picking_from)
    await callback.answer()

@dp.callback_query(BotState.picking_from)
async def from_city(callback: types.CallbackQuery, state: FSMContext):
    city = callback.data.split("_")[1]
    await state.update_data(f_c=city)
    await callback.message.edit_text(f"Звідки: {CITY_EMOJIS[city]} {city}\n📍 Куди?", reply_markup=get_city_inline(exclude_city=city))
    await state.set_state(BotState.picking_to)

@dp.callback_query(BotState.picking_to)
async def to_city(callback: types.CallbackQuery, state: FSMContext):
    t_c = callback.data.split("_")[1]
    data = await state.get_data()
    f_c = data.get('f_c')
    await callback.message.edit_text(f"🚀 {f_c} ➔ {t_c}...")
    res = await scan_logic(f_c, t_c)
    await display_results(callback.message, res)
    await state.clear()
    await callback.message.answer("Готово!", reply_markup=get_main_kb())

# ================= ОБРОБНИКИ СТАНІВ ВВОДУ ЦИФР =================
@dp.message(BotState.waiting_for_buy_limit)
async def handle_buy_limit_input(message: types.Message, state: FSMContext):
    val = to_int(message.text)
    if val is not None:
        global max_buy_limit
        max_buy_limit = val
        await state.clear()
        if current_mode is None:
            await message.answer("✅ Збережено!\nТепер <b>обери режим пошуку</b>:", reply_markup=get_mode_inline(), parse_mode=ParseMode.HTML)
        else:
            await message.answer(f"✅ Ліміт купівлі <b>{max_buy_limit:,}</b> збережено.", reply_markup=get_main_kb(), parse_mode=ParseMode.HTML)
    else:
        await message.answer("❌ Введи коректне число!")

@dp.message(BotState.waiting_for_profit_limit)
async def handle_profit_limit_input(message: types.Message, state: FSMContext):
    val = to_int(message.text)
    if val is not None:
        global min_profit_limit
        min_profit_limit = val
        await state.clear()
        if current_mode is None:
            await message.answer("✅ Збережено!\nТепер <b>обери режим пошуку</b>:", reply_markup=get_mode_inline(), parse_mode=ParseMode.HTML)
        else:
            await message.answer(f"✅ Мін. прибуток <b>{min_profit_limit:,}</b> збережено.", reply_markup=get_main_kb(), parse_mode=ParseMode.HTML)
    else:
        await message.answer("❌ Введи коректне число (можна з мінусом)!")

@dp.message(BotState.calc_count)
async def calc_cnt(message: types.Message, state: FSMContext):
    val = to_int(message.text)
    if val is not None and val > 0:
        await state.update_data(cnt=val)
        await message.answer(f"✅ Кількість: {val}\n💰 <b>Ціна КУПІВЛІ (1 шт):</b>", parse_mode=ParseMode.HTML)
        await state.set_state(BotState.calc_buy)
    else:
        await message.answer("❌ Введи додатнє число!")

@dp.message(BotState.calc_buy)
async def calc_b(message: types.Message, state: FSMContext):
    val = to_int(message.text)
    if val is not None:
        await state.update_data(b=val)
        await message.answer(f"✅ Купівля: {val:,}\n📤 <b>Ціна ПРОДАЖУ (1 шт):</b>", parse_mode=ParseMode.HTML)
        await state.set_state(BotState.calc_sell)
    else:
        await message.answer("❌ Введи число!")

@dp.message(BotState.calc_sell)
async def calc_s(message: types.Message, state: FSMContext):
    val = to_int(message.text)
    if val is not None:
        data = await state.get_data()
        cnt, b, s = data['cnt'], data['b'], val
        total_p = int(((s * 0.935) - b) * cnt)
        total_n = int(((s * 0.895) - b) * cnt)
        await message.answer(
            f"📊 <b>Результат для {cnt} шт:</b>\n"
            f"👑 П: <b>{total_p:,}</b>\n"
            f"💀 Б: <b>{total_n:,}</b>", 
            reply_markup=get_main_kb(), parse_mode=ParseMode.HTML
        )
        await state.clear()
    else:
        await message.answer("❌ Введи число!")

async def display_results(message, res):
    if not res:
        return await message.answer("Нічого не знайдено.")
    res.sort(key=lambda x: (max(get_dt(x['bd']), get_dt(x['sd'])), x['p_n']), reverse=True)
    for r in res[:15]:
        item_raw = r['id'].split("@")
        base_id = item_raw[0]
        enchant = item_raw[1] if len(item_raw) > 1 else "0"
        tier = base_id.split("_")