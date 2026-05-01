import os
import json
import aiohttp
import asyncio
from datetime import datetime, timedelta, UTC
from aiogram import Bot, Dispatcher, types
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton

print("ФАЙЛ ЗАПУЩЕНО:", __file__)

# Railway передає токен через Variables
BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
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
    """Завантажує items.json з GitHub."""
    global items_data

    print("⬇️ Завантажую items.json з GitHub...")

    async with aiohttp.ClientSession() as session:
        async with session.get(ITEMS_URL) as resp:
            text = await resp.text()
            items_data = {item["UniqueName"]: item for item in json.loads(text)}

    print(f"✅ Завантажено предметів: {len(items_data)}")


# 🔥 УНІВЕРСАЛЬНИЙ ФІЛЬТР (працює з будь-яким items.json)
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

        # 1) Tier з назви
        if not item_id.startswith(("T4_", "T5_", "T6_", "T7_", "T8_")):
            continue

        # 2) Категорія по назві предмета
        name = item_id.lower()
        if not any(key in name for key in allowed_keywords):
            continue

        # 3) Відсікаємо сміття
        if "test" in name or "debug" in name or "unused" in name:
            continue

        filtered[item_id] = item

    print(f"🔎 Фільтр предметів: {len(filtered)} з {len(items_data)}")
    return filtered


def get_item_type(item_id):
    base = item_id.split("@")[0]
    item = items_data.get(base)
    return item.get("ShopCategory", "unknown") if item else "unknown"


def enchant_multiplier(e):
    return {1: 1.5, 2: 2, 3: 3, 4: 5}.get(e, 1)


def is_fake(item_type, enchant, buy, sell, buy_date, sell_date):
    if buy <= 0 or sell <= 0:
        return True, "zero price"
    if sell < buy:
        return True, "sell < buy"
    if sell / buy > 50:
        return True, "ratio > 50x"

    now = datetime.now(UTC)

    try:
        if buy_date:
            bd = datetime.fromisoformat(buy_date.replace("Z", "+00:00"))
            if now - bd > timedelta(hours=12):
                return True, "buy date old"
    except:
        pass

    try:
        if sell_date:
            sd = datetime.fromisoformat(sell_date.replace("Z", "+00:00"))
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


async def fetch_prices(session, item_id, city_from, city_to):
    for base in MARKET_BASE_URLS:
        url = base + MARKET_PATH.format(item_id, f"{city_from},{city_to}")
        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    continue
                data = await resp.json()
                if data:
                    return data
        except:
            continue
    return None


async def scan_market():
    global last_sent
    results = []
    fake_count = 0
    real_count = 0

    total_items = len(items_data)
    processed_items = 0
    api_calls = 0

    start_time = datetime.now(UTC)
    last_log_time = start_time

    print("────────────────────────────────────────────")
    print("🔎 Сканування розпочато...")
    print(f"📦 Всього предметів після фільтра: {total_items}")
    print("────────────────────────────────────────────")

    async with aiohttp.ClientSession() as session:
        for item_id in items_data.keys():
            processed_items += 1

            now = datetime.now(UTC)
            if (now - last_log_time).total_seconds() >= 0.5:
                elapsed = (now - start_time).total_seconds()
                speed = processed_items / elapsed if elapsed > 0 else 0

                print(
                    f"[{elapsed:6.1f}s] "
                    f"ITEMS: {processed_items}/{total_items} "
                    f"| REAL: {real_count} "
                    f"| FAKE: {fake_count} "
                    f"| API: {api_calls} "
                    f"| SPEED: {speed:.1f} items/s"
                )

                last_log_time = now

            for city_from in CITIES:
                for city_to in CITIES:
                    if city_from == city_to:
                        continue

                    api_calls += 1
                    data = await fetch_prices(session, item_id, city_from, city_to)
                    if not data:
                        continue

                    for entry in data:
                        buy = entry.get("buy_price_max", 0)
                        sell = entry.get("sell_price_min", 0)
                        bd = entry.get("buy_price_max_date", "")
                        sd = entry.get("sell_price_min_date", "")

                        enchant = int(item_id.split("@")[1]) if "@" in item_id else 0
                        item_type = get_item_type(item_id)

                        fake, _ = is_fake(item_type, enchant, buy, sell, bd, sd)
                        if fake:
                            fake_count += 1
                            continue

                        profit = sell - buy
                        if profit < 5000:
                            continue

                        key = f"{item_id}_{city_from}_{city_to}_{sell}"
                        if key in last_sent:
                            continue

                        real_count += 1
                        results.append((item_id, city_from, city_to, buy, sell, profit))

    end_time = datetime.now(UTC)
    total_time = (end_time - start_time).total_seconds()

    print("────────────────────────────────────────────")
    print("🏁 Сканування завершено!")
    print(f"⏱ Час: {total_time:.2f} сек")
    print(f"📦 Оброблено предметів: {processed_items}/{total_items}")
    print(f"🔁 API запитів: {api_calls}")
    print(f"✔ Реальних фліпів: {real_count}")
    print(f"✖ Фейкових записів: {fake_count}")
    print(f"📊 Результатів до відправки: {len(results)}")
    print("────────────────────────────────────────────")

    return results


async def send_flips(results, message):
    global last_sent
    for item_id, city_from, city_to, buy, sell, profit in results:
        await message.answer(
            f"📦 <b>{item_id}</b>\n"
            f"🔹 {city_from} → {city_to}\n"
            f"💰 Купити: <b>{buy}</b>\n"
            f"💸 Продати: <b>{sell}</b>\n"
            f"📈 Профіт: <b>{profit}</b>",
            parse_mode=ParseMode.HTML
        )
        last_sent[f"{item_id}_{city_from}_{city_to}_{sell}"] = True


@dp.message(Command("scan"))
async def scan_cmd(message):
    global scan_running

    if scan_running:
        await message.answer("⏳ Сканування вже виконується…")
        return

    scan_running = True
    await message.answer("⏳ Сканую ринок...")

    results = await scan_market()

    if not results:
        await message.answer("Немає нових фліпів.")
    else:
        await send_flips(results, message)

    scan_running = False


@dp.message(Command("start"))
async def start_cmd(message):
    await message.answer("Бот запущено. Використовуй /scan", reply_markup=keyboard)


@dp.message(lambda m: m.text == "🔄 Оновити зараз")
async def refresh_cmd(message):
    await scan_cmd(message)


@dp.message(lambda m: m.text == "🔁 Перезапустити бота")
async def restart_cmd(message):
    global last_sent
    last_sent = {}
    await message.answer("♻️ Перезапускаю бота...")
    await download_items()
    await message.answer("Готово! Натисни /scan")


async def main():
    await download_items()

    global items_data
    items_data = filter_items()

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())