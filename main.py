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

print("ФАЙЛ ЗАПУЩЕНО:", __file__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")

if not BOT_TOKEN:
    print("🚨 АЛАРМ! Доступні змінні в системі:", list(os.environ.keys()))
    raise Exception("❌ BOT_TOKEN не знайдено! Додай його у Railway → Variables")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# ================= НАЛАШТУВАННЯ =================
ITEMS_URL = "https://raw.githubusercontent.com/ao-data/ao-bin-dumps/master/formatted/items.json"

MARKET_BASE_URLS = [
    "https://www.albion-online-data.com",
    "https://europe.albion-online-data.com",
    "https://us.albion-online-data.com",
]
MARKET_PATH = "/api/v2/stats/prices/{}?locations={}"
CITIES = ["Bridgewatch", "Martlock", "Lymhurst", "Thetford", "Caerleon", "Brecilien"]

QUALITY_NAMES = {1: "Обычное", 2: "Хорошее", 3: "Выдающееся", 4: "Отличное", 5: "Шедевр"}

# Тепер стартуємо з нуля, щоб бот ОБОВ'ЯЗКОВО запитав ліміт у користувача
MAX_BUY_PRICE = 0
# ================================================

items_data = {}
last_sent = {}
last_cache_clear = datetime.now(UTC)
scan_running = False

class LimitState(StatesGroup):
    waiting_for_limit = State()

keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🔄 Оновити зараз"), KeyboardButton(text="⚙️ Ліміт Скуп")],
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
        if not item_id or not item_id.startswith(("T4_", "T5_", "T6_", "T7_", "T8_")):
            continue
        name = item_id.lower()
        if not any(key in name for key in allowed_keywords) or any(x in name for x in ["test", "debug", "unused"]):
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
    
    return f"[{tier}] {name}" if tier else name

def format_time_ago(date_str):
    if not date_str or date_str.startswith("0001"):
        return "Невідомо"
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
            
        diff = datetime.now(UTC) - dt
        minutes = int(diff.total_seconds() / 60)
        
        if minutes < 0: return "Щойно"
        if minutes < 60: return f"{minutes} хв. тому"
        return f"{minutes // 60} год. тому"
    except:
        return "Невідомо"

def is_fake(item_type, enchant, buy, sell, buy_date, sell_date):
    if buy <= 0 or sell <= 0 or sell < buy or (sell / buy > 50):
        return True

    now = datetime.now(UTC)

    def is_old(d_str):
        if d_str and not d_str.startswith("0001"):
            try:
                dt = datetime.fromisoformat(d_str.replace("Z", "+00:00"))
                if dt.tzinfo is None: dt = dt.replace(tzinfo=UTC)
                return (now - dt) > timedelta(hours=12)
            except: pass
        return False

    if is_old(buy_date) or is_old(sell_date):
        return True

    m = {1: 1.5, 2: 2, 3: 3, 4: 5}.get(enchant, 1)
    limits = {"consumables": 300000, "resources": 150000, "equipment": 2000000, 
              "artefacts": 15000000, "products": 60000000, "mount": 80000000}
    
    if sell > limits.get(item_type, 999999999) * m:
        return True

    return False

async def fetch_prices(session, items_chunk_str, cities_str):
    for base in MARKET_BASE_URLS:
        url = base + MARKET_PATH.format(items_chunk_str, cities_str)
        try:
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data: return data
        except Exception:
            continue
    return None

async def scan_market():
    global last_sent, last_cache_clear, MAX_BUY_PRICE
    results = []
    item_ids = list(items_data.keys())
    total_items = len(item_ids)
    processed_items = 0
    start_time = datetime.now(UTC)
    last_log_time = start_time

    if (start_time - last_cache_clear) > timedelta(hours=12):
        last_sent.clear()
        last_cache_clear = start_time
        print("🧹 Кеш старих повідомлень автоматично очищено.")

    cities_str = ",".join(CITIES)
    chunk_size = 50

    async with aiohttp.ClientSession() as session:
        for i in range(0, total_items, chunk_size):
            chunk = item_ids[i:i + chunk_size]
            items_chunk_str = ",".join(chunk)
            
            data = await fetch_prices(session, items_chunk_str, cities_str)
            processed_items += len(chunk)

            now = datetime.now(UTC)
            if (now - last_log_time).total_seconds() >= 2.0:
                print(f"⏳ Сканування: {processed_items}/{total_items} предметів...")
                last_log_time = now

            if not data:
                continue

            grouped_data = {}
            for entry in data:
                key = f"{entry.get('item_id')}|{entry.get('quality', 1)}"
                if key not in grouped_data:
                    grouped_data[key] = []
                grouped_data[key].append(entry)

            for key_id, entries in grouped_data.items():
                i_id, quality_str = key_id.split("|")
                quality = int(quality_str)
                enchant = int(i_id.split("@")[1]) if "@" in i_id else 0
                item_type = get_item_type(i_id)
                
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

                        if is_fake(item_type, enchant, buy, sell, bd, sd):
                            continue

                        profit_prem = int((sell * 0.935) - buy)
                        profit_norm = int((sell * 0.895) - buy)

                        if profit_prem >= 5000 and buy <= MAX_BUY_PRICE:
                            hash_key = f"{i_id}_{quality}_{city_from}_{city_to}_{sell}"
                            if hash_key not in last_sent:
                                results.append((i_id, quality, city_from, city_to, buy, sell, profit_prem, profit_norm, bd, sd))
                                last_sent[hash_key] = True

    return results

async def send_flips(results, message):
    def get_safe_date(d_str):
        return d_str if (d_str and not d_str.startswith("0001")) else "1970-01-01T00:00:00"

    results.sort(key=lambda x: (min(get_safe_date(x[8]), get_safe_date(x[9])), x[6]), reverse=True)
    
    for item_id, quality, city_from, city_to, buy, sell, profit_prem, profit_norm, bd, sd in results[:30]:
        item_name = get_item_name(item_id)
        q_name = QUALITY_NAMES.get(quality, "Неизвестно")
        
        await message.answer(
            f"📦 <b>{item_name}</b> (<i>{q_name}</i>)\n"
            f"🔹 {city_from} → {city_to}\n"
            f"💰 Купити: <b>{buy:,}</b> ⏳ <i>{format_time_ago(bd)}</i>\n"
            f"💸 Продати: <b>{sell:,}</b> ⏳ <i>{format_time_ago(sd)}</i>\n\n"
            f"👑 Прибуток (Прем): <b>{profit_prem:,}</b>\n"
            f"💀 Прибуток (Без Прем): <b>{profit_norm:,}</b>",
            parse_mode=ParseMode.HTML
        )
    
    if len(results) > 30:
        await message.answer(f"⚠️ Знайдено ще {len(results) - 30} фліпів, але показано 30 найсвіжіших.")

@dp.message(Command("start"))
async def start_cmd(message: types.Message, state: FSMContext):
    await message.answer(
        "👋 Привіт! Бот для пошуку фліпів запущено.\n\n"
        "💰 Щоб не пропонувати тобі занадто дорогі предмети, давай одразу встановимо <b>максимальну суму покупки за 1 предмет</b>.\n\n"
        "Введи свій ліміт цифрами (наприклад: 300000 або 1 000 000):",
        parse_mode=ParseMode.HTML,
        reply_markup=ReplyKeyboardRemove()
    )
    await state.set_state(LimitState.waiting_for_limit)

@dp.message(F.text == "⚙️ Ліміт Скуп")
async def set_limit_start(message: types.Message, state: FSMContext):
    global MAX_BUY_PRICE
    curr_limit = f"<b>{MAX_BUY_PRICE:,}</b> срібла." if MAX_BUY_PRICE > 0 else "ще не встановлено."
    
    await message.answer(
        f"Поточний ліміт: {curr_limit}\n\n"
        f"Введи нову максимальну суму покупки за 1 предмет цифрами (наприклад: 500000):",
        parse_mode=ParseMode.HTML,
        reply_markup=ReplyKeyboardRemove()
    )
    await state.set_state(LimitState.waiting_for_limit)

@dp.message(LimitState.waiting_for_limit)
async def set_limit_finish(message: types.Message, state: FSMContext):
    global MAX_BUY_PRICE
    
    raw_text = message.text.replace(" ", "").replace(",", "").replace(".", "")
    
    if not raw_text.isdigit():
        await message.answer("❌ Будь ласка, введи тільки цифри (наприклад: 1000000). Спробуй ще раз:")
        return
    
    MAX_BUY_PRICE = int(raw_text)
    await state.clear()
    
    await message.answer(
        f"✅ Готово! Максимальна сума покупки за 1 предмет встановлена: <b>{MAX_BUY_PRICE:,}</b> срібла.\n\n"
        f"Тепер можеш сканувати ринок!",
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard
    )

@dp.message(Command("scan"))
async def scan_cmd(message: types.Message, state: FSMContext):
    global scan_running, MAX_BUY_PRICE
    
    # Якщо ліміт ще не встановлено (наприклад, після перезапуску бота)
    if MAX_BUY_PRICE <= 0:
        await start_cmd(message, state)
        return

    if scan_running:
        await message.answer("⏳ Сканування вже виконується…")
        return
        
    scan_running = True
    await message.answer(f"⏳ Сканую ринок (ліміт до {MAX_BUY_PRICE:,} ср.)...")
    results = await scan_market()
    if not results:
        await message.answer("Немає нових свіжих фліпів у межах бюджету.")
    else:
        await send_flips(results, message)
    scan
