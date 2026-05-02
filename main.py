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
ADMIN_ID = 1052964898  # ⚠️ ВПИШИ СВІЙ ID

bot = Bot(token=os.environ.get("BOT_TOKEN"))
dp = Dispatcher(storage=MemoryStorage())
items_data = {}; is_db_ready = False

# Контроль навантаження
scan_semaphore = asyncio.Semaphore(3) 
user_cooldowns = {} 
active_scans = set() 

CITIES = ["Bridgewatch", "Martlock", "Lymhurst", "Thetford", "Fort Sterling", "Caerleon", "Brecilien", "Black Market"]
CITY_EMOJIS = {"Lymhurst":"🟢","Martlock":"🔵","Caerleon":"⚫","Thetford":"🟣","Bridgewatch":"🟠","Fort Sterling":"⚪","Brecilien":"🌸","Black Market":"💀"}
QUALITY_NAMES = {1:"Обычное", 2:"Хорошее", 3:"Выдающееся", 4:"Отличное", 5:"Шедевр"}
TRASH = ["Знаток ","Мастер ","Великий мастер ","Старейшина ","Ученик ","Новичок "]

class BotState(StatesGroup):
    waiting_for_buy_limit = State(); waiting_for_profit_limit = State()
    picking_from = State(); picking_to = State()
    calc_count = State(); calc_buy = State(); calc_sell = State()

# ================= КЛАВІАТУРИ (ОНОВЛЕНІ) =================
def get_start_kb(): 
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="❓ Допомога"), KeyboardButton(text="💰 Налаштувати бюджет")]], resize_keyboard=True)

def get_main_kb(d):
    m = d.get("mode","all")
    mode_label = "🌍 Охоплення: Всі міста" if m == "all" else "📍 Маршрут: Шлях"
    extra_label = "🚫 Вимкнути фільтр 30хв" if d.get("extra") else "⚡ Свіжі ціни (30хв)"
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="🚀 Запустити сканер")],
        [KeyboardButton(text=mode_label), KeyboardButton(text=extra_label)],
        [KeyboardButton(text="🧮 Калькулятор"), KeyboardButton(text="💰 Налаштувати бюджет")],
        [KeyboardButton(text="🔄 Перезавантаження")]
    ], resize_keyboard=True)

def get_mode_inline(): 
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎲 Всі міста", callback_data="set_mode_all")],
        [InlineKeyboardButton(text="📍 Конкретний шлях", callback_data="set_mode_custom")]
    ])

def get_city_inline(ex=None): 
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=f"{CITY_EMOJIS[c]} {c}", callback_data=f"city_{c}")] for c in CITIES if c!="Black Market" and c!=ex])

# ================= ЛОГІКА =================
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

def fmt_t(s):
    if not s or s.startswith("0001"): return "???"
    try:
        dt = datetime.fromisoformat(s.split(".")[0].replace("Z","")).replace(tzinfo=UTC)
        m = int((datetime.now(UTC)-dt).total_seconds()/60)
        return f"{m}м" if m < 60 else f"{m//60}г"
    except: return "???"

def get_item_icon(unique_name):
    un = unique_name.lower()
    if any(x in un for x in ["sword", "axe", "bow", "staff", "hammer", "mace", "dagger", "spear"]): return "🗡"
    if any(x in un for x in ["armor", "jacket", "robe"]): return "🧥"
    if any(x in un for x in ["helmet", "hood", "cowl"]): return "🪖"
    if "shoes" in un or "boots" in un: return "🥾"
    if "mount" in un: return "🐴"
    if "bag" in un: return "🎒"
    if "cape" in un: return "🧣"
    return "📦"

