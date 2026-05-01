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
    "Brecilien": "🌸", "Black Market": "🚩🏴"
}

QUALITY_NAMES = {1: "Обычное", 2: "Хорошее", 3: "Выдающееся", 4: "Отличное", 5: "Шедевр"}

# ================= СТАННИ FSM =================
class BotState(StatesGroup):
    waiting_for_limit = State()
    picking_from = State()
    picking_to = State()
    calc_buy = State()
    calc_sell = State()

# ================= ГЛОБАЛЬНІ ЗМІННІ =================
bot = Bot(token=os.environ.get("BOT_TOKEN"))
dp = Dispatcher()
items_data = {}
max_buy_limit = 0
extra_filter_active = False # Стан фільтру "Екстра"

# ================= КЛАВІАТУРИ =================
def get_main_kb():
    extra_text = "🚫 Екстра відміна" if extra_filter_active else "⚡ Екстра тестування"
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🗺️ Режими"), KeyboardButton(text=extra_text)],
            [KeyboardButton(text="🧮 Калькулятор"), KeyboardButton(text="⚙️ Ліміт")],
            [KeyboardButton(text="🔁 Оновити базу")]
        ],
        resize_keyboard=True
    )

def get_mode_inline():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎲 Рандом міста (Всі)", callback_data="mode_all")],
        [InlineKeyboardButton(text="📍 На вибір (Шлях)", callback_data="mode_custom")]
    ])

