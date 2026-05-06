import os, json, aiohttp, asyncio, re, logging, signal, html, time as time_module
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Dict
from aiogram import Bot, Dispatcher, types, F
from aiogram.enums import ParseMode, ChatAction
from aiogram.filters import Command, StateFilter
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.exceptions import TelegramUnauthorizedError

try:
    from google import genai
except ImportError:
    genai = None

# ================= КОНФІГУРАЦІЯ =================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
TOKEN = os.environ.get("BOT_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

# Ініціалізація Gemini без перевірки supported_generation_methods
gemini_client = None
AVAILABLE_GEMINI_MODELS = []
if genai and GEMINI_API_KEY:
    try:
        gemini_client = genai.Client(api_key=GEMINI_API_KEY)
        # Просто беремо всі моделі, які містять "gemini" у назві
        models_response = gemini_client.models.list()
        for m in models_response:
            name = m.name if hasattr(m, 'name') else str(m)
            if "gemini" in name.lower():
                AVAILABLE_GEMINI_MODELS.append(name)
        logger.info(f"Знайдено моделей Gemini: {len(AVAILABLE_GEMINI_MODELS)}")
        if not AVAILABLE_GEMINI_MODELS:
            logger.warning("Не знайдено жодної моделі Gemini. AI вимкнено.")
            gemini_client = None
    except Exception as e:
        logger.error(f"Помилка ініціалізації Gemini: {e}")
        gemini_client = None

bot = Bot(token=TOKEN) if TOKEN else None
dp = Dispatcher(storage=MemoryStorage())

items_data = {}
is_db_ready = False
http_session: Optional[aiohttp.ClientSession] = None
scan_semaphore = asyncio.Semaphore(5)
history_cache: Dict[str, dict] = {}
price_cache: Dict[str, list] = {}
price_cache_time: float = 0
CACHE_PRICE_TTL = 60
is_shutting_down = False
last_scan_time: Dict[int, float] = {}

CACHE_TTL = 3600
CITIES = ["Bridgewatch", "Martlock", "Lymhurst", "Thetford", "Fort Sterling", "Caerleon", "Brecilien", "Black Market"]
CITY_EMOJIS = {"Lymhurst":"🟢","Martlock":"🔵","Caerleon":"🔴","Thetford":"🟣","Bridgewatch":"🟠","Fort Sterling":"⚪","Brecilien":"🌸","Black Market":"⚫"}
QUALITY_NAMES = {1:"Обычное", 2:"Хорошее", 3:"Выдающееся", 4:"Отличное", 5:"Шедевр"}
TRASH = ["Знаток ","Мастер ","Великий мастер ","Старейшина ","Ученик ","Новичок "]
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

BLACKLIST_KEYWORDS = ["OFF_BOOK", "OFF_ORB"]

class BotState(StatesGroup):
    waiting_for_buy_limit = State()
    waiting_for_profit_limit = State()
    picking_from = State()
    picking_to = State()
    calc_count = State()
    calc_buy = State()
    calc_sell = State()
    confirm_reset = State()

# ================= СЛУЖБОВІ ФУНКЦІЇ =================
def is_blacklisted(unique_name):
    name = unique_name.upper()
    env_blacklist = os.environ.get("BLACKLIST", "")
    for w in env_blacklist.split(","):
        w = w.strip().upper()
        if w and w in name:
            return True
    for w in BLACKLIST_KEYWORDS:
        if w in name:
            return True
    return False

async def safe_delete(msg):
    try: await msg.delete()
    except: pass

def get_item_icon(unique_name):
    un = unique_name.lower()
    if any(x in un for x in ["hood", "cowl", "helmet", "cap"]): return "🪖"
    if any(x in un for x in ["armor", "jacket", "robe", "garb"]): return "🧥"
    if any(x in un for x in ["shoes", "boots", "sandals"]): return "🥾"
    if any(x in un for x in ["sword", "axe", "bow", "staff", "hammer", "mace", "dagger", "spear", "glove"]): return "⚔️"
    if "bag" in un: return "🎒"
    if "cape" in un: return "🧣"
    if "mount" in un: return "🐴"
    return "📦"

def fmt_t(s):
    try:
        if not s: return "??"
        dt = datetime.fromisoformat(s.split(".")[0].replace("Z", "")).replace(tzinfo=timezone.utc)
        m = int((datetime.now(timezone.utc) - dt).total_seconds() / 60)
        return f"{m}м" if m < 60 else f"{m//60}г"
    except: return "??"

async def get_item_liquidity(item_id, city, quality):
    global http_session, history_cache
    if not http_session or http_session.closed or is_shutting_down:
        return 0, 0
    cache_key = f"{item_id}|{city}|{quality}"
    now = datetime.now(timezone.utc)
    if cache_key in history_cache and (now - history_cache[cache_key]['time']).total_seconds() < CACHE_TTL:
        return history_cache[cache_key]['volume'], history_cache[cache_key]['avg_p']
    url = f"https://europe.albion-online-data.com/api/v2/stats/history/{item_id}?locations={city}&time-series=1&qualities={quality}"
    async with scan_semaphore:
        try:
            async with http_session.get(url, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data and isinstance(data, list) and len(data) > 0:
                        history = data[0].get('data', [])
                        vol_24h, vol_total = 0, 0
                        price_vol_24h, price_vol_total = 0, 0
                        entries_24h, entries_total = 0, 0
                        for day in history:
                            v = day.get('item_count', 0)
                            p = day.get('avg_price') or day.get('average_price', 0)
                            if v <= 0 or p <= 0: continue
                            try:
                                ts = datetime.fromisoformat(day['timestamp'].replace("Z", "+00:00"))
                                is_recent = (now - ts).total_seconds() <= 86400
                            except: is_recent = False
                            if is_recent:
                                vol_24h += v
                                price_vol_24h += (p * v)
                                entries_24h += 1
                            vol_total += v
                            price_vol_total += (p * v)
                            entries_total += 1
                        if vol_24h > 0:
                            res_vol = vol_24h
                            res_p = int(price_vol_24h / vol_24h)
                        elif vol_total > 0:
                            res_vol = int(vol_total / max(entries_total, 1))
                            res_p = int(price_vol_total / vol_total)
                        else:
                            res_vol, res_p = 0, 0
                        history_cache[cache_key] = {'volume': res_vol, 'avg_p': res_p, 'time': now}
                        return res_vol, res_p
        except Exception: pass
    return 0, 0

async def download_items():
    global items_data, is_db_ready, http_session
    try:
        async with http_session.get(
            "https://raw.githubusercontent.com/ao-data/ao-bin-dumps/master/formatted/items.json",
            timeout=60
        ) as r:
            if r.status == 200:
                data = await r.json(content_type=None)
                allowed = ["weapon","armor","plate","leather","cloth","bag","cape","potion","meal","mount","tool","offhand"]
                items_data = {
                    i["UniqueName"]: i
                    for i in data
                    if i.get("UniqueName","").startswith(("T4_", "T5_", "T6_", "T7_", "T8_"))
                    and any(x in i.get("UniqueName","").lower() for x in allowed)
                    and not is_blacklisted(i.get("UniqueName",""))
                }
                is_db_ready = True
                logger.info(f"Базу предметів завантажено: {len(items_data)} позицій")
    except Exception as e:
        logger.error(f"Помилка завантаження предметів: {e}")
# ================= Частина 2 (оновлена) =================

import re, json, asyncio, logging, html, time as time_module
from datetime import datetime, timezone

# ... (усі імпорти вже є у частині 1)

AI_ANALYSIS_PROMPT = """Ти — фінансовий аналітик ринку Albion Online. Проаналізуй наведені нижче ринкові пропозиції та вибери 15 найкращих для перепродажу.

Критерії відбору:
1. Адекватність ціни: ціна продажу не повинна бути завищеною в кілька разів порівняно з середньою історією. Такі пропозиції — пастки, їх ігноруй.
2. Попит: віддавай перевагу предметам із більшим обсягом продажів на день. Але для дуже дорогих товарів (ціна > 100,000) низький попит є нормою.
3. Прибутковість: чистий прибуток повинен бути не менше 4000 срібла, але вище — краще.
4. Ризики: враховуй свіжість цін. Якщо ціни старі (більше 12 годин), ризик вищий.
5. Різноманітність: намагайся відбирати різні предмети, а не один і той самий з різною якістю.
6. Бюджет гравця: {buy_limit} срібла. Не пропонуй предмети дорожчі за бюджет. Якщо бюджет = 0 – обмежень немає.

Дані для аналізу (JSON):
{data}

Поверни ТІЛЬКИ JSON-масив із 15 об'єктів (або менше, якщо хороших пропозицій менше). Формат кожного об'єкта:
{{
  "item_id": "рядок (назва предмета)",
  "from_city": "рядок (місто купівлі)",
  "to_city": "рядок (місто продажу)",
  "buy_price": число,
  "sell_price": число,
  "profit": число (чистий прибуток),
  "volume": число (обсяг за добу),
  "reason": "рядок (коротке пояснення вибору українською)"
}}

НЕ додавай жодних коментарів поза JSON. Відповідь має починатися з '[' і закінчуватися ']'."""

async def ai_scan_logic(d, f_c=None, t_c=None):
    if not gemini_client or not AVAILABLE_GEMINI_MODELS:
        return None

    raw_list = await scan_logic(d, f_c, t_c, ai_mode=True)
    if not raw_list:
        return []

    simplified_data = []
    for item in raw_list:
        item_name = items_data.get(item['id'], {}).get("LocalizedNames", {}).get("RU-RU", item['id'])
        item_name = re.sub(r'\s*\([^)]*\)', '', item_name)
        simplified_data.append({
            "item_name": item_name,
            "item_id": item['id'],
            "quality": QUALITY_NAMES.get(item['q'], 'Обычное'),
            "from_city": item['from'],
            "to_city": item['to'],
            "buy_price": item['buy'],
            "sell_price": item['sell'],
            "profit": item['p_n'],
            "volume": item['vol'],
            "avg_price": item['avg_p'],
            "buy_age": item.get('bd', '??'),
            "sell_age": item.get('sd', '??')
        })

    buy_limit = d.get("buy_limit", 0)
    prompt = AI_ANALYSIS_PROMPT.replace("{data}", json.dumps(simplified_data, ensure_ascii=False, indent=2))
    prompt = prompt.replace("{buy_limit}", str(buy_limit))

    response_text = None
    priority_models = sorted(
        AVAILABLE_GEMINI_MODELS,
        key=lambda x: ("flash" in x, "pro" in x),
        reverse=True
    )
    for model_name in priority_models:
        logger.info(f"AI: спроба з моделлю {model_name}")
        await asyncio.sleep(8)   # захисна пауза
        try:
            response = await asyncio.to_thread(
                gemini_client.models.generate_content,
                model=model_name,
                contents=prompt,
            )
            response_text = response.text
            logger.info(f"AI: успіх з {model_name}")
            break
        except Exception as e:
            err_str = str(e)
            if "429" in err_str:
                logger.warning(f"429 від {model_name}, чекаємо 10 секунд...")
                await asyncio.sleep(10)
            else:
                logger.warning(f"AI помилка з {model_name}: {e}")

    if not response_text:
        return None

    try:
        start = response_text.find('[')
        end = response_text.rfind(']')
        if start != -1 and end != -1:
            clean_json = response_text[start:end+1]
            ai_result = json.loads(clean_json)
        else:
            raise ValueError("JSON not found")

        final_list = []
        for ai_item in ai_result:
            orig = next((item for item in raw_list if item['id'] == ai_item.get('item_id')), None)
            if orig:
                orig['ai_reason'] = ai_item.get('reason', '')
                final_list.append(orig)

        if len(final_list) < 15:
            existing_ids = {item['id'] for item in final_list}
            for item in raw_list:
                if item['id'] not in existing_ids and len(final_list) < 15:
                    final_list.append(item)
                    existing_ids.add(item['id'])
        return final_list[:15]
    except Exception as e:
        logger.error(f"AI помилка обробки JSON: {e}")
        return None

async def fetch_prices_with_cache(item_ids, cities):
    global price_cache, price_cache_time
    now = time_module.time()
    if price_cache and (now - price_cache_time) < CACHE_PRICE_TTL:
        logger.info("Використовуємо кеш цін")
        return price_cache.get('data', [])
    all_data = []
    for i in range(0, len(item_ids), 50):
        chunk = item_ids[i:i+50]
        url = f"https://europe.albion-online-data.com/api/v2/stats/prices/{','.join(chunk)}?locations={','.join(cities)}"
        async with scan_semaphore:
            try:
                async with http_session.get(url, timeout=15) as resp:
                    if resp.status == 200:
                        all_data.extend(await resp.json())
            except Exception as e:
                logger.error(f"Помилка запиту цін: {e}")
    price_cache = {'data': all_data, 'time': now}
    price_cache_time = now
    return all_data

async def scan_logic(d, f_c=None, t_c=None, ai_mode=False):
    if not items_data or not http_session or is_shutting_down:
        return []

    pre_res = []
    # 💎 Фікс: для AI теж використовуємо реальні бюджет і профіт
    b_l = d.get("buy_limit", 0)
    p_l = d.get("profit_limit", 4000)
    ext = d.get("extra", False) and not ai_mode
    check_liq = d.get("check_liq", False) and not ai_mode
    MAX_AGE_MINUTES = 720 if not ai_mode else 1440

    i_list = list(items_data.keys())
    cities = [f_c, t_c] if f_c and t_c else CITIES

    data = await fetch_prices_with_cache(i_list, cities)
    if not data:
        return []

    now = datetime.now(timezone.utc)
    grouped = {}
    for e in data:
        k = f"{e['item_id']}|{e['quality']}"
        grouped.setdefault(k, {})[e['city']] = e

    for k, c_d in grouped.items():
        i_id, q = k.split("|")
        srcs = [f_c] if f_c else [c for c in c_d if c != "Black Market"]
        for sc in srcs:
            if sc not in c_d:
                continue
            buy = c_d[sc].get('sell_price_min', 0)
            if buy <= 500:
                continue
            if b_l > 0 and buy > b_l:
                continue
            try:
                b_dt = datetime.fromisoformat(c_d[sc]['sell_price_min_date'].split(".")[0]).replace(tzinfo=timezone.utc)
                age_b = (now - b_dt).total_seconds() / 60
                if age_b > MAX_AGE_MINUTES:
                    continue
            except:
                continue
            targets = [t_c] if t_c else [c for c in c_d if c != sc]
            for tc in targets:
                if tc not in c_d:
                    continue
                is_bm = (tc == "Black Market")
                sell = c_d[tc].get('buy_price_max' if is_bm else 'sell_price_min', 0)
                if sell <= buy:
                    continue
                try:
                    sk = 'buy_price_max_date' if is_bm else 'sell_price_min_date'
                    s_dt = datetime.fromisoformat(c_d[tc][sk].split(".")[0]).replace(tzinfo=timezone.utc)
                    age_s = (now - s_dt).total_seconds() / 60
                    if age_s > MAX_AGE_MINUTES:
                        continue
                except:
                    continue
                tax = 0.91 if is_bm else 0.895
                p_n = int(sell * tax - buy)
                p_p = int(sell * (tax + 0.04) - buy)
                if p_n >= p_l:
                    if ext and (age_b > 30 or age_s > 30):
                        continue
                    pre_res.append({
                        'id': i_id,
                        'q': int(q),
                        'from': sc,
                        'to': tc,
                        'buy': buy,
                        'sell': sell,
                        'p_p': p_p,
                        'p_n': p_n,
                        'bd': c_d[sc]['sell_price_min_date'],
                        'sd': c_d[tc][sk]
                    })

    if ai_mode:
        # Спеціальна обробка для AI: обов'язковий антифейк
        pre_res.sort(key=lambda x: x['p_n'], reverse=True)
        top_ai_candidates = []
        for item in pre_res[:40]:               # ← зменшено до 40
            vol, avg_p = await get_item_liquidity(item['id'], item['to'], item['q'])
            # Антифейк: якщо ціна продажу в 4+ рази вища за середню — ігноруємо
            if avg_p > 0 and item['sell'] > (avg_p * 4):
                logger.debug(f"AI відкинуто {item['id']}: sell={item['sell']} avg={avg_p}")
                continue
            item['vol'] = vol
            item['avg_p'] = avg_p
            top_ai_candidates.append(item)
        return top_ai_candidates

    # Звичайний режим (без змін)
    logger.info(f"Кандидатів після фільтрації цін: {len(pre_res)}")
    pre_res.sort(key=lambda x: x['p_n'], reverse=True)
    candidates_for_liquidity = pre_res[:150]
    enriched = []
    for item in candidates_for_liquidity:
        vol, avg_p = await get_item_liquidity(item['id'], item['to'], item['q'])
        if not check_liq and vol == 0:
            continue
        if avg_p > 0 and item['sell'] > (avg_p * 4):
            continue
        if check_liq:
            min_vol = 2 if item['buy'] > 100000 else 5
            if vol < min_vol:
                continue
        item['vol'] = vol
        item['avg_p'] = avg_p
        item['real_profit'] = item['p_n'] * min(vol, 10)
        enriched.append(item)

    dedup = {}
    for item in enriched:
        key = f"{item['id']}|{item['q']}"
        if key not in dedup or item['real_profit'] > dedup[key]['real_profit']:
            dedup[key] = item
    final_list = sorted(dedup.values(), key=lambda x: x['real_profit'], reverse=True)[:15]
    d['last_results'] = final_list
    return final_list

async def disp_res(msg: types.Message, res: list, d: dict):
    if not res:
        await msg.answer("📭 Нічого не знайдено.")
        return
    await msg.answer(f"🔎 Знайдено <b>{len(res)}</b> результатів:", parse_mode=ParseMode.HTML)
    messages, full_text = [], ""
    for idx, r in enumerate(res, 1):
        id_parts = r['id'].split("@")
        b_id = id_parts[0]
        tier = b_id.split('_')[0][1:]
        enc = id_parts[1] if len(id_parts) > 1 else "0"
        icon = get_item_icon(b_id)
        name = items_data.get(b_id, {}).get("LocalizedNames", {}).get("RU-RU", b_id)
        name = re.sub(r'\s*\([^)]*\)', '', html.escape(name.upper()))
        for t in TRASH:
            name = name.replace(t, "")
        tbd = fmt_t(r.get('bd'))
        tsd = fmt_t(r.get('sd'))
        liq = r.get('vol', 0)
        lbl = "🔥" if liq > 100 else ("⚡" if liq > 30 else ("✅" if liq > 5 else "🐢"))
        avg_str = f"{r['avg_p']:,}" if r['avg_p'] > 0 else "???"
        ai_reason = r.get('ai_reason', '')
        reason_block = f"\n🧠 <i>AI: {ai_reason}</i>" if ai_reason else ""

        item_block = (
            f"{idx}) {icon} <b>{name}</b> [{tier}.{enc}]\n"
            f"✨ {QUALITY_NAMES.get(r['q'], 'Обычное')}\n"
            f"📥 {CITY_EMOJIS[r['from']]} {r['buy']:,} | 🕒 {tbd}\n"
            f"📤 {CITY_EMOJIS[r['to']]} {r['sell']:,} | 🕒 {tsd}\n"
            f"<pre>"
            f"Прибуток:\n"
            f"{f'👑 {r['p_p']:,}':<17} Попит: {lbl} {liq} шт/д\n"
            f"{f'💀 {r['p_n']:,}':<17} Сер.ціна: {avg_str}"
            f"</pre>"
            f"{reason_block}\n"
            f"───────────────────\n\n"
        )
        if len(full_text) + len(item_block) > 3900:
            messages.append(full_text)
            full_text = item_block
        else:
            full_text += item_block
    if full_text:
        messages.append(full_text)
    for t in messages:
        await msg.answer(t, parse_mode=ParseMode.HTML)
    await msg.answer(f"📊 Усього знайдено <b>{len(res)}</b> позицій.", parse_mode=ParseMode.HTML)
# ================= КЛАВІАТУРА =================
def get_main_kb(d):
    mode = d.get("mode")
    budget = d.get("buy_limit", 0)
    searched = d.get("has_searched", False)
    ai_active = d.get("ai_mode", False)
    has_results = bool(d.get("last_results"))

    m_btn = "Режим: 🌍 Всі міста" if mode == "all" else ("Режим: 📍 Шлях" if mode == "custom" else "🗺 Режим")
    kb = []
    if budget > 0 and mode:
        kb.append([KeyboardButton(text="🚀 Запустити сканер")])
    if searched or ai_active:
        kb.append([KeyboardButton(text=m_btn), KeyboardButton(text=f"⚡ 30хв: {'ON' if d.get('extra') else 'OFF'}")])
        kb.append([
            KeyboardButton(text=f"📊 Попит Ліміт: {'ON' if d.get('check_liq') else 'OFF'}"),
            KeyboardButton(text="🧮 Калькулятор")
        ])
        kb.append([KeyboardButton(text="💰 Бюджет"), KeyboardButton(text="🔄 Скинути")])
        kb.append([KeyboardButton(text=f"🧠 AI Аналіз: {'ON' if ai_active else 'OFF'}")])
        if has_results:
            kb.append([KeyboardButton(text="🔄 Оновити пошук")])
    else:
        kb.append([KeyboardButton(text="💰 Бюджет"), KeyboardButton(text=m_btn)])
        kb.append([KeyboardButton(text="🧮 Калькулятор"), KeyboardButton(text="❓ Допомога")])
        kb.append([KeyboardButton(text=f"🧠 AI Аналіз: {'ON' if ai_active else 'OFF'}")])
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

# ================= ОБРОБНИКИ =================
@dp.message(Command("start"), StateFilter('*'))
async def cmd_start(m: types.Message, state: FSMContext):
    await state.clear()
    await m.answer("👋 <b>Привіт! Я Albion Trade Bot.</b>\nВкажіть Бюджет та Режим.", parse_mode=ParseMode.HTML, reply_markup=get_main_kb({}))

@dp.message(F.text == "🚀 Запустити сканер", StateFilter('*'))
async def main_search(m: types.Message, state: FSMContext):
    d = await state.get_data()
    chat_id = m.chat.id

    now = time_module.time()
    last = last_scan_time.get(chat_id, 0)
    if now - last < 10:
        await m.answer("⏳ Зачекайте 10 секунд між скануваннями.")
        return
    last_scan_time[chat_id] = now

    if not is_db_ready:
        return await m.answer("⏳ База предметів ще завантажується...")

    ai_active = d.get("ai_mode", False)
    if not ai_active:
        await bot.send_chat_action(chat_id, ChatAction.TYPING)
        s_msg = await m.answer("🔍 Шукаю вигідні маршрути...")
        res = await scan_logic(d, d.get('f_c'), d.get('t_c'))
        await safe_delete(s_msg)
        await state.update_data(has_searched=True, last_results=res)
        d['has_searched'] = True
        await disp_res(m, res, d)
        await m.answer("✅ Сканування завершено", reply_markup=get_main_kb(d))
        return

    # AI-режим
    if not gemini_client:
        await m.answer("❌ GEMINI_API_KEY не налаштовано.")
        return
    await bot.send_chat_action(chat_id, ChatAction.TYPING)
    s_msg = await m.answer("🧠 AI аналізує ринок...")
    res = await ai_scan_logic(d, d.get('f_c'), d.get('t_c'))
    await safe_delete(s_msg)
    if res is None:
        await m.answer("⚠️ AI не зміг обробити дані. Використовую звичайний аналіз.")
        res = await scan_logic(d, d.get('f_c'), d.get('t_c'))
        if not res:
            await m.answer("📭 Нічого не знайдено.", reply_markup=get_main_kb(d))
            return
    elif not res:
        await m.answer("📭 AI не знайшов хороших пропозицій.", reply_markup=get_main_kb(d))
        return
    await state.update_data(has_searched=True, last_results=res)
    d['has_searched'] = True
    await disp_res(m, res, d)
    await m.answer("✅ AI-аналіз завершено", reply_markup=get_main_kb(d))

@dp.message(F.text == "🔄 Оновити пошук", StateFilter('*'))
async def refresh_search(m: types.Message, state: FSMContext):
    d = await state.get_data()
    last_res = d.get("last_results")
    if not last_res:
        await m.answer("Немає попередніх результатів для оновлення. Запустіть новий пошук.")
        return
    item_ids = list({r['id'] for r in last_res})
    cities = list({r['from'] for r in last_res} | {r['to'] for r in last_res})
    global price_cache, price_cache_time
    price_cache = {}
    data = await fetch_prices_with_cache(item_ids, cities)
    if not data:
        await m.answer("❌ Не вдалося отримати свіжі ціни.")
        return
    updated = []
    for r in last_res:
        new_buy = None
        new_sell = None
        for e in data:
            if e['item_id'] == r['id'] and e['city'] == r['from']:
                new_buy = e.get('sell_price_min', 0)
            if e['item_id'] == r['id'] and e['city'] == r['to']:
                is_bm = (r['to'] == "Black Market")
                new_sell = e.get('buy_price_max' if is_bm else 'sell_price_min', 0)
        if new_buy and new_sell:
            tax = 0.91 if r['to'] == "Black Market" else 0.895
            new_p_n = int(new_sell * tax - new_buy)
            if new_p_n > 0:
                r['buy'] = new_buy
                r['sell'] = new_sell
                r['p_n'] = new_p_n
                r['p_p'] = int(new_sell * (tax + 0.04) - new_buy)
                updated.append(r)
    if updated:
        await disp_res(m, updated, d)
        await m.answer("🔄 Ціни оновлено (кеш скинуто).", reply_markup=get_main_kb(d))
    else:
        await m.answer("😕 Пропозиції більше не актуальні. Запустіть новий пошук.")

@dp.message(F.text.startswith("Режим:") | (F.text == "🗺 Режим"), StateFilter('*'))
async def choose_mode(m: types.Message, state: FSMContext):
    await m.answer("Оберіть режим:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎲 Всі міста", callback_data="set_mode_all")],
        [InlineKeyboardButton(text="📍 Шлях", callback_data="set_mode_custom")]
    ]))