async def scan_logic(d, f_c=None, t_c=None):
    res = []; b_l = d.get("buy_limit",0); p_l = d.get("profit_limit",4000); ext = d.get("extra",False)
    i_list = list(items_data.keys()); cities = [f_c, t_c] if f_c and t_c else CITIES
    async with aiohttp.ClientSession() as s:
        for i in range(0, len(i_list), 50):
            batch = i_list[i:i+50]
            url = f"https://europe.albion-online-data.com/api/v2/stats/prices/{','.join(batch)}?locations={','.join(cities)}"
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
                            if p_n >= p_l:
                                if ext and ((now-b_dt).total_seconds()/60 > 30 or (now-s_dt).total_seconds()/60 > 30): continue
                                res.append({'id':i_id,'q':int(q),'from':sc,'to':tc,'buy':buy,'sell':sell,'p_p':int(sell*0.935-buy),'p_n':p_n,'bd':c_d[sc]['sell_price_min_date'],'sd':c_d[tc].get(sk)})
                if i % 250 == 0: await asyncio.sleep(0.3)
            except: continue
    return res

# ================= ОФОРМЛЕННЯ РЕЗУЛЬТАТІВ =================
async def disp_res(msg, res):
    if not res: return
    res.sort(key=lambda x: x['p_n'], reverse=True)
    for r in res[:15]:
        b_id = r['id'].split("@")[0]
        enc = r['id'].split("@")[1] if "@" in r['id'] else "0"
        tier = b_id.split("_")[0][1:]
        name = items_data.get(b_id, {}).get("LocalizedNames", {}).get("RU-RU", b_id)
        for t in TRASH: name = name.replace(t, "")
        q_name = QUALITY_NAMES.get(r['q'], "Обычное")
        icon = get_item_icon(b_id)
        
        text = (
            f"{icon} <b>{name.upper()}</b> <code>[{tier}.{enc}]</code>\n"
            f"✨ Качество: <b>{q_name}</b>\n"
            f"──────────────────\n"
            f"📥 <b>КУПІВЛЯ:</b> {CITY_EMOJIS[r['from']]} {r['from']}\n"
            f"💰 Ціна: <code>{r['buy']:,}</code>\n"
            f"⏳ Оновлено: <b>{fmt_t(r['bd'])}</b> тому\n\n"
            f"📤 <b>ПРОДАЖ:</b> {CITY_EMOJIS[r['to']]} {r['to']}\n"
            f"💰 Ціна: <code>{r['sell']:,}</code>\n"
            f"⏳ Оновлено: <b>{fmt_t(r['sd'])}</b> тому\n"
            f"──────────────────\n"
            f"💵 <b>ЧИСТИЙ ПРИБУТОК:</b>\n"
            f"👑 З Преміумом:  <code>+{r['p_p']:,}</code>\n"
            f"💀 Без Преміуму: <code>+{r['p_n']:,}</code>\n"
            f"──────────────────"
        )
        await msg.answer(text, parse_mode=ParseMode.HTML)

# ================= ОБРОБНИКИ =================
@dp.message(F.text == "🚀 Запустити сканер", StateFilter('*'))
async def main_search(m, state: FSMContext):
    u_id = m.from_user.id
    is_admin = (u_id == ADMIN_ID)
    now = datetime.now()

    if not is_admin:
        if u_id in active_scans: return await m.answer("⚠️ Твій запит вже обробляється!")
        if u_id in user_cooldowns:
            diff = (now - user_cooldowns[u_id]).total_seconds()
            if diff < 30: return await m.answer(f"⏳ Зачекай {int(30-diff)} сек.")

    if not is_db_ready: return await m.answer("⏳ База ще вантажиться...")
    d = await state.get_data(); b = d.get("buy_limit", 0); mode = d.get("mode", "all")
    if b <= 0: return await m.answer("💰 Спочатку встанови бюджет!")

    if is_admin:
        s_msg = await m.answer("⚡ <b>Адмін-сканування активовано...</b>", parse_mode=ParseMode.HTML, reply_markup=ReplyKeyboardRemove())
        res = await scan_logic(d, d.get('f_c'), d.get('t_c'))
        await s_msg.delete(); await disp_res(m, res); await m.answer(f"✅ Знайдено угод: {len(res)}", reply_markup=get_main_kb(d))
    else:
        if scan_semaphore.locked(): await m.answer("🕒 Черга заповнена. Пошук почнеться автоматично...")
        try:
            active_scans.add(u_id)
            async with scan_semaphore:
                user_cooldowns[u_id] = now
                s_msg = await m.answer("🔍 Сканую ринок Європи...", reply_markup=ReplyKeyboardRemove())
                res = await scan_logic(d, d.get('f_c'), d.get('t_c'))
                await s_msg.delete()
                if not res: await m.answer("📭 Нічого не знайдено.")
                else: await disp_res(m, res)
                await m.answer(f"✅ Готово! Угод: {len(res)}", reply_markup=get_main_kb(d))
        finally:
            if u_id in active_scans: active_scans.remove(u_id)

