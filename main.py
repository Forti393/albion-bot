import os
import json
import aiohttp
import asyncio
from datetime import datetime, timedelta, UTC
from aiogram import Bot, Dispatcher, types
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from aiogram.exceptions import TelegramConflictError

print("ФАЙЛ ЗАПУЩЕНО:", __file__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")

if not BOT_TOKEN:
    print("🚨 АЛАРМ! Доступні змінні в системі:", list(os.environ.keys()))
    raise Exception("❌ BOT_TOKEN не знайдено! Додай його у Railway → Variables")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

ITEMS_URL = "https://raw.githubusercontent.com/ao-data/ao-bin-dumps/master/formatted/items.json"

MARKET_BASE_URLS = [
    "https://www.albion-online-data.com",
    "https://europe.albion-online-data.com",
    "https://us.albion-online-data.com",
]

MARKET_PATH = "/api/v2/stats/prices/{}?locations={}"

CITIES = ["Bridgewatch", "Martlock", "Lymhurst", "Thetford", "Caerleon", "Brecilien"]

QUALITY_NAMES = {
    1: "Обычное",
    2: "Хорошее",
    3: "Выдающееся",
    4: "Отличное",
    5: "Шедевр"
}

items_data = {}
last_sent = {}
scan_running = False

keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🔄 Оновити зараз")],
        [KeyboardButton(text="🔁 Перезапустити бота")]
    ],
    resize_keyboard=True
)

async def download_items():
    global items_data
    print("⬇️ Завантажую items.json з GitHub...")
    async with aiohttp.ClientSession() as session:
        async with session.get(ITEMS_URL) as resp:
            text = await resp.text()
            items_data = {item["UniqueName"]: item for item in json.loads(text)}
    print(f"✅ Завантажено предметів: {len(items_data)}")

def filter_items():
    allowed_keywords = [
        "weapon", "armor", "plate", "leather", "cloth",
        "melee", "ranged", "magic", "off", "shield", "torch", "book",
        "bag", "cape", "potion", "meal", "fish", "farm", "mount",
        "gather", "journal", "relic", "artefact", "token",
        "stone", "wood", "plank", "metal", "hide", "fiber", "ore", "bar", "block",
        "tool", "shapeshifter", "avalon", "hell", "keeper", "morgana",
        "undead", "heretic", "demon"
    ]

    filtered = {}
    for item_id, item in items_data.items():
        if not item_id:
            continue
        if not item_id.startswith(("T4_", "T5_", "T6_", "T7_", "T8_")):
            continue
        name = item_id.lower()
        if not any(key in name for key in allowed_keywords):
            continue
        if "test" in name or "debug" in name or "unused" in name:
            continue
        filtered[item_id] = item

    print(f"🔎 Фільтр предметів: {len(filtered)} з {len(items_data)}")
    return filtered

def get_item_type(item_id):
    base = item_id.split("@")[0]
    item = items_data.get(base)
    return item.get("ShopCategory", "unknown") if item else "unknown"

def get_item_name(item_id):
    base_id = item_id.split("@")[0]
    enchant = item_id.split("@")[1] if "@" in item_id else ""
    
    tier = base_id.split("_")[0] if base_id.startswith("T") else ""
    if enchant:
        tier += f".{enchant}"
        
    item = items_data.get(base_id, {})
    loc_names = item.get("LocalizedNames", {})
    name = loc_names.get("RU-RU", loc_names.get("EN-US", base_id))
    
    if tier:
        return f"[{tier}] {name}"
    return name

def enchant_multiplier(e):
    return {1: 1.5, 2: 2, 3: 3, 4: 5}.get(e, 1)

def format_time_ago(date_str):
    if not date_str or date_str.startswith("0001"):
        return "Невідомо"
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
            
        now = datetime.now(UTC)
        diff = now - dt
        minutes = int(diff.total_seconds() / 60)
        
        if minutes < 0:
            return "Щойно"
        elif minutes < 60:
            return f"{minutes} хв. тому"
        else:
            hours = minutes // 60
            return f"{hours} год. тому"
    except:
        return "Невідомо"