def get_city_inline():
    buttons = []
    for c in CITIES:
        if c != "Black Market":
            buttons.append([InlineKeyboardButton(text=f"{CITY_EMOJIS[c]} {c}", callback_data=f"city_{c}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# ================= ДОПОМІЖНІ ФУНКЦІЇ =================
async def download_items():
    global items_data
    async with aiohttp.ClientSession() as session:
        async with session.get(ITEMS_URL) as resp:
            text = await resp.text()
            items_data = {item["UniqueName"]: item for item in json.loads(text)}

def filter_items():
    allowed = ["weapon", "armor", "plate", "leather", "cloth", "bag", "cape", "potion", "meal", "mount", "relic", "artefact", "tool"]
    return {k: v for k, v in items_data.items() if k.startswith(("T4_", "T5_", "T6_", "T7_", "T8_")) and any(x in k.lower() for x in allowed)}

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

# ================= ЛОГІКА СКАНУВАННЯ =================
async def scan_logic(from_city=None, to_city=None):
    results = []
    item_list = list(filter_items().keys())
    search_cities = [from_city, to_city] if from_city and to_city else CITIES
    
    # Авто-порівняння для Карлеону
    if to_city == "Caerleon" and "Black Market" not in search_cities:
        search_cities.append("Black Market")

    async with aiohttp.ClientSession() as session:
        for i in range(0, len(item_list), 50):
            chunk = item_list[i:i + 50]
            url = f"{MARKET_BASE_URL}{MARKET_PATH.format(','.join(chunk), ','.join(search_cities))}"
            async with session.get(url) as resp:
                data = await resp.json() if resp.status == 200 else []
                
            grouped = {}
            for e in data:
                k = f"{e['item_id']}|{e['quality']}"
                if k not in grouped: grouped[k] = {}
                grouped[k][e['city']] = e

            now = datetime.now(UTC)
            for k, city_data in grouped.items():
                i_id, qual = k.split("|")
                sources = [from_city] if from_city else [c for c in city_data if c != "Black Market"]
                
                for f_city in sources:
                    if f_city not in city_data: continue
                    buy = city_data[f_city].get('sell_price_min', 0)
                    if buy <= 100 or buy > max_buy_limit: continue
                    
                    # Екстра-фільтр часу для купівлі
                    bd_dt = get_dt(city_data[f_city]['sell_price_min_date'])
                    if extra_filter_active and (now - bd_dt) > timedelta(minutes=10): continue

                    targets = [to_city] if to_city else [c for c in city_data if c != f_city]
                    for t_city in targets:
                        if t_city not in city_data: continue
                        
                        sell = city_data[t_city].get('buy_price_max' if t_city == "Black Market" else 'sell_price_min', 0)
                        if sell <= buy or (sell/buy) > 10: continue
                        
                        # Екстра-фільтр часу для продажу
                        sd_date_key = 'buy_price_max_date' if t_city == "Black Market" else 'sell_price_min_date'
                        sd_dt = get_dt(city_data[t_city].get(sd_date_key))
                        if extra_filter_active and (now - sd_dt) > timedelta(minutes=10): continue

                        extra_info = ""
                        if t_city == "Caerleon" and "Black Market" in city_data:
                            bm_p = city_data["Black Market"].get('buy_price_max', 0)
                            if bm_p > 0: extra_info = f"\n⚖️ <i>На ЧР зараз: {bm_p:,}</i>"

                        profit = int((sell * 0.935) - buy)
                        if profit > 5000:
                            results.append({
                                'id': i_id, 'q': int(qual), 'from': f_city, 'to': t_city,
                                'buy': buy, 'sell': sell, 'profit': profit,
                                'bd': city_data[f_city]['sell_price_min_date'],
                                'sd': city_data[t_city].get(sd_date_key),
                                'extra': extra_info
                            })
    return results

# ================= ОБРОБНИКИ КОМАНД =================
@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await message.answer("👋 Привіт! Встанови ліміт срібла на 1 предмет:", reply_markup=ReplyKeyboardRemove())
    await state.set_state(BotState.waiting_for_limit)

@dp.message(BotState.waiting_for_limit)
async def set_limit(message: types.Message, state: FSMContext):
    global max_buy_limit
    if message.text.isdigit():
        max_buy_limit = int(message.text)
        await message.answer(f"✅ Ліміт {max_buy_limit:,} встановлено.\nОбери режим пошуку:", reply_markup=get_mode_inline())
        # Клавіатуру поки не виводимо, вона з'явиться після вибору режиму або скасування
    else:
        await message.answer("Будь ласка, введи число.")

@dp.callback_query(F.data == "mode_all")
async def mode_all_handler(callback: types.CallbackQuery):
    await callback.message.answer("Режим: Рандом міста. Запускаю пошук...", reply_markup=get_main_kb())
    res = await scan_logic()
    await display_results(callback.message, res)
    await callback.answer()

@dp.callback_query(F.data == "mode_custom")
async def mode_custom_handler(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Звідки веземо?", reply_markup=get_city_inline())
    await state.set_state(BotState.picking_from)
    await callback.answer()

@dp.message(F.text == "🗺️ Режими")
async def show_modes(message: types.Message):
    await message.answer("Обери режим пошуку:", reply_markup=get_mode_inline())

@dp.message(F.text.contains("Екстра"))
async def toggle_extra(message: types.Message):
    global extra_filter_active
    extra_filter_active = not extra_filter_active
    status = "АКТИВОВАНО (фільтр 10хв)" if extra_filter_active else "ВИМКНЕНО"
    await message.answer(f"⚡ Екстра тестування: {status}", reply_markup=get_main_kb())

# ================= ЛОГІКА ВИБОРУ МІСТ =================
@dp.callback_query(BotState.picking_from)
async def city_from_chosen(callback: types.CallbackQuery, state: FSMContext):
    city = callback.data.split("_")[1]
    await state.update_data(from_city=city)
    await callback.message.edit_text(f"Звідки: {CITY_EMOJIS[city]} {city}\nКуди веземо?", reply_markup=get_city_inline())
    await state.set_state(BotState.picking_to)

@dp.callback_query(BotState.picking_to)
async def city_to_chosen(callback: types.CallbackQuery, state: FSMContext):
    city_to = callback.data.split("_")[1]
    data = await state.get_data()
    city_from = data['from_city']
    await callback.message.edit_text(f"🚀 Маршрут: {city_from} ➔ {city_to}\nШукаю...")
    res = await scan_logic(city_from, city_to)
    await display_results(callback.message, res)
    await callback.message.answer("Меню:", reply_markup=get_main_kb())
    await state.clear()

# ================= КАЛЬКУЛЯТОР ТА ІНШЕ =================
@dp.message(F.text == "🧮 Калькулятор")
async def calc_start(message: types.Message, state: FSMContext):
    await message.answer("Введи ціну КУПІВЛІ:", reply_markup=ReplyKeyboardRemove())
    await state.set_state(BotState.calc_buy)

@dp.message(BotState.calc_buy)
async def calc_buy_step(message: types.Message, state: FSMContext):
    if message.text.isdigit():
        await state.update_data(buy=int(message.text))
        await message.answer("Введи ціну ПРОДАЖУ:")
        await state.set_state(BotState.calc_sell)

@dp.message(BotState.calc_sell)
async def calc_sell_step(message: types.Message, state: FSMContext):
    if message.text.isdigit():
        data = await state.get_data()
        buy, sell = data['buy'], int(message.text)
        p_tax = int(sell * 0.935 - buy)
        n_tax = int(sell * 0.895 - buy)
        await message.answer(
            f"📊 <b>Розрахунок:</b>\n"
            f"📥 Купівля: {buy:,}\n"
            f"📤 Продаж: {sell:,}\n\n"
            f"👑 З Премом (6.5%): <b>{p_tax:,}</b>\n"
            f"💀 Без Према (10.5%): <b>{n_tax:,}</b>",
            parse_mode=ParseMode.HTML, reply_markup=get_main_kb()
        )
        await state.clear()

async def display_results(message, res):
    if not res:
        await message.answer("Нічого не знайдено (спробуй вимкнути Екстра режим або змінити міста).")
        return
    res.sort(key=lambda x: (max(get_dt(x['bd']), get_dt(x['sd'])), x['profit']), reverse=True)
    for r in res[:15]:
        name = items_data.get(r['id'].split("@")[0], {}).get("LocalizedNames", {}).get("RU-RU", r['id'])
        q = QUALITY_NAMES.get(r['q'], "Обычное")
        await message.answer(
            f"📦 <b>{name}</b> ({q})\n"
            f"{CITY_EMOJIS[r['from']]} {r['from']} ➔ {CITY_EMOJIS[r['to']]} <b>{r['to']}</b>\n"
            f"💰 Купити: <b>{r['buy']:,}</b> (⏳{format_time(r['bd'])})\n"
            f"💸 Продати: <b>{r['sell']:,}</b> (⏳{format_time(r['sd'])})\n"
            f"👑 Прибуток: <b>{profit_fmt(r['profit'])}</b>{r['extra']}",
            parse_mode=ParseMode.HTML
        )

def profit_fmt(val): return f"{val:,}"

@dp.message(F.text == "⚙️ Ліміт")
async def cmd_limit_reset(message: types.Message, state: FSMContext):
    await cmd_start(message, state)

@dp.message(F.text == "🔁 Оновити базу")
async def refresh_base(message: types.Message):
    await download_items()
    await message.answer("✅ Базу предметів оновлено!")

async def main():
    await download_items()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