@dp.message(F.text.contains("Охоплення") | F.text.contains("Маршрут"), StateFilter('*'))
async def modes_btn(m): await m.answer("🗺 Обери спосіб сканування:", reply_markup=get_mode_inline())

@dp.message(F.text.contains("ціни (30хв)") | F.text.contains("Вимкнути фільтр"), StateFilter('*'))
async def toggle_extra(m, state: FSMContext):
    d = await state.get_data(); val = not d.get("extra", False); await state.update_data(extra=val)
    d = await state.get_data(); await m.answer(f"⚡ Фільтр 30хв: {'УВІМКНЕНО' if val else 'ВИМКНЕНО'}", reply_markup=get_main_kb(d))

@dp.message(Command("start"), StateFilter('*'))
async def cmd_start(m, state: FSMContext):
    await state.clear(); await m.answer("👋 Бот готовий до пошуку прибутку!", reply_markup=get_start_kb())

@dp.message(F.text == "💰 Налаштувати бюджет", StateFilter('*'))
async def limit_menu(m, state: FSMContext):
    d = await state.get_data(); b = d.get("buy_limit",0); p = d.get("profit_limit",4000)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=f"💰 Бюджет ({b:,})", callback_data="set_limit_buy")],[InlineKeyboardButton(text=f"📈 Мін. Профіт ({p:,})", callback_data="set_limit_profit")]])
    await m.answer("⚙️ <b>Твої налаштування:</b>", reply_markup=kb, parse_mode=ParseMode.HTML)

@dp.callback_query(F.data.startswith("set_limit_"), StateFilter('*'))
async def set_limit_cb(cb, state: FSMContext):
    t = cb.data.split("_")[2]; await cb.message.edit_text("💰 Скільки максимум срібла витрачаємо на 1 предмет?" if t=="buy" else "📈 Який чистий профіт (💀) шукати?")
    await state.set_state(BotState.waiting_for_buy_limit if t=="buy" else BotState.waiting_for_profit_limit); await cb.answer()

@dp.message(StateFilter(BotState.waiting_for_buy_limit, BotState.waiting_for_profit_limit))
async def h_limits(m, state: FSMContext):
    try:
        v = int(m.text.replace(" ","").replace(",","")); curr = await state.get_state()
        if "buy" in curr: await state.update_data(buy_limit=v)
        else: await state.update_data(profit_limit=v)
        d = await state.get_data(); await state.set_state(None)
        await m.answer(f"✅ Збережено: {v:,}", reply_markup=get_main_kb(d) if d.get("mode") else get_mode_inline())
    except: await m.answer("❌ Будь ласка, введи тільки число!")

@dp.callback_query(F.data.startswith("set_mode_"), StateFilter('*'))
async def set_mode_cb(cb, state: FSMContext):
    m = cb.data.split("_")[2]; await state.update_data(mode=m); d = await state.get_data()
    if m=="all": await cb.message.answer("✅ Тепер скануємо всі міста!", reply_markup=get_main_kb(d))
    else: await cb.message.answer("📍 Звідки веземо товар?", reply_markup=get_city_inline())
    await cb.answer()

