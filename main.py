import os, json, aiohttp, asyncio, re
from datetime import datetime, UTC, timedelta
from aiogram import Bot, Dispatcher, types, F
from aiogram.enums import ParseMode
from aiogram.filters import Command, StateFilter
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

# ================= НАЛАШТУВАННЯ =================
ADMIN_ID = 1052964898  # ⚠️ ВПИШИ СВІЙ ID

bot = Bot(token=os.environ.get("BOT_TOKEN"))
dp = Dispatcher(storage=MemoryStorage())
items_data = {}; is_db_ready = False

scan_semaphore = asyncio.Semaphore(3) 
user_cooldowns = {}; active_scans = set() 

CITIES = ["Bridgewatch", "Martlock", "Lymhurst", "Thetford", "Fort Sterling", "Caerleon", "Brecilien", "Black Market"]
CITY_EMOJIS = {"Lymhurst":"🟢","Martlock":"🔵","Caerleon":"⚫","Thetford":"🟣","Bridgewatch":"🟠","Fort Sterling":"⚪","Brecilien":"🌸","Black Market":"💀"}
QUALITY_NAMES = {1:"Обычное", 2:"Хорошее", 3:"Выдающееся", 4:"Отличное", 5:"Шедевр"}
TRASH = ["Знаток ","Мастер ","Великий мастер ","Старейшина ","Ученик ","Новичок "]

class BotState(StatesGroup):
    waiting_for_buy_limit = State()
    waiting_for_profit_limit = State()
    picking_from = State()
    picking_to = State()

# ================= ФУНКЦІЇ ВІЗУАЛУ =================
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

def get_start_kb(): 
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="❓ Допомога"), KeyboardButton(text="💰 Налаштувати бюджет")]], resize_keyboard=True)

def get_main_kb(d):
    m = d.get("mode")
    if not m: return get_start_kb()
    m_l = "🌍 Охоплення: Всі міста" if m == "all" else "📍 Маршрут: Шлях"
    e_l = "🚫 Вимкнути фільтр 30хв" if d.get("extra") else "⚡ Свіжі ціни (30хв)"
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="🚀 Запустити сканер")],
        [KeyboardButton(text=m_l), KeyboardButton(text=e_l)],
        [KeyboardButton(text="🧮 Калькулятор"), KeyboardButton(text="💰 Налаштувати бюджет")],
        [KeyboardButton(text="🔄 Перезавантаження")]
    ], resize_keyboard=True)

def get_mode_inline(): 
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎲 Всі міста", callback_data="set_mode_all")],
        [InlineKeyboardButton(text="📍 Конкретний шлях", callback_data="set_mode_custom")]
    ])

# ================= ЛОГІКА СКАНУВАННЯ =================
async def download_items():
    global items_data, is_db_ready
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get("https://raw.githubusercontent.com/ao-data/ao-bin-dumps/master/formatted/items.json", timeout=60) as r:
                if r.status == 200:
                    data = await r.json(content_type=None)
                    allowed = ["weapon","armor","plate","leather","cloth","bag","cape","potion","meal","mount","tool","shapeshifter","offhand"]
                    items_data = {i["UniqueName"]: i for i in data if i.get("UniqueName","").startswith(("T4_","T5_","T6_","T7_","T8_")) and any(x in i.get("UniqueName","").lower() for x in allowed)}
    except: pass
    finally: is_db_ready = True

def fmt_t(s):
    if not s or s.startswith("0001"): return "???"
    try:
        dt = datetime.fromisoformat(s.split(".")[0].replace("Z","")).replace(tzinfo=UTC)
        m = int((datetime.now(UTC)-dt).total_seconds()/60)
        return f"{m}м" if m < 60 else f"{m//60}г"
    except: return "???"

