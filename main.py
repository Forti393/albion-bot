import os, json, aiohttp, asyncio, re, logging, time, signal, random, html
from datetime import datetime, timezone, timedelta
from typing import List, Optional
from aiogram import Bot, Dispatcher, types, F
from aiogram.enums import ParseMode, ChatAction
from aiogram.filters import Command, StateFilter
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.exceptions import TelegramUnauthorizedError

# ================= ЛОГУВАННЯ =================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ================= КОНФІГУРАЦІЯ =================
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0")) 
TOKEN = os.environ.get("BOT_TOKEN")

bot = Bot(token=TOKEN) if TOKEN else None
dp = Dispatcher(storage=MemoryStorage())

# Глобальні змінні
items_data = {}; is_db_ready = False
http_session: Optional[aiohttp.ClientSession] = None 
scan_semaphore = asyncio.Semaphore(5) 
history_cache = {}
is_shutting_down = False

# Константи
CACHE_TTL = 3600 
CITIES = ["Bridgewatch", "Martlock", "Lymhurst", "Thetford", "Fort Sterling", "Caerleon", "Brecilien", "Black Market"]
CITY_EMOJIS = {"Lymhurst":"🟢","Martlock":"🔵","Caerleon":"🔴","Thetford":"🟣","Bridgewatch":"🟠","Fort Sterling":"⚪","Brecilien":"🌸","Black Market":"⚫"}
QUALITY_NAMES = {1:"Обычное", 2:"Хорошее", 3:"Выдающееся", 4:"Отличное", 5:"Шедевр"}
TRASH = ["Знаток ","Мастер ","Великий мастер ","Старейшина ","Ученик ","Новичок "]
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

class BotState(StatesGroup):
    waiting_for_buy_limit = State(); waiting_for_profit_limit = State()
    picking_from = State(); picking_to = State()
    calc_count = State(); calc_buy = State(); calc_sell = State()
    confirm_reset = State()

# ================= СЛУЖБОВІ ФУНКЦІЇ =================
async def safe_delete(msg):
    try: await msg.delete()
    except Exception: pass

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
        dt = datetime.fromisoformat(s.split(".")[0].replace("Z","")).replace(tzinfo=timezone.utc)
        m = int((datetime.now(timezone.utc)-dt).total_seconds()/60)
        return f"{m}м" if m < 60 else f"{m//60}г"
    except Exception: return "??"