@dp.callback_query(F.data.startswith("set_mode_"))
async def set_mode_cb(cb: types.CallbackQuery, state: FSMContext):
    m_type = cb.data.split("_")[2]
    if m_type == "all":
        await state.update_data(mode="all")
        await cb.message.edit_text("🌍 Режим: Всі міста!")
        await cb.message.answer("Оновлено", reply_markup=get_main_kb(await state.get_data()))
    else:
        await state.set_state(BotState.picking_from)
        await cb.message.edit_text("Звідки:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"{CITY_EMOJIS[c]} {c}", callback_data=f"city_{c}")] for c in CITIES if c != "Black Market"
        ]))

@dp.callback_query(F.data.startswith("city_"))
async def city_pick(cb: types.CallbackQuery, state: FSMContext):
    curr = await state.get_state()
    c = cb.data.split("_")[1]
    if curr == BotState.picking_from.state:
        await state.update_data(f_c=c)
        await state.set_state(BotState.picking_to)
        await cb.message.edit_text(f"З: {c}. Куди:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"{CITY_EMOJIS[ci]} {ci}", callback_data=f"city_{ci}")] for ci in CITIES if ci != c
        ]))
    elif curr == BotState.picking_to.state:
        await state.update_data(t_c=c, mode="custom")
        await state.set_state(None)
        await cb.message.edit_text(f"📍 Шлях збережено!")
        await cb.message.answer("Оновлено", reply_markup=get_main_kb(await state.get_data()))