async def scan_logic(d, f_c=None, t_c=None):
    res = []; b_l = d.get("buy_limit", 0); p_l = d.get("profit_limit", 4000); ext = d.get("extra", False)
    i_list = list(items_data.keys()); cities = [f_c, t_c] if f_c and t_c else CITIES
    async with aiohttp.ClientSession() as s:
        for i in range(0, len(i_list), 50):
            url = f"https://europe.albion-online-data.com/api/v2/stats/prices/{','.join(i_list[i:i+50])}?locations={','.join(cities)}"
            try:
                async with s.get(url, timeout=20) as resp:
                    data = await resp.json() if resp.status == 200 else []
                now = datetime.now(UTC)
                grouped = {}
                for e in data:
                    k = f"{e['item_id']}|{e['quality']}"; grouped.setdefault(k, {})[e['city']] = e
                for k, c_d in grouped.items():
                    i_id, q = k.split("|"); srcs = [f_c] if f_c else [c for c in c_d if c!="Black Market"]
                    for sc in srcs:
                        if sc not in c_d: continue
                        buy = c_d[sc].get('sell_price_min', 0)
                        if buy <= 500 or buy > b_l: continue
                        b_dt = datetime.fromisoformat(c_d[sc]['sell_price_min_date'].split(".")[0].replace("Z","")).replace(tzinfo=UTC)
                        if (now-b_dt).total_seconds()/60 > 180: continue
                        targets = [t_c] if t_c else [c for c in c_d if c!=sc]
                        for tc in targets:
                            if tc not in c_d: continue
                            sk = 'buy_price_max_date' if tc=="Black Market" else 'sell_price_min_date'
                            sell = c_d[tc].get('buy_price_max' if tc=="Black Market" else 'sell_price_min', 0)
                            if sell <= buy or (sell/buy) > 10: continue
                            s_dt = datetime.fromisoformat(c_d[tc].get(sk).split(".")[0].replace("Z","")).replace(tzinfo=UTC)
                            if (now-s_dt).total_seconds()/60 > 180: continue
                            p_n = int(sell*0.895-buy)
                            if p_n >= p_l:
                                if ext and ((now-b_dt).total_seconds()/60 > 30 or (now-s_dt).total_seconds()/60 > 30): continue
                                res.append({'id':i_id,'q':int(q),'from':sc,'to':tc,'buy':buy,'sell':sell,'p_p':int(sell*0.935-buy),'p_n':p_n,'bd':c_d[sc]['sell_price_min_date'],'sd':c_d[tc].get(sk)})
                if i % 300 == 0: await asyncio.sleep(0.2)
            except: continue
    return res
# ================= ОБРОБНИКИ ПОДІЙ =================
@dp.message(F.text == "🚀 Запустити сканер", StateFilter('*'))
async def main_search(m, state: FSMContext):
    u_id, now = m.from_user.id, datetime.now()
    is_admin = (u_id == ADMIN_ID)
    d = await state.get_data()
    if not is_admin:
        if u_id in active_scans: return await m.answer("⚠️ Твій запит ще обробляється!")
        if u_id in user_cooldowns and (now - user_cooldowns[u_id]).total_seconds() < 25:
            return await m.answer(f"⏳ Зачекай {int(25-(now-user_cooldowns[u_id]).total_seconds())} сек.")

    if not is_db_ready: return await m.answer("⏳ База вантажиться...")
    b = d.get("buy_limit", 0)
    if b <= 0: return await m.answer("💰 Встанови бюджет у меню!")
    
    mode = d.get("mode")
    if not mode: return await m.answer("🗺️ Обери режим!", reply_markup=get_mode_inline())

    if is_admin:
        s_msg = await m.answer("⚡ <b>Адмін-сканування...</b>", parse_mode=ParseMode.HTML, reply_markup=ReplyKeyboardRemove())
        res = await scan_logic(d, d.get('f_c'), d.get('t_c'))
        await s_msg.delete(); await disp_res(m, res); await m.answer(f"✅ Угод: {len(res)}", reply_markup=get_main_kb(d))
    else:
        async with scan_semaphore:
            active_scans.add(u_id); user_cooldowns[u_id] = now
            s_msg = await m.answer("🔍 Сканую Європу...", reply_markup=ReplyKeyboardRemove())
            res = await scan_logic(d, d.get('f_c'), d.get('t_c'))
            await s_msg.delete()
            if not res: await m.answer("📭 Нічого не знайдено.")
            else: await disp_res(m, res)
            await m.answer(f"✅ Готово! Знайдено: {len(res)}", reply_markup=get_main_kb(d))
            active_scans.remove(u_id)

