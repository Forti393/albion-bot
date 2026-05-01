import os
import json
import aiohttp
import asyncio
from datetime import datetime, timedelta, UTC
from aiogram import Bot, Dispatcher, types, F
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from aiogram.exceptions import TelegramConflictError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

# ================= НАЛАШТУВАННЯ =================
MARKET_BASE_URLS = [
    "https://europe.albion-online-data.com", # Твій сервер (EU)
    "https://www.albion-online-data.com",
]

ITEMS_URL = "https://raw.githubusercontent.com/ao-data/ao-bin-dumps/master/formatted/items.json"
MARKET_PATH = "/api/v2/stats/prices/{}?locations={}"

CITIES = ["Bridgewatch", "Martlock", "Lymhurst", "Thetford", "Fort Sterling", "Caerleon", "Brecilien", "Black Market"]

CITY_EMOJIS = {
    "Lymhurst": "🟢", "Martlock": "🔵", "Caerleon": "⚫",
    "Thetford": "🟣", "Bridgewatch": "🟠", "Fort Sterling": "⚪",
    "Brecilien": "🌸", "Black Market": "🚩🏴"
}

QUALITY_NAMES = {1: "Обычное", 2: "Хорошее", 3: "Выдающееся", 4: "Отличное", 5: "Шедевр"}
MAX_BUY_PRICE = 0
# ================================================

bot = Bot(token=os.environ.get("BOT_TOKEN"))
dp = Dispatcher()

items_data = {}
last_sent = {}
scan_running = False

class LimitState(StatesGroup):
    waiting_for_limit = State()

keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🔍 Пошук"), KeyboardButton(text="⚙️ Ліміт Скуп")],
        [KeyboardButton(text="🔁 Перезапустити бота")]
    ],
    resize_keyboard=True
)

async def download_items():
    global items_data
    async with aiohttp.ClientSession() as session:
        async with session.get(ITEMS_URL) as resp:
            text = await resp.text()
            items_data = {item["UniqueName"]: item for item in json.loads(text)}

def filter_items():
    allowed = ["weapon", "armor", "plate", "leather", "cloth", "bag", "cape", "potion", "meal", "mount", "relic", "artefact", "tool"]
    filtered = {}
    for item_id, item in items_data.items():
        if not item_id or not item_id.startswith(("T4_", "T5_", "T6_", "T7_", "T8_")): continue
        if any(key in item_id.lower() for key in allowed):
            filtered[item_id] = item
    return filtered

