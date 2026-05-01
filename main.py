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
    choosing_mode = State()
    picking_from = State()
    picking_to = State()
    calc_buy = State()
    calc_sell = State()
    calc_premium = State()

# ================= КЛАВІАТУРИ =================
main_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🔍 Всі міста"), KeyboardButton(text="📍 Обрати шлях")],
        [KeyboardButton(text="🧮 Калькулятор"), KeyboardButton(text="⚙️ Ліміт")],
        [KeyboardButton(text="🔁 Оновити базу")]
    ],
    resize_keyboard=True
)

def get_city_kb():
    buttons = [[InlineKeyboardButton(text=f"{CITY_EMOJIS[c]} {c}", callback_data=f"city_{c}")] for c in CITIES if c != "Black Market"]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# ================= КОД БОТА =================
bot = Bot(token=os.environ.get("BOT_TOKEN"))
dp = Dispatcher()
items_data = {}
max_buy_limit = 0

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

async def scan(from_city=None, to_city=None):
    results = []
    item_ids = list(filter_items().keys())
    search_cities = [from_city, to_city] if from_city and to_city else CITIES
    if to_city == "Caerleon" and "Black Market" not in search_cities:
        search_cities.append("Black Market") # Авто-додавання ЧР для порівняння

    async with aiohttp.ClientSession() as session:
        for i in range(0, len(item_ids), 50):
            chunk = item_ids[i:i + 50]
            url = f"{MARKET_BASE_URL}{MARKET_PATH.format(','.join(chunk), ','.join(search_cities))}"
            async with session.get(url) as resp:
                data = await resp.json() if resp.status == 200 else []
                
            grouped = {}
            for e in data:
                k = f"{e['item_id']}|{e['quality']}"
                if k not in grouped: grouped[k] = {}
                grouped[k][e['city']] = e

            for k, city_data in grouped.items():
                i_id, qual = k.split("|")
                # Визначаємо де купуємо
                sources = [from_city] if from_city else [c for c in city_data if c != "Black Market"]
                
                for f_city in sources:
                    if f_city not in city_data: continue
                    buy = city_data[f_city].get('sell_price_min', 0)
                    if buy <= 100 or buy > max_buy_limit: continue
                    
                    # Визначаємо куди продаємо
                    targets = [to_city] if to_city else [c for c in city_data if c != f_city]
                    
                    for t_city in targets:
                        if t_city not in city_data: continue
                        
                        if t_city == "Black Market":
                            sell = city_data[t_city].get('buy_price_max', 0)
                        else:
                            sell = city_data[t_city].get('sell_price_min', 0)
                        
                        if sell <= buy or (sell/buy) > 10: continue
                        
                        # Якщо знайшли Карлеон, автоматично шукаємо ЧР для порівняння
                        extra_info = ""
                        if t_city == "Caerleon" and "Black Market" in city_data:
                            bm_price = city_data["Black Market"].get('buy_price_max', 0)
                            if bm_price > 0:
                                extra_info = f"\n⚖️ <i>На ЧР зараз: {bm_price:,}</i>"

                        profit = int((sell * 0.935) - buy)
                        if profit > 5000:
                            results.append({
                                'id': i_id, 'q': int(qual), 'from': f_city, 'to': t_city,
                                'buy': buy, 'sell': sell, 'profit': profit,
                                'bd': city_data[f_city]['sell_price_min_date'],
                                'sd': city_data[t_city].get('buy_price_max_date' if t_city=="Black Market" else 'sell_price_min_date'),
                                'extra': extra_info
                            })
    return results

# ================= ОБРОБНИКИ =================
@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await message.answer("Привіт! Встанови ліміт срібла на 1 предмет (наприклад, 500000):")
    await state.set_state(BotState.waiting_for_limit)

@dp.message(BotState.waiting_for_limit)
async def set_limit(message: types.Message, state: FSMContext):
    global max_buy_limit
    try:
        max_buy_limit = int(message.text.replace(" ",""))
        await message.answer(f"✅ Ліміт {max_buy_limit:,} встановлено. Обирай режим:", reply_markup=main_kb)
        await state.clear()
    except: await message.answer("Тільки цифри!")