async def get_item_liquidity(item_id, city, quality):
    global http_session, history_cache
    if not http_session or http_session.closed: return 0, 0
    
    cache_key, now = f"{item_id}|{city}|{quality}", datetime.now(timezone.utc)
    if cache_key in history_cache and (now - history_cache[cache_key]['time']).total_seconds() < CACHE_TTL:
        return history_cache[cache_key]['volume'], history_cache[cache_key]['avg_p']
        
    # Використовуємо t-series 24 для отримання детальних даних за добу
    url = f"https://europe.albion-online-data.com/api/v2/stats/history/{item_id}?locations={city}&qualities={quality}&t-series=24"
    async with scan_semaphore:
        try:
            async with http_session.get(url, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data and isinstance(data, list) and len(data) > 0:
                        history = data[0].get('data', [])
                        if history:
                            # Беремо останній запис історії (найсвіжіші дані)
                            last_entry = history[-1]
                            vol = last_entry.get('item_count', 0)
                            avg_p = int(last_entry.get('average_price', 0))
                            
                            history_cache[cache_key] = {'volume': vol, 'avg_p': avg_p, 'time': now}
                            return vol, avg_p
        except Exception: pass
    return 0, 0

async def download_items():
    global items_data, is_db_ready, http_session
    try:
        async with http_session.get("https://raw.githubusercontent.com/ao-data/ao-bin-dumps/master/formatted/items.json", timeout=60) as r:
            if r.status == 200:
                data = await r.json(content_type=None)
                allowed = ["weapon","armor","plate","leather","cloth","bag","cape","potion","meal","mount","tool","offhand"]
                items_data = {i["UniqueName"]: i for i in data if i.get("UniqueName","").startswith(("T4_","T5_","T6_","T7_","T8_")) and any(x in i.get("UniqueName","").lower() for x in allowed)}
                is_db_ready = True
    except Exception as e: 
        logger.error(f"Помилка завантаження БД: {e}")
        is_db_ready = False

async def scan_logic(d, f_c=None, t_c=None):
    global http_session
    if not items_data or not http_session: return []
    pre_res = []; b_l = d.get("buy_limit", 0); p_l = d.get("profit_limit", 4000)
    ext = d.get("extra", False); check_liq_limit = d.get("check_liq", False) 
    
    i_list = list(items_data.keys()); cities = [f_c, t_c] if f_c and t_c else CITIES
    for i in range(0, len(i_list), 50):
        if is_shutting_down: break
        url = f"https://europe.albion-online-data.com/api/v2/stats/prices/{','.join(i_list[i:i+50])}?locations={','.join(cities)}"
        data = None
        async with scan_semaphore:
            try:
                async with http_session.get(url, timeout=20) as resp:
                    if resp.status == 200: data = await resp.json()
            except Exception: continue
        if not data: continue
        
        now = datetime.now(timezone.utc)
        grouped = {}
        for e in data:
            k = f"{e['item_id']}|{e['quality']}"; grouped.setdefault(k, {})[e['city']] = e
            
        for k, c_d in grouped.items():
            i_id, q = k.split("|")
            srcs = [f_c] if f_c else [c for c in c_d if c != "Black Market"]
            
            for sc in srcs:
                if sc not in c_d: continue
                buy = c_d[sc].get('sell_price_min', 0)
                if buy <= 500 or buy > b_l: continue
                
                bd_str = c_d[sc]['sell_price_min_date']
                try:
                    b_dt = datetime.fromisoformat(bd_str.split(".")[0].replace("Z","")).replace(tzinfo=timezone.utc)
                except Exception: continue
                if (now-b_dt).total_seconds()/60 > 180: continue
                
                targets = [t_c] if t_c else [c for c in c_d if c != sc]
                for tc in targets:
                    if tc not in c_d: continue
                    is_bm = (tc == "Black Market")
                    sell = c_d[tc].get('buy_price_max' if is_bm else 'sell_price_min', 0)
                    if sell <= buy: continue
                    
                    sd_str = c_d[tc].get('buy_price_max_date' if is_bm else 'sell_price_min_date')
                    try:
                        s_dt = datetime.fromisoformat(sd_str.split(".")[0].replace("Z","")).replace(tzinfo=timezone.utc)
                    except Exception: continue
                    if (now-s_dt).total_seconds()/60 > 180: continue

                    # Податки: 10.5% звичайна торгівля (з премом), 9% Блек Маркет (ордер)
                    tax = 0.91 if is_bm else 0.895
                    p_n = int(sell * tax - buy)
                    
                    if p_n >= p_l:
                        if ext and ((now-b_dt).total_seconds()/60 > 30 or (now-s_dt).total_seconds()/60 > 30): continue
                        pre_res.append({'id':i_id,'q':int(q),'from':sc,'to':tc,'buy':buy,'sell':sell,
                                        'p_p':int(sell*0.935-buy),'p_n':p_n,'bd':bd_str,'sd':sd_str})

    pre_res.sort(key=lambda x: x['p_n'], reverse=True)
    res_final = []
    
    for item in pre_res[:30]:
        vol, avg_p = await get_item_liquidity(item['id'], item['to'], item['q'])
        item['vol'] = vol
        item['avg_p'] = avg_p
        
        if check_liq_limit and vol < 4: continue
            
        res_final.append(item)
        if len(res_final) >= 15: break

    return res_final

async def disp_res(msg, res, d):
    messages, full_text = [], ""
    for idx, r in enumerate(res, 1):
        id_parts = r['id'].split("@")
        b_id, enc = id_parts[0], id_parts[1] if len(id_parts) > 1 else "0"
        icon = get_item_icon(b_id)
        tier = b_id.split('_')[0][1:]
        
        name = items_data.get(b_id, {}).get("LocalizedNames", {}).get("RU-RU", b_id)
        name = re.sub(r'\s*\([^)]*\)', '', html.escape(name.upper()))
        for t in TRASH: name = name.replace(t, "")
        tbd, tsd = fmt_t(r.get('bd')), fmt_t(r.get('sd'))
        
        item_block = (
            f"{idx}) {icon} <b>{name}</b> [{tier}.{enc}]\n"
            f"✨ {QUALITY_NAMES.get(r['q'], 'Обычное')}\n"
            f"📥 {CITY_EMOJIS[r['from']]} {r['buy']:,} | 🕒 {tbd}\n"
            f"📤 {CITY_EMOJIS[r['to']]} {r['sell']:,} | 🕒 {tsd}\n"
            f"<pre>"
            f"Прибуток:\n"
            f"{f'👑 {r[´p_p´]:,}':<17} Попит: {r.get('vol', 0)} шт/д\n"
            f"{f'💀 {r[´p_n´]:,}':<17} Сер. ціна: {r.get('avg_p', 0):,}"
            f"</pre>\n"
            f"───────────────────\n\n"
        ).replace("´", "'")
        
        if len(full_text) + len(item_block) > 3900: 
            messages.append(full_text); full_text = item_block
        else: full_text += item_block
    if full_text: messages.append(full_text)
    for t in messages: await msg.answer(t, parse_mode=ParseMode.HTML)

def get_main_kb(d):
    mode, budget, searched = d.get("mode"), d.get("buy_limit", 0), d.get("has_searched", False)
    m_btn = "Режим: 🌍 Всі міста" if mode == "all" else ("Режим: 📍 Шлях" if mode == "custom" else "🗺 Режим")
    kb = []
    if budget > 0 and mode: kb.append([KeyboardButton(text="🚀 Запустити сканер")])
    if searched:
        kb.append([KeyboardButton(text=m_btn), KeyboardButton(text=f"⚡ 30хв: {'ON' if d.get('extra') else 'OFF'}")])
        kb.append([KeyboardButton(text=f"📊 Попит Ліміт: {'ON' if d.get('check_liq') else 'OFF'}"), KeyboardButton(text="🧮 Калькулятор")])
        kb.append([KeyboardButton(text="💰 Бюджет"), KeyboardButton(text="🔄 Скинути")])
    else:
        kb.append([KeyboardButton(text="💰 Бюджет"), KeyboardButton(text=m_btn)])
        kb.append([KeyboardButton(text="🧮 Калькулятор"), KeyboardButton(text="❓ Допомога")])
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

@dp.message(Command("start"), StateFilter('*'))
async def cmd_start(m, state: FSMContext):
    await state.clear()
    await m.answer("👋 <b>Привіт!</b> Оберіть 💰 <b>Бюджет</b> та 🗺 <b>Режим</b>.", parse_mode=ParseMode.HTML, reply_markup=get_main_kb({}))

@dp.message(F.text == "🚀 Запустити сканер", StateFilter('*'))
async def main_search(m, state: FSMContext):
    d = await state.get_data()
    if not is_db_ready: return await m.answer("⏳ БД вантажиться...")
    await bot.send_chat_action(m.chat.id, ChatAction.TYPING)
    s_msg = await m.answer("🔍 Шукаю..."); res = await scan_logic(d, d.get('f_c'), d.get('t_c'))
    await safe_delete(s_msg)
    await state.update_data(has_searched=True)
    if not res: await m.answer("📭 Порожньо. Збільште бюджет або вимкніть ліміти.")
    else: await disp_res(m, res, d)
    await m.answer("✅ Готово!", reply_markup=get_main_kb(await state.get_data()))

@dp.message(F.text.startswith("Режим:") | (F.text == "🗺 Режим"), StateFilter('*'))
async def choose_mode(m, state: FSMContext):
    await m.answer("Оберіть режим:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎲 Всі міста", callback_data="set_mode_all")],
        [InlineKeyboardButton(text="📍 Шлях", callback_data="set_mode_custom")]
    ]))