@dp.message(F.text == "💰 Бюджет", StateFilter('*'))
async def limit_menu(m: types.Message, state: FSMContext):
    d = await state.get_data()
    await m.answer(
        f"⚙️ Бюджет: {d.get('buy_limit',0):,}\n📈 Профіт: {d.get('profit_limit',4000):,}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💰 Бюджет", callback_data="set_limit_buy"),
             InlineKeyboardButton(text="📈 Профіт", callback_data="set_limit_profit")]
        ])
    )

@dp.callback_query(F.data.startswith("set_limit_"))
async def set_limit_cb(cb: types.CallbackQuery, state: FSMContext):
    t = cb.data.split("_")[2]
    await state.set_state(BotState.waiting_for_buy_limit if t == "buy" else BotState.waiting_for_profit_limit)
    await cb.message.answer("Введіть число:", reply_markup=ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="❌ Скасувати")]], resize_keyboard=True
    ))
    await cb.answer()

@dp.message(F.text == "🧮 Калькулятор", StateFilter('*'))
async def calc_start(m: types.Message, state: FSMContext):
    await state.set_state(BotState.calc_count)
    await m.answer("📦 Кількість:", reply_markup=ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="❌ Скасувати")]], resize_keyboard=True
    ))

@dp.message(F.text == "🔄 Скинути", StateFilter('*'))
async def reset_confirm(m: types.Message, state: FSMContext):
    await state.set_state(BotState.confirm_reset)
    await m.answer("Скинути всі дані?", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Так", callback_data="reset_yes"),
         InlineKeyboardButton(text="❌ Ні", callback_data="reset_no")]
    ]))