@dp.message(F.text == "🔍 Всі міста")
async def scan_all(message: types.Message):
    await message.answer("🔎 Шукаю найкращі варіанти на EU...")
    res = await scan()
    await display_results(message, res)

@dp.message(F.text == "📍 Обрати шлях")
async def pick_from(message: types.Message, state: FSMContext):
    await message.answer("Звідки веземо?", reply_markup=get_city_kb())
    await state.set_state(BotState.picking_from)

@dp.callback_query(BotState.picking_from)
async def pick_to(callback: types.CallbackQuery, state: FSMContext):
    city = callback.data.split("_")[1]
    await state.update_data(from_city=city)
    await callback.message.edit_text(f"Звідки: {CITY_EMOJIS[city]} {city}\nКуди веземо?", reply_markup=get_city_kb())
    await state.set_state(BotState.picking_to)

@dp.callback_query(BotState.picking_to)
async def start_custom_scan(callback: types.CallbackQuery, state: FSMContext):
    city_to = callback.data.split("_")[1]
    data = await state.get_data()
    city_from = data['from_city']
    await callback.message.edit_text(f"🚀 Шукаю маршрут: {city_from} ➔ {city_to}...")
    res = await scan(city_from, city_to)
    await state.clear()
    await display_results(callback.message, res)

# ================= КАЛЬКУЛЯТОР =================
@dp.message(F.text == "🧮 Калькулятор")
async def calc_start(message: types.Message, state: FSMContext):
    await message.answer("Введи ціну КУПІВЛІ:", reply_markup=ReplyKeyboardRemove())
    await state.set_state(BotState.calc_buy)

@dp.message(BotState.calc_buy)
async def calc_buy(message: types.Message, state: FSMContext):
    await state.update_data(buy=int(message.text.replace(" ","")))
    await message.answer("Введи ціну ПРОДАЖУ:")
    await state.set_state(BotState.calc_sell)

@dp.message(BotState.calc_sell)
async def calc_sell(message: types.Message, state: FSMContext):
    await state.update_data(sell=int(message.text.replace(" ","")))
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="З Премом (6.5%)", callback_data="tax_6.5"),
        InlineKeyboardButton(text="Без Према (10.5%)", callback_data="tax_10.5")
    ]])
    await message.answer("Ти з преміумом?", reply_markup=kb)

@dp.callback_query(F.data.startswith("tax_"))
async def calc_final(callback: types.CallbackQuery, state: FSMContext):
    tax_rate = float(callback.data.split("_")[1]) / 100
    data = await state.get_data()
    buy, sell = data['buy'], data['sell']
    profit = int(sell * (1 - tax_rate) - buy)
    await callback.message.edit_text(
        f"📊 <b>Результат:</b>\n"
        f"📥 Купівля: {buy:,}\n"
        f"📤 Продаж: {sell:,}\n"
        f"📈 Чистий прибуток: <b>{profit:,}</b>", 
        parse_mode=ParseMode.HTML
    )
    await callback.message.answer("Повертаємось до меню:", reply_markup=main_kb)
    await state.clear()

async def display_results(message, res):
    if not res:
        await message.answer("Нічого вигідного не знайдено.")
        return
    res.sort(key=lambda x: (max(get_dt(x['bd']), get_dt(x['sd'])), x['profit']), reverse=True)
    for r in res[:15]:
        item_name = items_data.get(r['id'].split("@")[0], {}).get("LocalizedNames", {}).get("RU-RU", r['id'])
        q = QUALITY_NAMES.get(r['q'], "Обычное")
        await message.answer(
            f"📦 <b>{item_name}</b> ({q})\n"
            f"{CITY_EMOJIS[r['from']]} {r['from']} ➔ {CITY_EMOJIS[r['to']]} <b>{r['to']}</b>\n"
            f"💰 Купити: <b>{r['buy']:,}</b> (⏳{format_time(r['bd'])})\n"
            f"💸 Продати: <b>{r['sell']:,}</b> (⏳{format_time(r['sd'])})\n"
            f"👑 Прибуток: <b>{r['profit']:,}</b>{r['extra']}",
            parse_mode=ParseMode.HTML
        )

async def main():
    await download_items()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
