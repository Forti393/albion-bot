import os, json, aiohttp, asyncio
from datetime import datetime, UTC, timedelta
from aiogram import Bot, Dispatcher, types, F
from aiogram.enums import ParseMode
from aiogram.filters import Command, StateFilter
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

# ================= НАЛАШТУВАННЯ =================
ADMIN_ID = 1052964898  # ⚠️ ВПИШИ СВІЙ TELEGRAM ID СЮДИ

bot = Bot(token=os.environ.get("BOT_TOKEN"))
dp = Dispatcher(storage=MemoryStorage())
items_data = {}; is_db_ready = False

# Керування чергою
scan_semaphore = asyncio.Semaphore(3) # Макс. 3 одночасних сканування для юзерів
user_cooldowns = {}

CITIES = ["Bridgewatch", "Martlock", "Lymhurst", "Thetford", "Fort Sterling", "Caerleon", "Brecilien", "Black Market"]
CITY_EMOJIS = {"Lymhurst":"🟢","Martlock":"🔵","Caerleon":"⚫","Thetford":"🟣","Bridgewatch":"🟠","Fort Sterling":"⚪","Brecilien":"🌸","Black Market":"💀"}
QUALITY_NAMES = {1:"Обычное", 2:"Хорошее", 3:"Выдающееся", 4:"Отличное", 5:"Шедевр"}
TRASH = ["Знаток ","Мастер ","Великий мастер ","Старейшина ","Ученик ","Новичок "]

class BotState(StatesGroup):
    waiting_for_buy_limit = State(); waiting_for_profit_limit = State()
    picking_from = State(); picking_to = State()
    calc_count = State(); calc_buy = State(); calc_sell = State()

# ================= КЛАВІАТУРИ =================
def get_start_kb(): return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="❓ Допомога"), KeyboardButton(text="⚙️ Ліміт")]], resize_keyboard=True)
def get_main_kb(d):
    m = d.get("mode","?"); m_l = "Всі" if m=="all" else ("Шлях" if m=="custom" else "?")
    e_l = "🚫 Екстра відміна" if d.get("extra") else "⚡ Екстра тестування"
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="🔍 Пошук")],[KeyboardButton(text=f"🗺️ Режими ({m_l})"), KeyboardButton(text=e_l)],[KeyboardButton(text="🧮 Калькулятор"), KeyboardButton(text="⚙️ Ліміт")],[KeyboardButton(text="🔄 Перезавантаження")]], resize_keyboard=True)

# ================= ЛОГІКА СКАНУВАННЯ =================
async def download_items():
    global items_data, is_db_ready
    is_db_ready = False
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get("https://raw.githubusercontent.com/ao-data/ao-bin-dumps/master/formatted/items.json", timeout=60) as r:
                if r.status == 200:
                    data = await r.json(content_type=None)
                    allowed = ["weapon","armor","plate","leather","cloth","bag","cape","potion","meal","mount","tool","shapeshifter","glove","offhand"]
                    items_data = {i["UniqueName"]: i for i in data if i.get("UniqueName","").startswith(("T4_","T5_","T6_","T7_","T8_")) and any(x in i.get("UniqueName","").lower() for x in allowed)}
    except: pass
    finally: is_db_ready = True

async def scan_logic(d, f_c=None, t_c=None):
    res = []; b_l = d.get("buy_limit",0); p_l = d.get("profit_limit",4000); ext = d.get("extra",False)
    i_list = list(items_data.keys()); cities = [f_c, t_c] if f_c and t_c else CITIES
    async with aiohttp.ClientSession() as s:
        for i in range(0, len(i_list), 50):
            batch = i_list[i:i+50]
            url = f"https://europe.albion-online-data.com/api/v2/stats/prices/{','.join(batch)}?locations={','.join(cities)}"
            try:
                async with s.get(url, timeout=15) as resp:
                    data = await resp.json() if resp.status == 200 else []
                now = datetime.now(UTC)
                grouped = {}
                for e in data:
                    k = f"{e['item_id']}|{e['quality']}"; grouped.setdefault(k, {})[e['city']] = e
                for k, c_d in grouped.items():
                    i_id, q = k.split("|"); srcs = [f_c] if f_c else [c for c in c_d if c!="Black Market"]
                    for sc in srcs:
                        if sc not in c_d: continue
                        buy = c_d[sc].get('sell_price_min',0)
                        if buy <= 500 or buy > b_l: continue
                        b_dt = datetime.fromisoformat(c_d[sc]['sell_price_min_date'].split(".")[0].replace("Z","")).replace(tzinfo=UTC)
                        if (now-b_dt).total_seconds()/60 > 180: continue
                        targets = [t_c] if t_c else [c for c in c_d if c!=sc]
                        for tc in targets:
                            if tc not in c_d: continue
                            sk = 'buy_price_max_date' if tc=="Black Market" else 'sell_price_min_date'
                            sell = c_d[tc].get('buy_price_max' if tc=="Black Market" else 'sell_price_min',0)
                            if sell<=buy or (sell/buy)>10: continue
                            s_dt = datetime.fromisoformat(c_d[tc].get(sk).split(".")[0].replace("Z","")).replace(tzinfo=UTC)
                            if (now-s_dt).total_seconds()/60 > 180: continue
                            p_n = int(sell*0.895-buy)
                            if p_n >= p_l: res.append({'id':i_id,'q':int(q),'from':sc,'to':tc,'buy':buy,'sell':sell,'p_p':int(sell*0.935-buy),'p_n':p_n,'bd':c_d[sc]['sell_price_min_date'],'sd':c_d[tc].get(sk)})
                if i % 250 == 0: await asyncio.sleep(0.3)
            except: continue
    return res