@dp.callback_query(F.data.startswith("reset_"), StateFilter(BotState.confirm_reset))
async def reset_action(cb: types.CallbackQuery, state: FSMContext):
    if cb.data == "reset_yes":
        await state.clear()
        await cb.message.edit_text("🔄 Скинуто.")
        await cb.message.answer("Почнемо заново?", reply_markup=get_main_kb({}))
    else:
        await cb.message.edit_text("🚫 Скасовано.")
    await state.set_state(None)

@dp.message(StateFilter(BotState.waiting_for_buy_limit, BotState.waiting_for_profit_limit,
                         BotState.calc_count, BotState.calc_buy, BotState.calc_sell))
async def numeric_handler(m: types.Message, state: FSMContext):
    if m.text == "❌ Скасувати":
        await state.set_state(None)
        return await m.answer("Скасовано", reply_markup=get_main_kb(await state.get_data()))
    try:
        v = int(m.text.replace(" ", ""))
        curr = await state.get_state()
        d = await state.get_data()
        if "buy_limit" in str(curr):
            await state.update_data(buy_limit=v)
            await state.set_state(None)
        elif "profit_limit" in str(curr):
            await state.update_data(profit_limit=v)
            await state.set_state(None)
        elif "calc_count" in str(curr):
            await state.update_data(c=v)
            await state.set_state(BotState.calc_buy)
            return await m.answer("📥 Ціна КУПІВЛІ:")
        elif "calc_buy" in str(curr):
            await state.update_data(b=v)
            await state.set_state(BotState.calc_sell)
            return await m.answer("📤 Ціна ПРОДАЖУ:")
        elif "calc_sell" in str(curr):
            await state.set_state(None)
            p_p = int((v * 0.935) - d['b']) * d['c']
            p_n = int((v * 0.895) - d['b']) * d['c']
            await m.answer(f"📊 Результат:\n👑 Пр: <b>{p_p:,}</b>\n💀 Пр: <b>{p_n:,}</b>",
                           reply_markup=get_main_kb(d), parse_mode=ParseMode.HTML)
            return
        await m.answer(f"✅ Збережено: {v:,}", reply_markup=get_main_kb(await state.get_data()))
    except ValueError:
        await m.answer("❌ Введіть ціле число!")