@dp.message(F.text == "💰 Налаштувати бюджет", StateFilter('*'))
async def limit_menu(m, state: FSMContext):
    await state.set_state(None)
    d = await state.get_data()
    b, p = d.get("buy_limit", 0), d.get("profit_limit", 4000)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"💰 Бюджет ({b:,})", callback_data="set_limit_buy")],
        [InlineKeyboardButton(text=f"📈 Профіт ({p:,})", callback_data="set_limit_profit")]
    ])
    await m.answer("⚙️ Налаштування бюджету:", reply_markup=kb)

@dp.callback_query(F.data.startswith("set_limit_"), StateFilter('*'))
async def set_limit_cb(cb, state: FSMContext):
    await cb.answer(); t = cb.data.split("_")[2]; await cb.message.delete()
    cancel_kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="❌ Скасувати")]], resize_keyboard=True)
    if t == "buy":
        await state.set_state(BotState.waiting_for_buy_limit)
        await cb.message.answer("💰 Введи макс. ціну покупки (лише цифри):", reply_markup=cancel_kb)
    else:
        await state.set_state(BotState.waiting_for_profit_limit)
        await cb.message.answer("📈 Введи мін. чистий профіт:", reply_markup=cancel_kb)

@dp.message(F.text == "❌ Скасувати", StateFilter(BotState.waiting_for_buy_limit, BotState.waiting_for_profit_limit))
async def cancel_limit(m, state: FSMContext):
    d = await state.get_data(); await state.set_state(None)
    await m.answer("🚫 Введення скасовано.", reply_markup=get_main_kb(d))

@dp.message(StateFilter(BotState.waiting_for_buy_limit, BotState.waiting_for_profit_limit))
async def h_limits(m, state: FSMContext):
    try:
        v = int(m.text.replace(" ","").replace(",",""))
        curr = await state.get_state()
        if "buy" in str(curr): await state.update_data(buy_limit=v)
        else: await state.update_data(profit_limit=v)
        d = await state.get_data(); await state.set_state(None)
        await m.answer(f"✅ Збережено: {v:,}", reply_markup=get_main_kb(d))
        if not d.get("mode"): await m.answer("🗺 Тепер обери режим пошуку:", reply_markup=get_mode_inline())
        else: await m.answer("🚀 Тисни <b>\"Запустити сканер\"</b>", parse_mode=ParseMode.HTML)
    except: await m.answer("❌ Помилка! Введи тільки ціле число.")

@dp.callback_query(F.data.startswith("set_mode_"), StateFilter('*'))
async def set_mode_cb(cb, state: FSMContext):
    await cb.answer(); m = cb.data.split("_")[2]
    await state.update_data(mode=m); d = await state.get_data(); await cb.message.delete()
    if m == "all": 
        await cb.message.answer("🌍 Режим: <b>Всі міста</b> встановлено!\n\n🚀 Тисни <b>\"Запустити сканер\"</b>", reply_markup=get_main_kb(d), parse_mode=ParseMode.HTML)
    else:
        await state.set_state(BotState.picking_from)
        await cb.message.answer("📍 Звідки веземо товар?", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=f"{CITY_EMOJIS[c]} {c}", callback_data=f"city_{c}")] for c in CITIES if c!="Black Market"]))

@dp.callback_query(StateFilter(BotState.picking_from), F.data.startswith("city_"))
async def from_cb(cb, state: FSMContext):
    await cb.answer(); c = cb.data.split("_")[1]; await state.update_data(f_c=c); await state.set_state(BotState.picking_to); await cb.message.delete()
    await cb.message.answer(f"✅ Звідки: {c}\n📍 Тепер обери куди (Пункт Б):", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=f"{CITY_EMOJIS[ci]} {ci}", callback_data=f"city_{ci}")] for ci in CITIES if ci!=c and ci!="Black Market"]))

@dp.callback_query(StateFilter(BotState.picking_to), F.data.startswith("city_"))
async def to_cb(cb, state: FSMContext):
    await cb.answer(); t = cb.data.split("_")[1]; await state.update_data(t_c=t, mode="custom"); d = await state.get_data(); await state.set_state(None); await cb.message.delete()
    await cb.message.answer(f"🚀 Маршрут <b>{d['f_c']} ➔ {t}</b> встановлено!\n\n🚀 Тисни <b>\"Запустити сканер\"</b>", reply_markup=get_main_kb(d), parse_mode=ParseMode.HTML)

