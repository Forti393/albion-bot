import os
import json
import aiohttp
import asyncio
from datetime import datetime, timedelta, UTC
from aiogram import Bot, Dispatcher, types, F
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

# ================= НАЛАШТУВАННЯ =================
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
min_profit_limit = 4000  # Базовий мінімальний прибуток БЕЗ прему
extra_filter_active = False 
current_mode = "all" 

# ================= КЛАВІАТУРИ =================
def get_start_kb():
    """Клавіатура при першому старті"""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="❓ Допомога"), KeyboardButton(text="⚙️ Ліміт")]], 
        resize_keyboard=True
    )

def get_limit_only_kb():
    """Клавіатура після прочитання допомоги"""
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="⚙️ Ліміт")]], resize_keyboard=True)

def get_search_only_kb():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="🔍 Пошук"), KeyboardButton(text="📱 Меню")]],
        resize_keyboard=True
    )

def get_main_kb():
    mode_label = "Всі" if current_mode == "all" else "Шлях"
    extra_label = "🚫 Екстра відміна" if extra_filter_active else "⚡ Екстра тестування"
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🔍 Пошук")],
            [KeyboardButton(text=f"🗺️ Режими ({mode_label})"), KeyboardButton(text=extra_label)],
            [KeyboardButton(text="🧮 Калькулятор"), KeyboardButton(text="⚙️ Ліміт")],
            [KeyboardButton(text="🔁 Оновити базу")]
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
    buttons = []
    for c in CITIES:
        if c == "Black Market" or c == exclude_city: continue
        buttons.append([InlineKeyboardButton(text=f"{CITY_EMOJIS[c]} {c}", callback_data=f"city_{c}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# ================= ЛОГІКА ДАНИХ =================
async def download_items():
    global items_data
    async with aiohttp.ClientSession() as session:
        async with session.get(ITEMS_URL) as resp:
            text = await resp.text()
            raw_data = json.loads(text)
            allowed = ["weapon", "armor", "plate", "leather", "cloth", "bag", "cape", "potion", "meal", "mount", "relic", "artefact", "tool", "shapeshifter"]
            items_data = {
                item["UniqueName"]: item for item in raw_data 
                if item["UniqueName"].startswith(("T4_", "T5_", "T6_", "T7_", "T8_")) 
                and any(x in item["UniqueName"].lower() for x in allowed)
            }

def get_dt(date_str):
    if not date_str or date_str.startswith("0001"): return datetime(1970, 1, 1, tzinfo=UTC)
    try: return datetime.fromisoformat(date_str.split(".")[0].replace("Z", "")).replace(tzinfo=UTC)
    except: return datetime(1970, 1, 1, tzinfo=UTC)

def format_time(date_str):
    dt = get_dt(date_str)
    if dt.year == 1970: return "???"
    diff = datetime.now(UTC) - dt
    m = int(diff.total_seconds() / 60)
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

async def scan_logic(from_city=None, to_city=None):
    results = []
    item_list = list(items_data.keys())
    search_cities = [from_city, to_city] if from_city and to_city else CITIES
    async with aiohttp.ClientSession() as session:
        for i in range(0, len(item_list), 50):
            chunk = item_list[i:i + 50]
            url = f"{MARKET_BASE_URL}{MARKET_PATH.format(','.join(chunk), ','.join(search_cities))}"
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
                    bd_dt = get_dt(city_data[f_city]['sell_price_min_date'])
                    buy_age = (now - bd_dt).total_seconds() / 60
                    if buy_age > 180: continue 
                    targets = [to_city] if to_city else [c for c in city_data if c != f_city]
                    for t_city in targets:
                        if t_city not in city_data: continue
                        sd_key = 'buy_price_max_date' if t_city == "Black Market" else 'sell_price_min_date'
                        sell = city_data[t_city].get('buy_price_max' if t_city == "Black Market" else 'sell_price_min', 0)
                        if sell <= buy or (sell/buy) > 10: continue
                        sd_dt = get_dt(city_data[t_city].get(sd_key))
                        sell_age = (now - sd_dt).total_seconds() / 60
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

# ================= ОБРОБНИКИ СТАРТУ ТА ДОПОМОГИ =================

@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "👋 <b>Вітаю в Albion Trader Bot!</b>\n\n"
        "Для ознайомлення з ботом натисни <b>❓ Допомога</b>.\n"
        "Якщо питань немає, тисни <b>⚙️ Ліміт</b> для початку роботи.",
        reply_markup=get_start_kb(), parse_mode=ParseMode.HTML
    )

@dp.message(F.text == "❓ Допомога")
async def cmd_help(message: types.Message, state: FSMContext):
    await state.clear()
    help_text = (
        "📖 <b>Як користуватися ботом:</b>\n\n"
        "1️⃣ <b>⚙️ Ліміт</b> — найголовніша кнопка. Тут ти задаєш свій максимальний бюджет на покупку однієї речі та бажаний мінімальний чистий прибуток. Без налаштування ліміту бот не почне пошук.\n\n"
        "2️⃣ <b>🔍 Пошук</b> — запускає сканування ринку Альбіону і видає найвигідніші варіанти для перепродажу (фліпи).\n\n"
        "3️⃣ <b>🗺️ Режими</b> — дозволяє змінити тип пошуку. Можна шукати 'Всі міста' одразу (Рандом) або везти товар з конкретного міста в інше (Шлях).\n\n"
        "4️⃣ <b>⚡ Екстра</b> — жорсткий фільтр за часом. Залишає в списку тільки ті ціни, які перевірялися іншими гравцями не пізніше ніж 30 хвилин тому.\n\n"
        "5️⃣ <b>🧮 Калькулятор</b> — ручний інструмент. Вводиш кількість предметів, ціну купівлі та ціну продажу, а бот рахує твій чистий прибуток з урахуванням усіх податків гри.\n\n"
        "🚀 <i>Все просто! Тисни <b>⚙️ Ліміт</b>, щоб встановити бюджет і розпочати роботу.</i>"
    )
    await message.answer(help_text, reply_markup=get_limit_only_kb(), parse_mode=ParseMode.HTML)

# ================= ІНШІ ОБРОБНИКИ =================

@dp.message(F.text == "⚙️ Ліміт")
async def limit_menu(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "⚙️ <b>Налаштування лімітів</b>\n"
        "Обери, який саме ліміт хочеш змінити:", 
        reply_markup=get_limits_inline(), parse_mode=ParseMode.HTML
    )

@dp.callback_query(F.data.startswith("set_limit_"))
async def set_limit_callback(callback: types.CallbackQuery, state: FSMContext):
    l_type = callback.data.split("_")[2]
    if l_type == "buy":
        await callback.message.edit_text("💰 Вкажи <b>максимальну ціну покупки</b> за 1 предмет:", parse_mode=ParseMode.HTML)
        await state.set_state(BotState.waiting_for_buy_limit)
    elif l_type == "profit":
        await callback.message.edit_text("📈 Вкажи <b>мінімальний чистий прибуток</b> (без преміуму).\n<i>Можна вводити з мінусом, напр. -1000:</i>", parse_mode=ParseMode.HTML)
        await state.set_state(BotState.waiting_for_profit_limit)
    await callback.answer()

@dp.message(BotState.waiting_for_buy_limit)
async def handle_buy_limit_input(message: types.Message, state: FSMContext):
    # Додали "❓" у перевірку, щоб кнопка допомоги теж скидала стан
    if any(x in message.text for x in ["🔍", "🗺️", "⚡", "🚫", "🧮", "⚙️", "🔁", "📱", "❓"]):
        await state.clear()
        return
    text = message.text.replace(" ","").replace(",","")
    if text.isdigit():
        global max_buy_limit
        max_buy_limit = int(text)
        await state.clear()
        await message.answer(f"✅ Ліміт купівлі <b>{max_buy_limit:,}</b> збережено.", reply_markup=get_main_kb(), parse_mode=ParseMode.HTML)
    else:
        await message.answer("❌ Введи тільки число!")

@dp.message(BotState.waiting_for_profit_limit)
async def handle_profit_limit_input(message: types.Message, state: FSMContext):
    if any(x in message.text for x in ["🔍", "🗺️", "⚡", "🚫", "🧮", "⚙️", "🔁", "📱", "❓"]):
        await state.clear()
        return
    text = message.text.replace(" ","").replace(",","")
    is_negative = text.startswith("-") and text[1:].isdigit()
    
    if text.isdigit() or is_negative:
        global min_profit_limit
        min_profit_limit = int(text)
        await state.clear()
        await message.answer(f"✅ Мінімальний прибуток <b>{min_profit_limit:,}</b> збережено.", reply_markup=get_main_kb(), parse_mode=ParseMode.HTML)
    else:
        await message.answer("❌ Введи тільки число (дозволяється мінус)!")

@dp.message(F.text == "🔍 Пошук")
async def main_search(message: types.Message, state: FSMContext):
    await state.clear()
    if max_buy_limit <= 0:
        await message.answer("Спочатку встанови <b>Ліміт купівлі</b>!", parse_mode=ParseMode.HTML)
        return
    if current_mode == "all":
        await message.answer(f"🔍 Сканую (Купівля: до {max_buy_limit:,} | Прибуток: від {min_profit_limit:,})...", reply_markup=ReplyKeyboardRemove())
        res = await scan_logic()
        await display_results(message, res)
        await message.answer("Завершено.", reply_markup=get_main_kb())
    else:
        await message.answer("📍 Шлях. Звідки?", reply_markup=get_city_inline())
        await state.set_state(BotState.picking_from)

@dp.message(F.text == "📱 Меню")
async def menu_back(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Головне меню:", reply_markup=get_main_kb())

@dp.callback_query(F.data.startswith("set_mode_"))
async def set_mode(callback: types.CallbackQuery, state: FSMContext):
    global current_mode
    current_mode = callback.data.split("_")[2]
    await state.clear()
    if current_mode == "all":
        await callback.message.answer("✅ Рандом вибрано → тисни <b>Пошук</b>", reply_markup=get_search_only_kb(), parse_mode=ParseMode.HTML)
    else:
        await callback.message.answer("📍 Шлях. Звідки?", reply_markup=get_city_inline())
        await state.set_state(BotState.picking_from)
    await callback.answer()

@dp.message(F.text.startswith("🗺️ Режими"))
async def modes_btn(message: types.Message):
    await message.answer("Режими:", reply_markup=get_mode_inline())

@dp.message(F.text.contains("Екстра"))
async def toggle_extra(message: types.Message):
    global extra_filter_active
    extra_filter_active = not extra_filter_active
    await message.answer(f"⚡ Екстра: {'УВІМК' if extra_filter_active else 'ВИМК'}", reply_markup=get_main_kb())

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

# --- ОНОВЛЕНИЙ КАЛЬКУЛЯТОР ---
@dp.message(F.text == "🧮 Калькулятор")
async def calc_init(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("📦 <b>Кількість предметів:</b>", reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.HTML)
    await state.set_state(BotState.calc_count)

@dp.message(BotState.calc_count)
async def calc_cnt(message: types.Message, state: FSMContext):
    text = message.text.replace(" ","")
    if text.isdigit():
        await state.update_data(cnt=int(text))
        await message.answer(f"✅ Кількість: {text}\n💰 <b>Ціна КУПІВЛІ (1 шт):</b>", parse_mode=ParseMode.HTML)
        await state.set_state(BotState.calc_buy)
    else:
        await message.answer("❌ Введи число!")

@dp.message(BotState.calc_buy)
async def calc_b(message: types.Message, state: FSMContext):
    text = message.text.replace(" ","")
    if text.isdigit():
        await state.update_data(b=int(text))
        await message.answer(f"✅ Купівля: {text}\n📤 <b>Ціна ПРОДАЖУ (1 шт):</b>", parse_mode=ParseMode.HTML)
        await state.set_state(BotState.calc_sell)
    else:
        await message.answer("❌ Введи число!")

@dp.message(BotState.calc_sell)
async def calc_s(message: types.Message, state: FSMContext):
    text = message.text.replace(" ","")
    if text.isdigit():
        data = await state.get_data()
        cnt, b, s = data['cnt'], data['b'], int(text)
        
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
        await message.answer("Нічого не знайдено.")
        return
    res.sort(key=lambda x: (max(get_dt(x['bd']), get_dt(x['sd'])), x['p_n']), reverse=True)
    for r in res[:15]:
        item_raw = r['id'].split("@")
        base_id = item_raw[0]
        enchant = item_raw[1] if len(item_raw) > 1 else "0"
        tier = base_id.split("_")[0].replace("T", "")
        name = items_data.get(base_id, {}).get("LocalizedNames", {}).get("RU-RU", base_id)
        for trash in ["Знаток ", "Мастер ", "Великий мастер ", "Старейшина ", "Ученик ", "Новичок "]: name = name.replace(trash, "")
        icon = get_item_prefix(base_id, name)
        quality = QUALITY_NAMES.get(r['q'], "Обычное")
        full_name = f"{icon} {name} [{tier}.{enchant}] ({quality})" if enchant != "0" else f"{icon} {name} [{tier}] ({quality})"
        await message.answer(
            f"📦 <b>{full_name}</b>\n"
            f"🛒 Куп: {CITY_EMOJIS[r['from']]} {r['from']} | <b>{r['buy']:,}</b> (⏳{format_time(r['bd'])})\n"
            f"💰 Прод: {CITY_EMOJIS[r['to']]} {r['to']} | <b>{r['sell']:,}</b> (⏳{format_time(r['sd'])})\n"
            f"👑 П: <b>{r['p_p']:,}</b> | 💀: <b>{r['p_n']:,}</b>", parse_mode=ParseMode.HTML
        )

@dp.message(F.text == "🔁 Оновити базу")
async def update_items(message: types.Message):
    await download_items()
    await message.answer("✅ Базу оновлено!")

async def main():
    await download_items()
    await dp.start_polling(bot)

if __name__ == "__main__": asyncio.run(main())