def is_fake(item_type, enchant, buy, sell, buy_date, sell_date):
    if buy <= 0 or sell <= 0:
        return True, "zero price"
    if sell < buy:
        return True, "sell < buy"
    if sell / buy > 50:
        return True, "ratio > 50x"

    now = datetime.now(UTC)

    try:
        if buy_date and not buy_date.startswith("0001"):
            bd = datetime.fromisoformat(buy_date.replace("Z", "+00:00"))
            if bd.tzinfo is None: 
                bd = bd.replace(tzinfo=UTC)
            if now - bd > timedelta(hours=12):
                return True, "buy date old"
    except:
        pass

    try:
        if sell_date and not sell_date.startswith("0001"):
            sd = datetime.fromisoformat(sell_date.replace("Z", "+00:00"))
            if sd.tzinfo is None: 
                sd = sd.replace(tzinfo=UTC)
            if now - sd > timedelta(hours=12):
                return True, "sell date old"
    except:
        pass

    m = enchant_multiplier(enchant)
    limits = {
        "consumables": 300000,
        "resources": 150000,
        "equipment": 2000000,
        "artefacts": 15000000,
        "products": 60000000,
        "mount": 80000000,
    }

    max_price = limits.get(item_type, 999999999) * m
    if sell > max_price:
        return True, "too expensive"

    return False, ""

async def fetch_prices(session, items_chunk_str, cities_str):
    for base in MARKET_BASE_URLS:
        url = base + MARKET_PATH.format(items_chunk_str, cities_str)
        try:
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data:
                        return data
        except Exception:
            continue
    return None

async def scan_market():
    global last_sent
    results = []
    fake_count = 0
    real_count = 0

    item_ids = list(items_data.keys())
    total_items = len(item_ids)
    processed_items = 0
    api_calls = 0

    start_time = datetime.now(UTC)
    last_log_time = start_time

    cities_str = ",".join(CITIES)
    chunk_size = 50
    
    # 💰 Твій ліміт по грошах для покупки:
    MAX_BUY_PRICE = 300000

    async with aiohttp.ClientSession() as session:
        for i in range(0, total_items, chunk_size):
            chunk = item_ids[i:i + chunk_size]
            items_chunk_str = ",".join(chunk)
            
            api_calls += 1
            data = await fetch_prices(session, items_chunk_str, cities_str)
            processed_items += len(chunk)

            now = datetime.now(UTC)
            if (now - last_log_time).total_seconds() >= 1.0:
                elapsed = (now - start_time).total_seconds()
                last_log_time = now

            if not data:
                continue

            grouped_data = {}
            for entry in data:
                i_id = entry.get("item_id")
                quality = entry.get("quality", 1)
                
                key = f"{i_id}|{quality}"
                if key not in grouped_data:
                    grouped_data[key] = []
                grouped_data[key].append(entry)

            for key_id, entries in grouped_data.items():
                i_id, quality_str = key_id.split("|")
                quality = int(quality_str)
                
                for entry_from in entries:
                    for entry_to in entries:
                        city_from = entry_from.get("city")
                        city_to = entry_to.get("city")
                        
                        if city_from == city_to or city_from not in CITIES or city_to not in CITIES:
                            continue

                        buy = entry_from.get("sell_price_min", 0)
                        bd = entry_from.get("sell_price_min_date", "")

                        sell = entry_to.get("sell_price_min", 0)
                        sd = entry_to.get("sell_price_min_date", "")

                        enchant = int(i_id.split("@")[1]) if "@" in i_id else 0
                        item_type = get_item_type(i_id)

                        fake, _ = is_fake(item_type, enchant, buy, sell, bd, sd)
                        if fake:
                            fake_count += 1
                            continue

                        profit_prem = int((sell * 0.935) - buy)
                        profit_norm = int((sell * 0.895) - buy)

                        # Додали фільтр ціни покупки: buy <= MAX_BUY_PRICE
                        if profit_prem >= 5000 and buy <= MAX_BUY_PRICE:
                            hash_key = f"{i_id}_{quality}_{city_from}_{city_to}_{sell}"
                            if hash_key not in last_sent:
                                real_count += 1
                                results.append((i_id, quality, city_from, city_to, buy, sell, profit_prem, profit_norm, bd, sd))
                                last_sent[hash_key] = True

    return results