# ================= ОБРОБНИКИ =================
@dp.message(F.text == "🔍 Пошук", StateFilter('*'))
async def main_search(m, state: FSMContext):
    u_id = m.from_user.id
    is_admin = (u_id == ADMIN_ID)
    
    # ПЕРЕВІРКА COOLDOWN (Тільки для юзерів)
    if not is_admin and u_id in user_cooldowns:
        diff = (datetime.now() - user_cooldowns[u_id]).total_seconds()
        if diff < 30: return await m.answer(f"⏳ Зачекай {int(30-diff)} сек.")

    if not is_db_ready: return await m.answer("⏳ База завантажується...")
    d = await state.get_data(); b = d.get("buy_limit",0); mode = d.get("mode")
    if b <= 0: return await m.answer("Встанови ліміт!"); if not mode: return await m.answer("Обери режим!", reply_markup=get_mode_inline())

    # ЛОГІКА ЧЕРГИ
    if is_admin:
        # Адмін іде поза чергою і без семафора
        status_msg = await m.answer("⚡ <b>Адмін-пошук (пріоритет)...</b>", parse_mode=ParseMode.HTML, reply_markup=ReplyKeyboardRemove())
        res = await scan_logic(d, d.get('f_c'), d.get('t_c'))
        await status_msg.delete()
        if not res: await m.answer("📭 Нічого.", reply_markup=get_main_kb(d))
        else: await disp_res(m, res); await m.answer(f"✅ Готово: {len(res)}", reply_markup=get_main_kb(d))
    else:
        # Звичайні юзери чекають на семафор
        if scan_semaphore.locked():
            await m.answer("🕒 Бот зайнятий, твій запит у черзі...")
        async with scan_semaphore:
            user_cooldowns[u_id] = datetime.now()
            status_msg = await m.answer("🔍 Сканую ринок...", reply_markup=ReplyKeyboardRemove())
            res = await scan_logic(d, d.get('f_c'), d.get('t_c'))
            await status_msg.delete()
            if not res: await m.answer("📭 Нічого.", reply_markup=get_main_kb(d))
            else: await disp_res(m, res); await m.answer(f"✅ Готово: {len(res)}", reply_markup=get_main_kb(d))

# --- Допоміжні функції (залишаються як були) ---
def get_mode_inline(): return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🎲 Всі міста", callback_data="set_mode_all")],[InlineKeyboardButton(text="📍 Шлях", callback_data="set_mode_custom")]])
def get_city_inline(ex=None): return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=f"{CITY_EMOJIS[c]} {c}", callback_data=f"city_{c}")] for c in CITIES if c!="Black Market" and c!=ex])

@dp.callback_query(F.data.startswith("set_mode_"), StateFilter('*'))
async def set_mode_cb(cb, state: FSMContext):
    m = cb.data.split("_")[2]; await state.update_data(mode=m); d = await state.get_data()
    await cb.message.answer(f"✅ Режим змінено!", reply_markup=get_main_kb(d)); await cb.answer()

async def disp_res(msg, res):
    res.sort(key=lambda x: x['p_n'], reverse=True)
    for r in res[:15]:
        b_id = r['id'].split("@")[0]; enc = r['id'].split("@")[1] if "@" in r['id'] else "0"
        name = items_data.get(b_id, {}).get("LocalizedNames", {}).get("RU-RU", b_id)
        for t in TRASH: name = name.replace(t, "")
        await msg.answer(f"📦 <b>{name} [{b_id.split('_')[0][1:]}.{enc}]</b>\n🛒 {CITY_EMOJIS[r['from']]} {r['from']}: <b>{r['buy']:,}</b>\n💰 {CITY_EMOJIS[r['to']]} {r['to']}: <b>{r['sell']:,}</b>\n💀: <b>{r['p_n']:,}</b>", parse_mode=ParseMode.HTML)

# --- Додай тут решту обробників (ліміти, перезавантаження, калькулятор) з попередніх повідомлень ---

async def main():
    try: await bot.delete_webhook(drop_pending_updates=True)
    except: pass
    asyncio.create_task(download_items()); await dp.start_polling(bot)

if __name__ == "__main__": asyncio.run(main())