@dp.callback_query(F.data.startswith("set_mode_"))
async def set_mode_cb(cb, state: FSMContext):
    m_type = cb.data.split("_")[2]
    if m_type == "all": 
        await state.update_data(mode="all"); d = await state.get_data()
        await cb.message.edit_text("🌍 Режим: Всі міста!"); await cb.message.answer("Ок", reply_markup=get_main_kb(d))
    else: 
        await state.set_state(BotState.picking_from)
        await cb.message.edit_text("Звідки:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"{CITY_EMOJIS[c]} {c}", callback_data=f"city_{c}")] for c in CITIES if c!="Black Market"
        ]))

@dp.callback_query(F.data.startswith("city_"))
async def city_pick(cb, state: FSMContext):
    curr = await state.get_state(); c = cb.data.split("_")[1]
    if curr == BotState.picking_from.state:
        await state.update_data(f_c=c); await state.set_state(BotState.picking_to)
        await cb.message.edit_text(f"З: {c}. Куди:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"{CITY_EMOJIS[ci]} {ci}", callback_data=f"city_{ci}")] for ci in CITIES if ci!=c
        ]))
    elif curr == BotState.picking_to.state:
        await state.update_data(t_c=c, mode="custom"); await state.set_state(None)
        await cb.message.edit_text(f"📍 Шлях збережено!"); await cb.message.answer("Ок", reply_markup=get_main_kb(await state.get_data()))

@dp.message(F.text == "💰 Бюджет", StateFilter('*'))
async def limit_menu(m, state: FSMContext):
    d = await state.get_data(); b, p = d.get("buy_limit", 0), d.get("profit_limit", 4000)
    await m.answer(f"⚙️ Бюджет: {b:,}\n📈 Профіт: {p:,}", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💰 Бюджет", callback_data="set_limit_buy"), InlineKeyboardButton(text="📈 Профіт", callback_data="set_limit_profit")]
    ]))