async def send_flips(results, message):
    global last_sent
    
    # Функція для безпечного отримання дати для сортування
    def get_safe_date(d_str):
        if not d_str or d_str.startswith("0001"):
            return "1970-01-01T00:00:00"
        return d_str

    # Сортуємо спочатку за найсвіжішим часом (беремо найстарішу дату з пари buy/sell), 
    # а потім за прибутком
    results.sort(key=lambda x: (min(get_safe_date(x[8]), get_safe_date(x[9])), x[6]), reverse=True)
    
    for item_id, quality, city_from, city_to, buy, sell, profit_prem, profit_norm, bd, sd in results[:30]:
        item_name = get_item_name(item_id)
        q_name = QUALITY_NAMES.get(quality, "Неизвестно")
        
        buy_time = format_time_ago(bd)
        sell_time = format_time_ago(sd)
        
        await message.answer(
            f"📦 <b>{item_name}</b> (<i>{q_name}</i>)\n"
            f"🔹 {city_from} → {city_to}\n"
            f"💰 Купити: <b>{buy:,}</b> ⏳ <i>{buy_time}</i>\n"
            f"💸 Продати: <b>{sell:,}</b> ⏳ <i>{sell_time}</i>\n\n"
            f"👑 Прибуток (Прем): <b>{profit_prem:,}</b>\n"
            f"💀 Прибуток (Без Прем): <b>{profit_norm:,}</b>",
            parse_mode=ParseMode.HTML
        )
    
    if len(results) > 30:
        await message.answer(f"⚠️ Знайдено ще {len(results) - 30} фліпів, але показано 30 найсвіжіших.")

@dp.message(Command("scan"))
async def scan_cmd(message: types.Message):
    global scan_running
    if scan_running:
        await message.answer("⏳ Сканування вже виконується…")
        return
    scan_running = True
    await message.answer("⏳ Сканую ринок (це займе близько хвилини)...")
    results = await scan_market()
    if not results:
        await message.answer("Немає нових свіжих фліпів у межах бюджету.")
    else:
        await send_flips(results, message)
    scan_running = False

@dp.message(Command("start"))
async def start_cmd(message: types.Message):
    await message.answer("Бот запущено. Використовуй /scan або кнопки нижче.", reply_markup=keyboard)

@dp.message(lambda m: m.text == "🔄 Оновити зараз")
async def refresh_cmd(message: types.Message):
    await scan_cmd(message)

@dp.message(lambda m: m.text == "🔁 Перезапустити бота")
async def restart_cmd(message: types.Message):
    global last_sent
    last_sent = {}
    await message.answer("♻️ Перезапускаю бота і завантажую свіжі предмети...")
    await download_items()
    global items_data
    items_data = filter_items()
    await message.answer("✅ Готово! Натисни '🔄 Оновити зараз' або /scan")

async def main():
    await asyncio.sleep(2)
    await bot.delete_webhook(drop_pending_updates=True) 
    
    await download_items()
    global items_data
    items_data = filter_items()
    
    print("🚀 Підключення до Telegram...")
    
    while True:
        try:
            await dp.start_polling(bot)
            break
        except TelegramConflictError:
            print("⚠️ Конфлікт підключення: стара копія бота ще працює.")
            print("⏳ Чекаємо 10 секунд, поки Railway її вб'є, і пробуємо знову...")
            await asyncio.sleep(10)
        except Exception as e:
            print(f"❌ Сталася інша помилка при підключенні: {e}")
            await asyncio.sleep(10)

if __name__ == "__main__":
    asyncio.run(main())