# ================= ВИВІД РЕЗУЛЬТАТІВ =================
async def disp_res(msg, res):
    if not res: return
    res.sort(key=lambda x: x['p_n'], reverse=True)
    full_text = ""; messages = []
    
    for idx, r in enumerate(res[:15], 1):
        b_id = r['id'].split("@")[0]
        enc = r['id'].split("@")[1] if "@" in r['id'] else "0"
        tier = b_id.split('_')[0][1:] # Отримуємо цифру Тіру (наприклад "4", "5")
        
        # Беремо назву з БД
        name = items_data.get(b_id, {}).get("LocalizedNames", {}).get("RU-RU", b_id)
        
        # ВИДАЛЯЄМО все, що в дужках (наприклад " (Эксперт)", " (Мастер)")
        name = re.sub(r'\s*\([^)]*\)', '', name)
        
        # Додатково чистимо від сміття без дужок (якщо таке є)
        for t in TRASH: name = name.replace(t, "")
        
        icon = get_item_icon(b_id)
        
        item_text = (f"{idx}) {icon} <b>{name.upper()}</b> 🔸 <b>[{tier}.{enc}]</b>\n"
                     f"✨ Качество: <b>{QUALITY_NAMES.get(r['q'], 'Обычное')}</b>\n"
                     f"──────────────────\n"
                     f"📥 <b>КУПІВЛЯ:</b> {CITY_EMOJIS[r['from']]} {r['from']}\n"
                     f"💰 Цена: <code>{r['buy']:,}</code> (⏳ <b>{fmt_t(r['bd'])}</b>)\n\n"
                     f"📤 <b>ПРОДАЖ:</b> {CITY_EMOJIS[r['to']]} {r['to']}\n"
                     f"💰 Цена: <code>{r['sell']:,}</code> (⏳ <b>{fmt_t(r['sd'])}</b>)\n"
                     f"──────────────────\n"
                     f"💵 <b>ПРИБУТОК:</b>\n"
                     f"👑 Преміум: <code>+{r['p_p']:,}</code>\n"
                     f"💀 Без према: <code>+{r['p_n']:,}</code>\n"
                     f"──────────────────\n\n")
        
        if len(full_text) + len(item_text) > 3900: 
            messages.append(full_text)
            full_text = item_text
        else: 
            full_text += item_text
            
    if full_text: messages.append(full_text)
    for text in messages: await msg.answer(text, parse_mode=ParseMode.HTML)

@dp.message(F.text == "🔄 Перезавантаження", StateFilter('*'))
async def btn_res(m, state: FSMContext):
    if m.from_user.id == ADMIN_ID:
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📥 Оновити БД", callback_data="adm_upd")],[InlineKeyboardButton(text="✅ Скинути все", callback_data="conf_res"), InlineKeyboardButton(text="❌ Ні", callback_data="cancel_res")]])
        await m.answer("🛠 Адмін-панель:", reply_markup=kb)
    else:
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Скинути", callback_data="conf_res"), InlineKeyboardButton(text="❌ Ні", callback_data="cancel_res")]])
        await m.answer("⚠️ Скинути твої дані?", reply_markup=kb)

@dp.callback_query(F.data == "adm_upd")
async def adm_upd(cb):
    if cb.from_user.id == ADMIN_ID: asyncio.create_task(download_items()); await cb.message.answer("✅ База оновлюється...")
    await cb.answer()

@dp.callback_query(F.data == "conf_res")
async def conf_res(cb, state: FSMContext): await state.clear(); await cb.answer(); await cb.message.delete(); await cb.message.answer("🔄 Все скинуто!", reply_markup=get_start_kb())

@dp.callback_query(F.data == "cancel_res")
async def cancel_res(cb): await cb.answer(); await cb.message.delete()

@dp.message(Command("start"), StateFilter('*'))
async def cmd_start(m, state: FSMContext): await state.clear(); await m.answer("👋 Бот готовий!", reply_markup=get_start_kb())

async def main():
    await bot.delete_webhook(drop_pending_updates=True)
    asyncio.create_task(download_items()); await dp.start_polling(bot)

if __name__ == "__main__": asyncio.run(main())