@dp.callback_query(F.data.startswith("set_limit_"))
async def set_limit_cb(cb, state: FSMContext):
    t = cb.data.split("_")[2]; await state.set_state(BotState.waiting_for_buy_limit if t=="buy" else BotState.waiting_for_profit_limit)
    await cb.message.answer(f"Введіть число:", reply_markup=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="❌ Скасувати")]], resize_keyboard=True))

@dp.message(F.text == "🧮 Калькулятор", StateFilter('*'))
async def calc_start(m, state: FSMContext):
    await state.set_state(BotState.calc_count)
    await m.answer("📦 Кількість:", reply_markup=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="❌ Скасувати")]], resize_keyboard=True))

@dp.message(F.text == "🔄 Скинути", StateFilter('*'))
async def reset_confirm(m, state: FSMContext):
    await state.set_state(BotState.confirm_reset)
    await m.answer("⚠️ Скинути дані?", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Так", callback_data="reset_yes"), InlineKeyboardButton(text="❌ Ні", callback_data="reset_no")]
    ]))

@dp.callback_query(F.data.startswith("reset_"), StateFilter(BotState.confirm_reset))
async def reset_action(cb, state: FSMContext):
    if cb.data == "reset_yes": await state.clear(); await cb.message.edit_text("🔄 Скинуто."); await cb.message.answer("Почнемо?", reply_markup=get_main_kb({}))
    else: await cb.message.edit_text("🚫 Відміна.")
    await state.set_state(None)

@dp.message(StateFilter(BotState.waiting_for_buy_limit, BotState.waiting_for_profit_limit, BotState.calc_count, BotState.calc_buy, BotState.calc_sell))
async def numeric_handler(m, state: FSMContext):
    if m.text == "❌ Скасувати":
        await state.set_state(None); return await m.answer("Ок", reply_markup=get_main_kb(await state.get_data()))
    try:
        v = int(m.text.replace(" ","")); curr = await state.get_state()
        if "buy_limit" in str(curr): await state.update_data(buy_limit=v); await state.set_state(None)
        elif "profit_limit" in str(curr): await state.update_data(profit_limit=v); await state.set_state(None)
        elif "calc_count" in str(curr): await state.update_data(c=v); await state.set_state(BotState.calc_buy); return await m.answer("📥 Ціна КУПІВЛІ:")
        elif "calc_buy" in str(curr): await state.update_data(b=v); await state.set_state(BotState.calc_sell); return await m.answer("📤 Ціна ПРОДАЖУ:")
        elif "calc_sell" in str(curr):
            d = await state.get_data(); await state.set_state(None)
            p_p = int((v*0.935)-d['b'])*d['c']; p_n = int((v*0.895)-d['b'])*d['c']
            return await m.answer(f"📊 {d['c']} шт:\n👑 Пр: <b>{p_p:,}</b>\n💀 Пр: <b>{p_n:,}</b>", reply_markup=get_main_kb(d), parse_mode=ParseMode.HTML)
        await m.answer(f"✅ Збережено: {v:,}", reply_markup=get_main_kb(await state.get_data()))
    except: await m.answer("❌ Тільки число!")

@dp.message(F.text.regexp(r"⚡ 30хв:|📊 Попит Ліміт:"), StateFilter('*'))
async def toggles(m, state: FSMContext):
    d = await state.get_data()
    if "30хв" in m.text:
        val = not d.get("extra", False); await state.update_data(extra=val)
    else:
        val = not d.get("check_liq", False); await state.update_data(check_liq=val)
    await m.answer("Оновлено", reply_markup=get_main_kb(await state.get_data()))

async def main():
    global http_session, is_shutting_down
    if not bot:
        logger.error("🚨 Токен відсутній!")
        return

    http_session = aiohttp.ClientSession(headers=HEADERS)
    await bot.delete_webhook(drop_pending_updates=True)
    asyncio.create_task(download_items())
    
    try:
        await dp.start_polling(bot)
    except TelegramUnauthorizedError:
        logger.error("🚨 ТОКЕН НЕВІРНИЙ АБО АНУЛЬОВАНИЙ!")
    except Exception as e:
        logger.error(f"Помилка: {e}")
    finally:
        is_shutting_down = True
        if http_session: await http_session.close()
        await bot.session.close()

if __name__ == "__main__":
    try: asyncio.run(main())
    except (KeyboardInterrupt, SystemExit): pass
