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
# Пріоритет на Європу для точних цін
MARKET_BASE_URLS = [
    "https://europe.albion-online-data.com", # Твій сервер
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
last_cache_clear = datetime.now(UTC)
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
    allowed_keywords = ["weapon", "armor", "plate", "leather", "cloth", "melee", "ranged", "magic", "off", "shield", "torch", "book", "bag", "cape", "potion", "meal", "mount", "relic", "artefact"]
    filtered = {}
    for item_id, item in items_data.items():
        if not item_id or not item_id.startswith(("T4_", "T5_", "T6_", "T7_", "T8_")): continue
        if any(key in item_id.lower() for key in allowed_keywords):
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
    global last_sent, last_cache_clear
    results = []
    item_ids = list(items_data.keys())
    
    if (datetime.now(UTC) - last_cache_clear) > timedelta(hours=12):
        last_sent.clear()
        last_cache_clear = datetime.now(UTC)

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
                    # Видаляємо помилку: З Чорного ринку купувати НЕ МОЖНА
                    if city_from == "Black Market": continue 
                    
                    buy = e_from.get('sell_price_min', 0)
                    if buy <= 0 or buy > MAX_BUY_PRICE: continue
                    bd = e_from.get('sell_price_min_date', "")

                    for e_to in entries:
                        city_to = e_to['city']
                        if city_from == city_to: continue

                        # Спеціальна логіка для Чорного ринку (тільки продаж)
                        if city_to == "Black Market":
                            sell = e_to.get('buy_price_max', 0) # Ціна миттєвого викупу системою
                            sd = e_to.get('buy_price_max_date', "")
                        else:
                            sell = e_to.get('sell_price_min', 0)
                            sd = e_to.get('sell_price_min_date', "")

                        if sell <= buy: continue

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
            f"💰 Купити: <b>{r[4]:,}</b>\n"
            f"💸 Продати: <b>{r[5]:,}</b>\n"
            f"👑 Прибуток: <b>{r[6]:,}</b>",
            parse_mode=ParseMode.HTML
        )

@dp.message(F.text == "🔍 Пошук")
async def scan_cmd(message: types.Message):
    global scan_running
    if MAX_BUY_PRICE <= 0:
        await message.answer("Спочатку встанови ліміт скупку!")
        return
    if scan_running:
        await message.answer("Пошук триває...")
        return
    scan_running = True
    await message.answer("🔍 Шукаю фліпи на <b>EU сервере</b>...", parse_mode=ParseMode.HTML)
    res = await scan_market()
    if not res: await message.answer("Нічого не знайдено.")
    else: await send_flips(res, message)
    scan_running = False

@dp.message(Command("start"))
@dp.message(F.text == "⚙️ Ліміт Скуп")
async def set_limit(message: types.Message, state: FSMContext):
    await message.answer("Введи ліміт срібла на 1 предмет:")
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
    await message.answer("🔄 Базу оновлено для Європи!", reply_markup=keyboard)

async def main():
    await download_items()
    global items_data
    items_data = filter_items()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
