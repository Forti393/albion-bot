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

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
TOKEN = os.environ.get("BOT_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

gemini_client = None
AVAILABLE_GEMINI_MODELS = []
if genai and GEMINI_API_KEY:
    try:
        gemini_client = genai.Client(api_key=GEMINI_API_KEY)
        models_response = gemini_client.models.list()
        for m in models_response:
            name = m.name if hasattr(m, 'name') else str(m)
            if "gemini" in name.lower():
                AVAILABLE_GEMINI_MODELS.append(name)
        if not AVAILABLE_GEMINI_MODELS:
            gemini_client = None
    except Exception as e:
        logger.error(f"Помилка Gemini: {e}")
        gemini_client = None

bot = Bot(token=TOKEN) if TOKEN else None
dp = Dispatcher(storage=MemoryStorage())

items_data = {}
is_db_ready = False
http_session: Optional[aiohttp.ClientSession] = None
scan_semaphore = asyncio.Semaphore(5)
history_cache = {}
history_fallback_cache = {}
price_cache = {}
price_cache_time = 0.0
CACHE_PRICE_TTL = 60
is_shutting_down = False
last_scan_time: Dict[int, float] = {}

CACHE_TTL = 3600
FALLBACK_CACHE_TTL = 7200

async def cache_cleaner():
    while not is_shutting_down:
        await asyncio.sleep(600)
        now = datetime.now(timezone.utc)
        for cache, ttl in [(history_cache, CACHE_TTL), (history_fallback_cache, FALLBACK_CACHE_TTL)]:
            expired = [k for k, v in cache.items() if (now - v.get('time', now)).total_seconds() > ttl]
            for k in expired:
                del cache[k]
        if price_cache and (time_module.time() - price_cache.get('time', 0)) > CACHE_PRICE_TTL:
            price_cache.clear()

CITIES = ["Bridgewatch", "Martlock", "Lymhurst", "Thetford", "Fort Sterling", "Caerleon", "Brecilien", "Black Market"]
CITY_EMOJIS = {"Lymhurst":"🟢","Martlock":"🔵","Caerleon":"🔴","Thetford":"🟣","Bridgewatch":"🟠","Fort Sterling":"⚪","Brecilien":"🌸","Black Market":"⚫"}
QUALITY_NAMES = {1:"Обычное", 2:"Хорошее", 3:"Выдающееся", 4:"Отличное", 5:"Шедевр"}
TRASH = ["Знаток ","Мастер ","Великий мастер ","Старейшина ","Ученик ","Новичок "]
HEADERS = {"User-Agent": "Mozilla/5.0"}

BLACKLIST_KEYWORDS = ["OFF_BOOK"]

RATIO_OPTIONS = [1.5, 2.0, 2.5, 3.0]
AVG_MULTIPLIER_OPTIONS = [1.2, 1.5, 1.8, 2.0, 2.2, 2.5, 3.0, 3.5, 4.0]

class BotState(StatesGroup):
    waiting_for_buy_limit = State()
    waiting_for_profit_limit = State()
    waiting_for_max_ratio = State()
    waiting_for_avg_mult = State()
    picking_from = State()
    picking_to = State()
    picking_origin = State()
    calc_count = State()
    calc_buy = State()
    calc_sell = State()
    confirm_reset = State()
    settings_menu = State()

def is_blacklisted(unique_name):
    name = unique_name.upper()
    env_blacklist = os.environ.get("BLACKLIST", "")
    for w in env_blacklist.split(","):
        if w.strip().upper() in name:
            return True
    for w in BLACKLIST_KEYWORDS:
        if w in name:
            return True
    return False

async def safe_delete(msg):
    try: await msg.delete()
    except: pass

def get_item_icon(un):
    un = un.lower()
    if any(x in un for x in ["hood","cowl","helmet","cap"]): return "🪖"
    if any(x in un for x in ["armor","jacket","robe","garb"]): return "🧥"
    if any(x in un for x in ["shoes","boots","sandals"]): return "🥾"
    if any(x in un for x in ["sword","axe","bow","staff","hammer","mace","dagger","spear","glove"]): return "⚔️"
    if "bag" in un: return "🎒"
    if "cape" in un: return "🧣"
    if "mount" in un: return "🐴"
    return "📦"

def fmt_t(s):
    try:
        if not s: return "??"
        dt = datetime.fromisoformat(s.split(".")[0].replace("Z","")).replace(tzinfo=timezone.utc)
        m = int((datetime.now(timezone.utc)-dt).total_seconds()/60)
        return f"{m}м" if m<60 else f"{m//60}г"
    except: return "??"

async def get_item_liquidity(item_id, city, quality):
    global http_session, history_cache
    if not http_session or http_session.closed or is_shutting_down:
        return 0,0,None
    cache_key = f"{item_id}|{city}|{quality}"
    now = datetime.now(timezone.utc)
    if cache_key in history_cache and (now-history_cache[cache_key]['time']).total_seconds()<CACHE_TTL:
        e=history_cache[cache_key]; return e['volume'],e['avg_p'],"24г"
    url=f"https://europe.albion-online-data.com/api/v2/stats/history/{item_id}?locations={city}&time-series=1&qualities={quality}"
    async with scan_semaphore:
        try:
            async with http_session.get(url,timeout=10) as resp:
                if resp.status==200:
                    data=await resp.json()
                    if data and isinstance(data,list) and len(data)>0:
                        hist=data[0].get('data',[])
                        v24h=vtot=0; pv24h=pvtot=0; e24h=etot=0
                        for day in hist:
                            v=day.get('item_count',0); p=day.get('avg_price') or day.get('average_price',0)
                            if v<=0 or p<=0: continue
                            try:
                                ts=datetime.fromisoformat(day['timestamp'].replace("Z","+00:00"))
                                recent=(now-ts).total_seconds()<=86400
                            except: recent=False
                            if recent: v24h+=v; pv24h+=p*v; e24h+=1
                            vtot+=v; pvtot+=p*v; etot+=1
                        if v24h>0: res_vol=v24h; res_p=int(pv24h/v24h); per="24г"
                        elif vtot>0: res_vol=int(vtot/max(etot,1)); res_p=int(pvtot/vtot); per="24г"
                        else: res_vol=res_p=0; per=None
                        history_cache[cache_key]={'volume':res_vol,'avg_p':res_p,'time':now}
                        return res_vol,res_p,per
                else:
                    logger.debug(f"get_item_liquidity: HTTP {resp.status} для {item_id}")
        except asyncio.TimeoutError:
            logger.debug(f"Таймаут отримання історії {item_id}")
        except Exception as e:
            logger.debug(f"Помилка отримання історії {item_id}: {e}")
    return 0,0,None

async def get_item_liquidity_fallback(item_id,city,quality):
    vol,avg_p,per=await get_item_liquidity(item_id,city,quality)
    if avg_p>0: return vol,avg_p,per
    global history_fallback_cache
    cache_key=f"{item_id}|{city}|{quality}"
    now=datetime.now(timezone.utc)
    if cache_key in history_fallback_cache and (now-history_fallback_cache[cache_key]['time']).total_seconds()<FALLBACK_CACHE_TTL:
        e=history_fallback_cache[cache_key]; return e['volume'],e['avg_p'],"7д"
    url=f"https://europe.albion-online-data.com/api/v2/stats/history/{item_id}?locations={city}&time-series=7&qualities={quality}"
    async with scan_semaphore:
        try:
            async with http_session.get(url,timeout=10) as resp:
                if resp.status==200:
                    data=await resp.json()
                    if data and isinstance(data,list) and len(data)>0:
                        hist=data[0].get('data',[]); tv=0; tpv=0; ent=0
                        for day in hist:
                            v=day.get('item_count',0); p=day.get('avg_price') or day.get('average_price',0)
                            if v<=0 or p<=0: continue
                            tv+=v; tpv+=p*v; ent+=1
                        if ent>0:
                            rv=int(tv/max(ent,1)); rp=int(tpv/tv) if tv>0 else 0
                            history_fallback_cache[cache_key]={'volume':rv,'avg_p':rp,'time':now}
                            return rv,rp,"7д"
                else:
                    logger.debug(f"get_item_liquidity_fallback: HTTP {resp.status} для {item_id}")
        except asyncio.TimeoutError:
            logger.debug(f"Таймаут fallback-історії {item_id}")
        except Exception as e:
            logger.debug(f"Помилка fallback-історії {item_id}: {e}")
    history_fallback_cache[cache_key]={'volume':0,'avg_p':0,'time':now}
    return 0,0,None

async def download_items():
    global items_data, is_db_ready, http_session
    url = "https://raw.githubusercontent.com/ao-data/ao-bin-dumps/master/formatted/items.json"
    for attempt in range(3):
        try:
            async with http_session.get(url, timeout=60) as r:
                if r.status == 200:
                    data = await r.json(content_type=None)
                    if not isinstance(data, list):
                        logger.error(f"Отриманий JSON не є списком, тип: {type(data)}")
                        continue
                    logger.info(f"Отримано {len(data)} записів з GitHub")

                    # ДОДАТИ ДІАГНОСТИКУ – ПОКАЗАТИ ПЕРШИЙ ЕЛЕМЕНТ
                    if data:
                        logger.info(f"ТИП data[0]: {type(data[0])}")
                        logger.info(f"ПЕРШИЙ ЕЛЕМЕНТ JSON: {json.dumps(data[0], ensure_ascii=False)[:2000]}")
                        logger.info(f"КЛЮЧІ ПЕРШОГО ЕЛЕМЕНТУ: {list(data[0].keys())}")

                    new_items = {}
                    for i in data:
                        raw_uid = i.get("UniqueName") or i.get("@uniquename") or ""
                        uid = str(raw_uid).strip().upper()
                        if not uid:
                            continue
                        base_uid = uid.split("@")[0]
                        if not re.match(r"^T[4-8]_", base_uid):
                            continue
                        if is_blacklisted(uid):
                            continue
                        new_items[uid] = i

                    if not new_items:
                        logger.error("Не знайдено жодного предмета T4_-T8_ після фільтрації")
                        items_data = {}
                        is_db_ready = False
                        return

                    items_data = new_items
                    is_db_ready = True
                    logger.info(f"Базу предметів завантажено: {len(items_data)} позицій")
                    return
                else:
                    logger.warning(f"Спроба {attempt+1}: HTTP {r.status}")
        except Exception as e:
            logger.error(f"Спроба {attempt+1}: помилка: {e}")
        await asyncio.sleep(5)
    logger.critical("Не вдалося завантажити базу після 3 спроб")
AI_ANALYSIS_PROMPT = """Ти — фінансовий аналітик ринку Albion Online. Проаналізуй наведені нижче ринкові пропозиції та вибери 15 найкращих для перепродажу.

Критерії відбору:
1. Прибутковість: чистий прибуток (profit) повинен бути не менше 4000 срібла, але вище — краще.
2. Попит: віддавай перевагу предметам із більшим обсягом продажів на день (volume). Позначка "0 шт/д" — ризик, такі предмети рідко продаються.
3. Свіжість цін: якщо з моменту оновлення ціни купівлі або продажу минуло більше 12 годин — ризик вищий.
4. Різноманітність: намагайся відбирати різні предмети, а не той самий з різною якістю.
5. Бюджет гравця: {buy_limit} срібла. Не пропонуй предмети дорожчі за бюджет. Якщо бюджет = 0 – обмежень немає.
6. Середня ціна (avg_price) — орієнтир для визначення адекватності ціни продажу. Якщо ціна продажу значно вища за середню — це може бути пастка.

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
    if len(raw_list) <= 5:
        return raw_list[:15]
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
    priority_models = sorted(AVAILABLE_GEMINI_MODELS, key=lambda x: ("flash" in x, "pro" in x), reverse=True)
    for model_name in priority_models:
        await asyncio.sleep(8)
        try:
            response = await asyncio.to_thread(gemini_client.models.generate_content, model=model_name, contents=prompt)
            response_text = response.text
            break
        except Exception as e:
            if "429" in str(e): await asyncio.sleep(10)
    if not response_text: return None
    try:
        start = response_text.find('['); end = response_text.rfind(']')
        if start != -1 and end != -1:
            ai_result = json.loads(response_text[start:end+1])
        else: raise ValueError("JSON not found")
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
        logger.error(f"AI помилка JSON: {e}")
        return None

async def fetch_prices_with_cache(item_ids, cities):
    global price_cache, price_cache_time
    now = time_module.time()
    if price_cache and (now - price_cache_time) < CACHE_PRICE_TTL:
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
                logger.debug(f"Помилка fetch_prices: {e}")
    price_cache = {'data': all_data, 'time': now}
    price_cache_time = now
    return all_data

async def scan_logic(d, f_c=None, t_c=None, ai_mode=False):
    if not items_data or not http_session or is_shutting_down:
        return []
    pre_res = []
    b_l = d.get("buy_limit", 0)
    p_l = d.get("profit_limit", 4000)
    ext = d.get("extra", False)
    check_liq = d.get("check_liq", False)
    max_ratio = float(d.get("max_ratio", 2.0))
    max_avg_mult = float(d.get("max_avg_mult", 4.0))
    MAX_AGE_MINUTES = 300

    i_list = list(items_data.keys())
    if f_c and t_c:
        cities = [f_c, t_c]
    elif f_c:
        cities = [f_c] + [c for c in CITIES if c != f_c]
    else:
        cities = CITIES

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
            if sc not in c_d: continue
            buy = c_d[sc].get('sell_price_min', 0)
            if buy <= 500: continue
            if b_l > 0 and buy > b_l: continue
            try:
                b_dt = datetime.fromisoformat(c_d[sc]['sell_price_min_date'].split(".")[0]).replace(tzinfo=timezone.utc)
                age_b = (now - b_dt).total_seconds() / 60
                if age_b > MAX_AGE_MINUTES: continue
                if ext and age_b > 60: continue
            except: continue
            targets = [t_c] if t_c else [c for c in c_d if c != sc]
            for tc in targets:
                if tc not in c_d: continue
                is_bm = (tc == "Black Market")
                sell = c_d[tc].get('buy_price_max' if is_bm else 'sell_price_min', 0)
                if sell <= buy: continue
                if sell > buy * max_ratio: continue
                try:
                    sk = 'buy_price_max_date' if is_bm else 'sell_price_min_date'
                    s_dt = datetime.fromisoformat(c_d[tc][sk].split(".")[0]).replace(tzinfo=timezone.utc)
                    age_s = (now - s_dt).total_seconds() / 60
                    if age_s > MAX_AGE_MINUTES: continue
                    if ext and age_s > 60: continue
                except: continue
                tax = 0.91 if is_bm else 0.895
                p_n = int(sell * tax - buy)
                p_p = int(sell * (tax + 0.04) - buy)
                if p_n >= p_l:
                    pre_res.append({'id': i_id, 'q': int(q), 'from': sc, 'to': tc, 'buy': buy, 'sell': sell, 'p_p': p_p, 'p_n': p_n, 'bd': c_d[sc]['sell_price_min_date'], 'sd': c_d[tc][sk]})

    if ai_mode:
        pre_res.sort(key=lambda x: x['p_n'], reverse=True)
        top_ai = []
        for item in pre_res[:200]:
            vol, avg_p, period = await get_item_liquidity_fallback(item['id'], item['to'], item['q'])
            if avg_p == 0: continue
            if avg_p > 0 and item['sell'] > (avg_p * max_avg_mult): continue
            item['vol'] = vol; item['avg_p'] = avg_p; item['price_period'] = period
            top_ai.append(item)
        return top_ai

    logger.info(f"Кандидатів після цін: {len(pre_res)}")
    pre_res.sort(key=lambda x: x['p_n'], reverse=True)
    candidates = pre_res[:150]
    enriched = []
    for item in candidates:
        vol, avg_p, period = await get_item_liquidity_fallback(item['id'], item['to'], item['q'])
        if avg_p == 0: continue
        if check_liq:
            min_vol = 2 if item['buy'] > 100000 else 5
            if vol < min_vol: continue
        if item['sell'] > (avg_p * max_avg_mult): continue
        item['vol'] = vol; item['avg_p'] = avg_p; item['price_period'] = period
        item['real_profit'] = item['p_n'] * min(vol, 10)
        enriched.append(item)
    dedup = {}
    for item in enriched:
        key = f"{item['id']}|{item['q']}"
        if key not in dedup or item['real_profit'] > dedup[key]['real_profit']:
            dedup[key] = item
    final = sorted(dedup.values(), key=lambda x: x['real_profit'], reverse=True)[:15]
    d['last_results'] = final
    return final

async def disp_res(msg, res, d):
    if not res:
        await msg.answer("📭 Нічого не знайдено.")
        return
    await msg.answer(f"🔎 Знайдено <b>{len(res)}</b> результатів:", parse_mode=ParseMode.HTML)
    messages, full = [], ""
    for idx, r in enumerate(res, 1):
        id_parts = r['id'].split("@")
        b_id = id_parts[0]; tier = b_id.split('_')[0][1:]; enc = id_parts[1] if len(id_parts)>1 else "0"
        icon = get_item_icon(b_id)
        name = items_data.get(b_id, {}).get("LocalizedNames", {}).get("RU-RU", b_id)
        name = re.sub(r'\s*\([^)]*\)', '', html.escape(name.upper()))
        for t in TRASH: name = name.replace(t, "")
        tbd = fmt_t(r.get('bd')); tsd = fmt_t(r.get('sd'))
        liq = r.get('vol', 0)
        lbl = "🔥" if liq>100 else ("⚡" if liq>30 else ("✅" if liq>5 else "🐢"))
        avg_p = r.get('avg_p', 0); period = r.get('price_period', '')
        avg_str = f"{avg_p:,}" if avg_p>0 else "???"
        if period: avg_str += f" ({period})"
        ai_reason = r.get('ai_reason', '')
        reason_block = f"\n🧠 <i>AI: {ai_reason}</i>" if ai_reason else ""

        p_p_val = r["p_p"]
        p_n_val = r["p_n"]
        profit_line = (
            f"<pre>"
            f"Прибуток:\n"
            f"{'👑 ' + f'{p_p_val:,}':<17} Попит: {lbl} {liq} шт/д\n"
            f"{'💀 ' + f'{p_n_val:,}':<17} Сер.ціна: {avg_str}"
            f"</pre>"
        )

        block = (
            f"{idx}) {icon} <b>{name}</b> [{tier}.{enc}]\n"
            f"✨ {QUALITY_NAMES.get(r['q'], 'Обычное')}\n"
            f"📥 {CITY_EMOJIS[r['from']]} {r['buy']:,} | 🕒 {tbd}\n"
            f"📤 {CITY_EMOJIS[r['to']]} {r['sell']:,} | 🕒 {tsd}\n"
            f"{profit_line}"
            f"{reason_block}\n"
            f"───────────────────\n\n"
        )
        if len(full)+len(block)>3900:
            messages.append(full)
            full = block
        else:
            full += block
    if full:
        messages.append(full)
    for t in messages:
        await msg.answer(t, parse_mode=ParseMode.HTML)
    await msg.answer(f"📊 Усього знайдено <b>{len(res)}</b> позицій.", parse_mode=ParseMode.HTML)
def get_main_kb(d):
    mode = d.get("mode")
    budget = d.get("buy_limit", 0)
    searched = d.get("has_searched", False)
    has_results = bool(d.get("last_results"))
    m_btn = "Режим: 🌍 Всі міста" if mode=="all" else ("Режим: 📍 Шлях" if mode=="custom" else ("Режим: 📍 З міста" if mode=="origin" else "🗺 Режим"))
    kb = []
    if budget>0 and mode: kb.append([KeyboardButton(text="🚀 Запустити сканер")])
    if searched:
        kb.append([KeyboardButton(text=m_btn)])
        kb.append([KeyboardButton(text="⚙️ Налаштування"), KeyboardButton(text="🧮 Калькулятор")])
        kb.append([KeyboardButton(text="💰 Бюджет"), KeyboardButton(text="🔄 Скинути")])
        if has_results: kb.append([KeyboardButton(text="🔄 Оновити пошук")])
    else:
        kb.append([KeyboardButton(text="💰 Бюджет"), KeyboardButton(text=m_btn)])
        kb.append([KeyboardButton(text="⚙️ Налаштування"), KeyboardButton(text="🧮 Калькулятор")])
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

def get_settings_kb(d):
    extra = d.get("extra", False)
    check_liq = d.get("check_liq", False)
    ai_mode = d.get("ai_mode", False)
    max_ratio = float(d.get("max_ratio", 2.0))
    max_avg_mult = float(d.get("max_avg_mult", 4.0))
    kb = [
        [KeyboardButton(text=f"⏱ 1 година: {'ON' if extra else 'OFF'}")],
        [KeyboardButton(text=f"📊 Попит Ліміт: {'ON' if check_liq else 'OFF'}")],
        [KeyboardButton(text=f"🧠 AI Аналіз: {'ON' if ai_mode else 'OFF'}")],
        [KeyboardButton(text=f"📈 Макс. множник: ×{max_ratio}")],
        [KeyboardButton(text=f"📉 Антифейк ×: {max_avg_mult}")],
        [KeyboardButton(text="🔙 Назад")]
    ]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

@dp.message(Command("start"), StateFilter('*'))
async def cmd_start(m, state: FSMContext):
    await state.clear()
    await m.answer("👋 <b>Привіт!</b> Вкажіть Бюджет та Режим.", parse_mode=ParseMode.HTML, reply_markup=get_main_kb({}))

@dp.message(F.text == "⚙️ Налаштування", StateFilter('*'))
async def open_settings(m, state: FSMContext):
    d = await state.get_data()
    await state.set_state(BotState.settings_menu)
    await m.answer("Оберіть параметр:", reply_markup=get_settings_kb(d))

@dp.message(StateFilter(BotState.settings_menu))
async def handle_settings(m, state: FSMContext):
    text = m.text; d = await state.get_data()
    if text == "🔙 Назад":
        await state.set_state(None); await m.answer("Головне меню.", reply_markup=get_main_kb(d)); return
    if text.startswith("⏱"):
        val = not d.get("extra", False); await state.update_data(extra=val)
        await m.answer(f"1 година: {'ON' if val else 'OFF'}.", reply_markup=get_settings_kb(await state.get_data()))
    elif text.startswith("📊"):
        val = not d.get("check_liq", False); await state.update_data(check_liq=val)
        await m.answer(f"Попит Ліміт: {'ON' if val else 'OFF'}.", reply_markup=get_settings_kb(await state.get_data()))
    elif text.startswith("🧠"):
        val = not d.get("ai_mode", False); await state.update_data(ai_mode=val)
        await m.answer(f"AI Аналіз: {'ON' if val else 'OFF'}.", reply_markup=get_settings_kb(await state.get_data()))
    elif text.startswith("📈"):
        await state.set_state(BotState.waiting_for_max_ratio)
        await m.answer("Оберіть макс. множник:", reply_markup=ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text=f"×{r}") for r in RATIO_OPTIONS], [KeyboardButton(text="🔙 Назад")]], resize_keyboard=True))
    elif text.startswith("📉"):
        await state.set_state(BotState.waiting_for_avg_mult)
        await m.answer("Оберіть антифейк множник:", reply_markup=ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text=f"×{a}") for a in AVG_MULTIPLIER_OPTIONS], [KeyboardButton(text="🔙 Назад")]], resize_keyboard=True))

@dp.message(StateFilter(BotState.waiting_for_max_ratio))
async def set_max_ratio(m, state: FSMContext):
    text = m.text
    if text == "🔙 Назад":
        await state.set_state(BotState.settings_menu); d = await state.get_data()
        await m.answer("Налаштування.", reply_markup=get_settings_kb(d)); return
    try:
        r = float(text.replace("×","").strip())
        if r in RATIO_OPTIONS:
            await state.update_data(max_ratio=r); await state.set_state(BotState.settings_menu)
            d = await state.get_data(); await m.answer(f"Макс. множник: ×{r}.", reply_markup=get_settings_kb(d))
    except: pass

@dp.message(StateFilter(BotState.waiting_for_avg_mult))
async def set_avg_mult(m, state: FSMContext):
    text = m.text
    if text == "🔙 Назад":
        await state.set_state(BotState.settings_menu); d = await state.get_data()
        await m.answer("Налаштування.", reply_markup=get_settings_kb(d)); return
    try:
        a = float(text.replace("×","").strip())
        if a in AVG_MULTIPLIER_OPTIONS:
            await state.update_data(max_avg_mult=a); await state.set_state(BotState.settings_menu)
            d = await state.get_data(); await m.answer(f"Антифейк: ×{a}.", reply_markup=get_settings_kb(d))
    except: pass

@dp.message(F.text == "🗺 Режим", StateFilter('*'))
async def choose_mode(m, state: FSMContext):
    await m.answer("Оберіть режим:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎲 Всі міста", callback_data="mode_all")],
        [InlineKeyboardButton(text="📍 Шлях", callback_data="mode_custom")],
        [InlineKeyboardButton(text="📍 З міста", callback_data="mode_origin")]
    ]))

@dp.callback_query(F.data.startswith("mode_"))
async def set_mode_cb(cb, state: FSMContext):
    m_type = cb.data.split("_")[1]
    if m_type == "all":
        await state.update_data(mode="all", f_c=None, t_c=None)
        await cb.message.edit_text("🌍 Всі міста!")
        await cb.message.answer("Оновлено", reply_markup=get_main_kb(await state.get_data()))
    elif m_type == "custom":
        await state.set_state(BotState.picking_from)
        await cb.message.edit_text("Звідки:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"{CITY_EMOJIS[c]} {c}", callback_data=f"city_from_{c}")] for c in CITIES if c!="Black Market"
        ]))
    else:
        await state.set_state(BotState.picking_origin)
        await cb.message.edit_text("Оберіть місто:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"{CITY_EMOJIS[c]} {c}", callback_data=f"origin_{c}")] for c in CITIES if c!="Black Market"
        ]))

@dp.callback_query(F.data.startswith("city_from_"))
async def city_from(cb, state: FSMContext):
    c = cb.data.split("_")[2]
    await state.update_data(f_c=c); await state.set_state(BotState.picking_to)
    await cb.message.edit_text(f"З: {c}. Куди:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{CITY_EMOJIS[ci]} {ci}", callback_data=f"city_to_{ci}")] for ci in CITIES if ci!=c
    ]))

@dp.callback_query(F.data.startswith("city_to_"))
async def city_to(cb, state: FSMContext):
    c = cb.data.split("_")[2]
    await state.update_data(t_c=c, mode="custom"); await state.set_state(None)
    await cb.message.edit_text("📍 Шлях збережено!")
    await cb.message.answer("Оновлено", reply_markup=get_main_kb(await state.get_data()))

@dp.callback_query(F.data.startswith("origin_"))
async def city_origin(cb, state: FSMContext):
    c = cb.data.split("_")[1]
    await state.update_data(f_c=c, t_c=None, mode="origin"); await state.set_state(None)
    await cb.message.edit_text(f"📍 Режим «З {c}»")
    await cb.message.answer("Оновлено", reply_markup=get_main_kb(await state.get_data()))

@dp.message(F.text == "🚀 Запустити сканер", StateFilter('*'))
async def main_search(m, state: FSMContext):
    global is_db_ready, items_data
    d = await state.get_data(); chat_id = m.chat.id
    now = time_module.time()
    if now - last_scan_time.get(chat_id, 0) < 10:
        await m.answer("⏳ Зачекайте 10 секунд."); return
    last_scan_time[chat_id] = now

    if not is_db_ready:
        await m.answer("⏳ База предметів не завантажена. Пробую завантажити...")
        await download_items()
        if not is_db_ready or not items_data:
            return await m.answer("❌ Не вдалося завантажити базу предметів. Спробуйте пізніше.")
        await m.answer(f"✅ Базу завантажено ({len(items_data)} предметів). Розпочинаю пошук...")

    ai_active = d.get("ai_mode", False)
    if not ai_active:
        await bot.send_chat_action(chat_id, ChatAction.TYPING)
        s_msg = await m.answer("🔍 Шукаю...")
        res = await scan_logic(d, d.get('f_c'), d.get('t_c'))
        await safe_delete(s_msg)
        await state.update_data(has_searched=True, last_results=res); d['has_searched']=True
        await disp_res(m, res, d)
        await m.answer("✅ Готово!", reply_markup=get_main_kb(d))
    else:
        if not gemini_client: return await m.answer("❌ GEMINI_API_KEY не налаштовано.")
        await bot.send_chat_action(chat_id, ChatAction.TYPING)
        s_msg = await m.answer("🧠 AI аналізує...")
        res = await ai_scan_logic(d, d.get('f_c'), d.get('t_c'))
        await safe_delete(s_msg)
        if res is None:
            await m.answer("⚠️ AI не зміг. Використовую звичайний аналіз.")
            res = await scan_logic(d, d.get('f_c'), d.get('t_c'))
        if not res: return await m.answer("📭 Нічого.", reply_markup=get_main_kb(d))
        await state.update_data(has_searched=True, last_results=res); d['has_searched']=True
        await disp_res(m, res, d)
        await m.answer("✅ AI-аналіз завершено", reply_markup=get_main_kb(d))

@dp.message(F.text == "🔄 Оновити пошук", StateFilter('*'))
async def refresh_search(m, state: FSMContext):
    d = await state.get_data(); last_res = d.get("last_results")
    if not last_res: return await m.answer("Немає результатів для оновлення.")
    item_ids = list({r['id'] for r in last_res})
    cities = list({r['from'] for r in last_res} | {r['to'] for r in last_res})
    global price_cache, price_cache_time; price_cache = {}
    data = await fetch_prices_with_cache(item_ids, cities)
    if not data: return await m.answer("❌ Не вдалося отримати свіжі ціни.")
    updated = []
    for r in last_res:
        new_buy = new_sell = None
        for e in data:
            if e['item_id']==r['id'] and e['city']==r['from']: new_buy = e.get('sell_price_min',0)
            if e['item_id']==r['id'] and e['city']==r['to']: new_sell = e.get('buy_price_max' if r['to']=="Black Market" else 'sell_price_min',0)
        if new_buy and new_sell:
            tax = 0.91 if r['to']=="Black Market" else 0.895
            np = int(new_sell*tax - new_buy)
            if np>0:
                r['buy']=new_buy; r['sell']=new_sell; r['p_n']=np; r['p_p']=int(new_sell*(tax+0.04)-new_buy)
                updated.append(r)
    if updated:
        await disp_res(m, updated, d)
        await m.answer("🔄 Оновлено.", reply_markup=get_main_kb(d))
    else: await m.answer("😕 Більше не актуальні.")

@dp.message(F.text == "💰 Бюджет", StateFilter('*'))
async def limit_menu(m, state: FSMContext):
    d = await state.get_data()
    await m.answer(f"⚙️ Бюджет: {d.get('buy_limit',0):,}\n📈 Профіт: {d.get('profit_limit',4000):,}",
                   reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                       [InlineKeyboardButton(text="💰 Бюджет", callback_data="set_buy"),
                        InlineKeyboardButton(text="📈 Профіт", callback_data="set_profit")]]))

@dp.callback_query(F.data.startswith("set_"))
async def set_limit_cb(cb, state: FSMContext):
    t = cb.data.split("_")[1]
    await state.set_state(BotState.waiting_for_buy_limit if t=="buy" else BotState.waiting_for_profit_limit)
    await cb.message.answer("Введіть число:", reply_markup=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="❌ Скасувати")]], resize_keyboard=True))
    await cb.answer()

@dp.message(F.text == "🧮 Калькулятор", StateFilter('*'))
async def calc_start(m, state: FSMContext):
    await state.set_state(BotState.calc_count)
    await m.answer("📦 Кількість:", reply_markup=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="❌ Скасувати")]], resize_keyboard=True))

@dp.message(F.text == "🔄 Скинути", StateFilter('*'))
async def reset_confirm(m, state: FSMContext):
    await state.set_state(BotState.confirm_reset)
    await m.answer("Скинути всі дані?", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Так", callback_data="reset_yes"), InlineKeyboardButton(text="❌ Ні", callback_data="reset_no")]]))

@dp.callback_query(F.data.startswith("reset_"), StateFilter(BotState.confirm_reset))
async def reset_action(cb, state: FSMContext):
    if cb.data=="reset_yes":
        await state.clear(); await cb.message.edit_text("🔄 Скинуто.")
        await cb.message.answer("Почнемо заново?", reply_markup=get_main_kb({}))
    else: await cb.message.edit_text("🚫 Скасовано.")
    await state.set_state(None)

@dp.message(StateFilter(BotState.waiting_for_buy_limit, BotState.waiting_for_profit_limit, BotState.calc_count, BotState.calc_buy, BotState.calc_sell))
async def numeric_handler(m, state: FSMContext):
    if m.text == "❌ Скасувати":
        await state.set_state(None); return await m.answer("Скасовано", reply_markup=get_main_kb(await state.get_data()))
    try:
        v = int(m.text.replace(" ","")); curr = await state.get_state(); d = await state.get_data()
        if "buy_limit" in str(curr): await state.update_data(buy_limit=v)
        elif "profit_limit" in str(curr): await state.update_data(profit_limit=v)
        elif "calc_count" in str(curr): await state.update_data(c=v); await state.set_state(BotState.calc_buy); return await m.answer("📥 Ціна КУПІВЛІ:")
        elif "calc_buy" in str(curr): await state.update_data(b=v); await state.set_state(BotState.calc_sell); return await m.answer("📤 Ціна ПРОДАЖУ:")
        elif "calc_sell" in str(curr):
            await state.set_state(None)
            pp = int((v*0.935)-d['b'])*d['c']; pn = int((v*0.895)-d['b'])*d['c']
            await m.answer(f"📊 Результат:\n👑 Пр: <b>{pp:,}</b>\n💀 Пр: <b>{pn:,}</b>", parse_mode=ParseMode.HTML, reply_markup=get_main_kb(d))
            return
        await state.set_state(None); await m.answer(f"✅ Збережено: {v:,}", reply_markup=get_main_kb(await state.get_data()))
    except ValueError: await m.answer("❌ Введіть ціле число!")

async def main():
    global http_session
    if not TOKEN: return
    http_session = aiohttp.ClientSession(headers=HEADERS)
    asyncio.create_task(download_items())
    asyncio.create_task(cache_cleaner())
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    except TelegramUnauthorizedError: logger.critical("❌ Токен недійсний.")
    except Exception as e: logger.exception(f"Критична помилка: {e}")
    finally:
        if http_session and not http_session.closed: await http_session.close()
        if bot and hasattr(bot,'session') and bot.session: await bot.session.close()

if __name__=="__main__":
    try: asyncio.run(main())
    except KeyboardInterrupt: logger.info("Бот зупинено.")