@dp.callback_query(StateFilter(BotState.picking_from))
async def from_cb(cb, state: FSMContext):
    c = cb.data.split("_")[1]; await state.update_data(f_c=c); await cb.message.edit_text(f"Звідки: {c}\n📍 Куди?", reply_markup=get_city_inline(c)); await state.set_state(BotState.picking_to)

@dp.callback_query(StateFilter(BotState.picking_to))
async def to_cb(cb, state: FSMContext):
    t = cb.data.split("_")[1]; await state.update_data(t_c=t, mode="custom"); d = await state.get_data()
    await cb.message.answer(f"✅ Маршрут {d['f_c']} ➔ {t} встановлено!", reply_markup=get_main_kb(d)); await state.set_state(None); await cb.answer()

@dp.message(F.text == "🧮 Калькулятор", StateFilter('*'))
async def calc_start(m, state: FSMContext):
    await state.set_state(BotState.calc_count); await m.answer("📦 Введи кількість предметів:", reply_markup=ReplyKeyboardRemove())

@dp.message(StateFilter(BotState.calc_count, BotState.calc_buy, BotState.calc_sell))
async def h_calc(m, state: FSMContext):
    try:
        v = int(m.text.replace(" ","")); curr = await state.get_state()
        if "count" in curr: await state.update_data(c=v); await m.answer("💰 Ціна КУПІВЛІ (за 1 шт):"); await state.set_state(BotState.calc_buy)
        elif "buy" in curr: await state.update_data(b=v); await m.answer("📤 Ціна ПРОДАЖУ (за 1 шт):"); await state.set_state(BotState.calc_sell)
        else:
            d = await state.get_data(); cnt, buy = d['c'], d['b']
            await m.answer(f"📊 <b>Результат:</b>\n👑 П: {(int(v*0.935-buy)*cnt):,}\n💀 Б: {(int(v*0.895-buy)*cnt):,}", reply_markup=get_main_kb(d), parse_mode=ParseMode.HTML); await state.set_state(None)
    except: await m.answer("❌ Введи число!")

@dp.message(F.text == "🔄 Перезавантаження", StateFilter('*'))
async def btn_res(m, state: FSMContext):
    if m.from_user.id == ADMIN_ID:
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📥 Оновити БД (Адмін)", callback_data="adm_upd")],[InlineKeyboardButton(text="🔄 Скинути все", callback_data="conf_res")]])
        await m.answer("🛠 Адмін-панель:", reply_markup=kb)
    else:
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Так, скинути", callback_data="conf_res"), InlineKeyboardButton(text="❌ Ні", callback_data="cancel_res")]])
        await m.answer("⚠️ Скинути твої налаштування?", reply_markup=kb)

@dp.callback_query(F.data == "adm_upd")
async def adm_upd(cb):
    if cb.from_user.id == ADMIN_ID: asyncio.create_task(download_items()); await cb.message.edit_text("✅ База оновлюється...")
    await cb.answer()

@dp.callback_query(F.data == "conf_res")
async def conf_res(cb, state: FSMContext): await state.clear(); await cb.message.answer("🔄 Все скинуто!", reply_markup=get_start_kb()); await cb.answer()

@dp.callback_query(F.data == "cancel_res")
async def cancel_res(cb): await cb.message.delete(); await cb.answer()

@dp.message(F.text == "❓ Допомога", StateFilter('*'))
async def cmd_help(m):
    await m.answer("📖 <b>Як працює Albion Trader?</b>\n\n1. Натисни <b>💰 Налаштувати бюджет</b>.\n2. Обери режим сканування (Всі міста або конкретний шлях).\n3. Тисни <b>🚀 Запустити сканер</b>.\n\nБот покаже топ-15 угод з найбільшим чистим прибутком!", parse_mode=ParseMode.HTML)

async def main():
    try: await bot.delete_webhook(drop_pending_updates=True)
    except: pass
    asyncio.create_task(download_items()); await dp.start_polling(bot)

if __name__ == "__main__": asyncio.run(main())