@dp.message(F.text.regexp(r"⚡ 30хв:|📊 Попит Ліміт:|🧠 AI Аналіз:"), StateFilter('*'))
async def toggles(m: types.Message, state: FSMContext):
    d = await state.get_data()
    if "⚡" in m.text:
        key = "extra"
    elif "📊" in m.text:
        key = "check_liq"
    else:
        key = "ai_mode"
    val = not d.get(key, False)
    await state.update_data({key: val})
    msg = "Увімкнено" if val else "Вимкнено"
    await m.answer(f"{msg}.", reply_markup=get_main_kb(await state.get_data()))

# ================= ЗАПУСК =================
async def main():
    global http_session
    if not TOKEN:
        logger.critical("BOT_TOKEN не задано! Зупиняюсь.")
        return
    http_session = aiohttp.ClientSession(headers=HEADERS)
    asyncio.create_task(download_items())
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    except TelegramUnauthorizedError:
        logger.critical("❌ Токен бота недійсний або відкликаний. Перевірте BOT_TOKEN.")
    except Exception as e:
        logger.exception(f"Критична помилка під час запуску: {e}")
    finally:
        if http_session and not http_session.closed:
            await http_session.close()
        if bot and hasattr(bot, 'session') and bot.session:
            await bot.session.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Бот зупинений вручну.")