async def fetch_prices(session, items_chunk_str, cities_str):
    for base in MARKET_BASE_URLS:
        url = base + MARKET_PATH.format(items_chunk_str, cities_str)
        try:
            async with session.get(url, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data: return data
        except: continue
    return None

async def scan_market():
    global last_sent, MAX_BUY_PRICE
    results = []
    item_ids = list(items_data.keys())
    
    cities_str = ",".join(CITIES)
    async with aiohttp.ClientSession() as session:
        for i in range(0, len(item_ids), 50):
            chunk = item_ids[i:i + 50]
            data = await fetch_prices(session, ",".join(chunk), cities_str)
            if not data: continue

            grouped = {}
            for entry in data:
                key = f"{entry['item_id']}|{entry['quality']}"
                if key not in grouped: grouped[key] = []
                grouped[key].append(entry)

            for key_id, entries in grouped.items():
                i_id, quality = key_id.split("|")
                
                for e_from in entries:
                    city_from = e_from['city']
                    if city_from == "Black Market": continue # На ЧР не можна купити
                    
                    buy = e_from.get('sell_price_min', 0)
                    if buy <= 100 or buy > MAX_BUY_PRICE: continue # Відсікаємо сміття
                    bd = e_from.get('sell_price_min_date', "")

                    for e_to in entries:
                        city_to = e_to['city']
                        if city_from == city_to: continue

                        if city_to == "Black Market":
                            sell = e_to.get('buy_price_max', 0)
                            sd = e_to.get('buy_price_max_date', "")
                        else:
                            sell = e_to.get('sell_price_min', 0)
                            sd = e_to.get('sell_price_min_date', "")

                        # АНТИ-ФЕЙК ФІЛЬТР: 
                        # 1. Ціна продажу не може бути меншою за покупку.
                        # 2. Якщо ціна продажу в 10+ разів вища за покупку - це аномалія/скам.
                        if sell <= buy or (sell / buy) > 10: continue

                        profit_prem = int((sell * 0.935) - buy)
                        if profit_prem > 5000:
                            hash_key = f"{i_id}_{quality}_{city_from}_{city_to}_{sell}"
                            if hash_key not in last_sent:
                                results.append((i_id, int(quality), city_from, city_to, buy, sell, profit_prem, bd, sd))
                                last_sent[hash_key] = True
    return results

def get_item_name(item_id):
    base_id = item_id.split("@")[0]
    enchant = item_id.split("@")[1] if "@" in item_id else ""
    tier = base_id.split("_")[0] if base_id.startswith("T") else ""
    if enchant: tier += f".{enchant}"
    item = items_data.get(base_id, {})
    name = item.get("LocalizedNames", {}).get("RU-RU", base_id)
    return f"[{tier}] {name}"

def format_time(date_str):
    if not date_str or date_str.startswith("0001"): return "???"
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        diff = datetime.now(UTC) - dt
        mins = int(diff.total_seconds() / 60)
        return f"{mins}м" if mins < 60 else f"{mins//60}г"
    except: return "???"

async def send_flips(results, message):
    results.sort(key=lambda x: x[6], reverse=True)
    for r in results[:20]:
        name = get_item_name(r[0])
        q = QUALITY_NAMES.get(r[1], "Обычное")
        f_e = CITY_EMOJIS.get(r[2], "🏙")
        t_e = CITY_EMOJIS.get(r[3], "🏙")
        
        await message.answer(
            f"📦 <b>{name}</b> ({q})\n"
            f"{f_e} {r[2]} ➔ {t_e} <b>{r[3]}</b>\n"
            f"💰 Купити: <b>{r[4]:,}</b> (⏳{format_time(r[7])})\n"
            f"💸 Продати: <b>{r[5]:,}</b> (⏳{format_time(r[8])})\n"
            f"👑 Прибуток: <b>{r[6]:,}</b>",
            parse_mode=ParseMode.HTML
        )

@dp.message(F.text == "🔍 Пошук")
async def scan_cmd(message: types.Message):
    global scan_running
    if MAX_BUY_PRICE <= 0:
        await message.answer("Встанови ліміт через кнопку!")
        return
    if scan_running: return
    scan_running = True
    await message.answer(f"🔍 Шукаю на <b>EU</b> (до {MAX_BUY_PRICE:,})...", parse_mode=ParseMode.HTML)
    res = await scan_market()
    if not res: await message.answer("Нічого свіжого.")
    else: await send_flips(res, message)
    scan_running = False

@dp.message(Command("start"))
@dp.message(F.text == "⚙️ Ліміт Скуп")
async def set_limit(message: types.Message, state: FSMContext):
    await message.answer("Введи ліміт ціни за 1 предмет:")
    await state.set_state(LimitState.waiting_for_limit)

@dp.message(LimitState.waiting_for_limit)
async def finish_limit(message: types.Message, state: FSMContext):
    global MAX_BUY_PRICE
    try:
        MAX_BUY_PRICE = int(message.text.replace(" ",""))
        await state.clear()
        await message.answer(f"✅ Ліміт {MAX_BUY_PRICE:,} встановлено.", reply_markup=keyboard)
    except: await message.answer("Введи число!")

@dp.message(F.text == "🔁 Перезапустити бота")
async def restart(message: types.Message):
    await download_items()
    global items_data
    items_data = filter_items()
    await message.answer("🔄 База EU оновлена!", reply_markup=keyboard)

async def main():
    await download_items()
    global items_data
    items_data = filter_items()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
