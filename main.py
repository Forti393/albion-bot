import os
import json
import aiohttp
import asyncio
from datetime import datetime, timedelta, UTC
from aiogram import Bot, Dispatcher, types, F
from aiogram.enums import ParseMode
from aiogram.filters import Command, StateFilter
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

# ================= НАЛАШТУВАННЯ =================
ADMIN_ID = 1052964898  # ⚠️ ВПИШИ СВІЙ TELEGRAM ID СЮДИ

MARKET_BASE_URL = "https://europe.albion-online-data.com"
MARKET_PATH = "/api/v2/stats/prices/{}?locations={}"
ITEMS_URL = "https://raw.githubusercontent.com/ao-data/ao-bin-dumps/master/formatted/items.json"

CITIES = ["Bridgewatch", "Martlock", "Lymhurst", "Thetford", "Fort Sterling", "Caerleon", "Brecilien", "Black Market"]
CITY_EMOJIS = {"Lymhurst": "🟢", "Martlock": "🔵", "Caerleon": "⚫", "Thetford": "🟣", "Bridgewatch": "🟠", "Fort Sterling": "⚪", "Brecilien": "🌸", "Black Market": "💀"}
QUALITY_NAMES = {1: "Обычное", 2: "Хорошее", 3: "Выдающееся", 4: "Отличное", 5: "Шедевр"}
TRASH_WORDS = ["Знаток ", "Мастер ", "Великий мастер ", "Старейшина ", "Ученик ", "Новичок "]

class BotState(StatesGroup):
    waiting_for_buy_limit = State()
    waiting_for_profit_limit = State()
    picking_from = State()
    picking_to = State()
    calc_count = State()
    calc_buy = State()
    calc_sell = State()

bot = Bot(token=os.environ.get("BOT_TOKEN"))
dp = Dispatcher(storage=MemoryStorage())
items_data = {}
is_db_ready = False

# ================= КЛАВІАТУРИ =================
def get_start_kb():
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="❓ Допомога"), KeyboardButton(text="⚙️ Ліміт")]], resize_keyboard=True)

def get_main_kb(user_data):
    mode = user_data.get("mode", "Не обрано")
    mode_label = "Всі" if mode == "all" else ("Шлях" if mode == "custom" else "Не обрано")
    extra = user_data.get("extra", False)
    extra_label = "🚫 Екстра відміна" if extra else "⚡ Екстра тестування"
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🔍 Пошук")],
            [KeyboardButton(text=f"🗺️ Режими ({mode_label})"), KeyboardButton(text=extra_label)],
            [KeyboardButton(text="🧮 Калькулятор"), KeyboardButton(text="⚙️ Ліміт")],
            [KeyboardButton(text="🔄 Перезавантаження")]
        ], resize_keyboard=True
    )

def get_limits_inline(user_data):
    b_lim = user_data.get("buy_limit", 0)
    p_lim = user_data.get("profit_limit", 4000)
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"💰 Ліміт купівлі ({b_lim:,})", callback_data="set_limit_buy")],
        [InlineKeyboardButton(text=f"📈 Мін. прибуток ({p_lim:,})", callback_data="set_limit_profit")]
    ])

def get_mode_inline():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎲 Рандом міста (Всі)", callback_data="set_mode_all")],
        [InlineKeyboardButton(text="📍 На вибір (Шлях)", callback_data="set_mode_custom")]
    ])

def get_city_inline(exclude_city=None):
    buttons = [[InlineKeyboardButton(text=f"{CITY_EMOJIS[c]} {c}", callback_data=f"city_{c}")] for c in CITIES if c != "Black Market" and c != exclude_city]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# ================= ЛОГІКА =================
async def download_items():
    global items_data, is_db_ready
    is_db_ready = False
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as s:
            async with s.get(ITEMS_URL) as r:
                if r.status == 200:
                    raw = await r.json(content_type=None)
                    allowed = ["weapon", "armor", "plate", "leather", "cloth", "bag", "cape", "potion", "meal", "mount", "relic", "artefact", "tool", "shapeshifter"]
                    items_data = {i["UniqueName"]: i for i in raw if i.get("UniqueName", "").startswith(("T4_", "T5_", "T6_", "T7_", "T8_")) and any(x in i.get("UniqueName", "").lower() for x in allowed)}
    except: pass
    finally: is_db_ready = True

def format_time(date_str):
    if not date_str or date_str.startswith("0001"): return "???"
    try:
        dt = datetime.fromisoformat(date_str.split(".")[0].replace("Z", "")).replace(tzinfo=UTC)
        m = int((datetime.now(UTC) - dt).total_seconds() / 60)
        return f"{m}м" if m < 60 else f"{m//60}г"
    except: return "???"

async def scan_logic(user_data, from_c=None, to_c=None):
    results = []
    b_lim = user_data.get("buy_limit", 0)
    p_lim = user_data.get("profit_limit", 4000)
    extra = user_data.get("extra", False)
    item_list = list(items_data.keys())
    cities = [from_c, to_c] if from_c and to_c else CITIES
    
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=45)) as s:
        for i in range(0, len(item_list), 50):
            url = f"{MARKET_BASE_URL}{MARKET_PATH.format(','.join(item_list[i:i+50]), ','.join(cities))}"
            try:
                async with s.get(url) as resp:
                    data = await resp.json() if resp.status == 200 else []
            except: continue
            grouped = {f"{e['item_id']}|{e['quality']}": {} for e in data}
            for e in data: grouped[f"{e['item_id']}|{e['quality']}"][e['city']] = e
            now = datetime.now(UTC)
            for k, city_data in grouped.items():
                i_id, qual = k.split("|")
                sources = [from_c] if from_c else [c for c in city_data if c != "Black Market"]
                for f_c in sources:
                    if f_c not in city_data: continue
                    buy = city_data[f_c].get('sell_price_min', 0)
                    if buy <= 100 or buy > b_lim: continue
                    
                    buy_dt = datetime.fromisoformat(city_data[f_c]['sell_price_min_date'].split(".")[0].replace("Z", "")).replace(tzinfo=UTC)
                    b_age = (now - buy_dt).total_seconds() / 60
                    if b_age > 180: continue
                    
                    targets = [to_c] if to_c else [c for c in city_data if c != f_c]
                    for t_c in targets:
                        if t_c not in city_data: continue
                        sd_k = 'buy_price_max_date' if t_c == "Black Market" else 'sell_price_min_date'
                        sell = city_data[t_c].get('buy_price_max' if t_c == "Black Market" else 'sell_price_min', 0)
                        if sell <= buy or (sell/buy) > 10: continue
                        
                        sell_dt = datetime.fromisoformat(city_data[t_c].get(sd_k).split(".")[0].replace("Z", "")).replace(tzinfo=UTC)
                        s_age = (now - sell_dt).total_seconds() / 60
                        if s_age > 180 or (extra and (b_age > 30 or s_age > 30)): continue
                        
                        p_p, p_n = int(sell * 0.935 - buy), int(sell * 0.895 - buy)
                        if p_n >= p_lim:
                            results.append({'id':i_id,'q':int(qual),'from':f_c,'to':t_c,'buy':buy,'sell':sell,'p_p':p_p,'p_n':p_n,'bd':city_data[f_c]['sell_price_min_date'],'sd':city_data[t_c].get(sd_k)})
    return results

# ================= ОБРОБНИКИ =================
@dp.message(Command("start"), StateFilter('*'))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("👋 <b>Вітаю!</b>\nНалаштуй бота для пошуку прибутку.", reply_markup=get_start_kb(), parse_mode=ParseMode.HTML)

@dp.message(F.text == "⚙️ Ліміт", StateFilter('*'))
async def limit_menu(message: types.Message, state: FSMContext):
    data = await state.get_data()
    await message.answer("⚙️ <b>Твої ліміти:</b>", reply_markup=get_limits_inline(data), parse_mode=ParseMode.HTML)

@dp.message(F.text == "🔍 Пошук", StateFilter('*'))
async def main_search(message: types.Message, state: FSMContext):
    if not is_db_ready: return await message.answer("⏳ База завантажується...")
    data = await state.get_data()
    if data.get("buy_limit", 0) <= 0: return await message.answer("Встанови ліміт купівлі!")
    mode = data.get("mode")
    if not mode: return await message.answer("Обери режим!", reply_markup=get_mode_inline())
    
    if mode == "all":
        await message.answer(f"🔍 Шукаю... (Ліміт: {data['buy_limit']:,})", reply_markup=ReplyKeyboardRemove())
        res = await scan_logic(data)
        await display_results(message, res)
        await message.answer("Завершено.", reply_markup=get_main_kb(data))
    else:
        await message.answer("📍 Звідки?", reply_markup=get_city_inline())
        await state.set_state(BotState.picking_from)

@dp.message(F.text.startswith("🗺️ Режими"), StateFilter('*'))
async def modes_btn(message: types.Message):
    await message.answer("Обери режим пошуку:", reply_markup=get_mode_inline())

@dp.message(F.text.contains("Екстра"), StateFilter('*'))
async def toggle_extra(message: types.Message, state: FSMContext):
    data = await state.get_data()
    new_val = not data.get("extra", False)
    await state.update_data(extra=new_val)
    updated_data = await state.get_data()
    await message.answer(f"⚡ Екстра: {'УВІМК' if new_val else 'ВИМК'}", reply_markup=get_main_kb(updated_data))

@dp.callback_query(F.data.startswith("set_limit_"), StateFilter('*'))
async def set_limit_callback(callback: types.CallbackQuery, state: FSMContext):
    l_type = callback.data.split("_")[2]
    if l_type == "buy":
        await callback.message.edit_text("💰 Макс. ціна покупки:")
        await state.set_state(BotState.waiting_for_buy_limit)
    else:
        await callback.message.edit_text("📈 Мін. прибуток (💀):")
        await state.set_state(BotState.waiting_for_profit_limit)
    await callback.answer()

@dp.message(StateFilter(BotState.waiting_for_buy_limit))
async def h_buy_limit(message: types.Message, state: FSMContext):
    try:
        val = int(message.text.replace(" ",""))
        await state.update_data(buy_limit=val)
        data = await state.get_data()
        if not data.get("mode"):
            await message.answer(f"✅ Ліміт {val:,} збережено! Тепер обери режим:", reply_markup=get_mode_inline())
        else:
            await message.answer(f"✅ Ліміт {val:,} збережено!", reply_markup=get_main_kb(data))
        await state.set_state(None)
    except: await message.answer("❌ Введи число!")

@dp.message(StateFilter(BotState.waiting_for_profit_limit))
async def h_profit_limit(message: types.Message, state: FSMContext):
    try:
        val = int(message.text.replace(" ",""))
        await state.update_data(profit_limit=val)
        data = await state.get_data()
        await message.answer(f"✅ Прибуток від {val:,} збережено!", reply_markup=get_main_kb(data))
        await state.set_state(None)
    except: await message.answer("❌ Введи число!")

@dp.callback_query(F.data.startswith("set_mode_"), StateFilter('*'))
async def set_mode_cb(callback: types.CallbackQuery, state: FSMContext):
    m = callback.data.split("_")[2]
    await state.update_data(mode=m)
    data = await state.get_data()
    if m == "all": await callback.message.answer("✅ Обрано 'Всі міста'.", reply_markup=get_main_kb(data))
    else: await callback.message.answer("📍 Звідки веземо?", reply_markup=get_city_inline())
    await callback.answer()

@dp.callback_query(StateFilter(BotState.picking_from))
async def from_city_cb(callback: types.CallbackQuery, state: FSMContext):
    city = callback.data.split("_")[1]
    await state.update_data(f_c=city)
    await callback.message.edit_text(f"Звідки: {city}\n📍 Куди?", reply_markup=get_city_inline(city))
    await state.set_state(BotState.picking_to)

@dp.callback_query(StateFilter(BotState.picking_to))
async def to_city_cb(callback: types.CallbackQuery, state: FSMContext):
    t_c = callback.data.split("_")[1]
    data = await state.get_data()
    f_c = data.get('f_c')
    await callback.message.edit_text(f"🚀 {f_c} ➔ {t_c}...")
    res = await scan_logic(data, f_c, t_c)
    await display_results(callback.message, res)
    await message.answer("Завершено.", reply_markup=get_main_kb(data))
    await state.set_state(None)

async def display_results(msg, res):
    if not res: return await msg.answer("Нічого не знайдено.")
    res.sort(key=lambda x: x['p_n'], reverse=True)
    for r in res[:15]:
        b_id = r['id'].split("@")[0]; enc = r['id'].split("@")[1] if "@" in r['id'] else "0"
        name = items_data.get(b_id, {}).get("LocalizedNames", {}).get("RU-RU", b_id)
        for t in TRASH_WORDS: name = name.replace(t, "")
        tier = b_id.split("_")[0][1:]
        from_emoji = CITY_EMOJIS.get(r['from'], "")
        to_emoji = CITY_EMOJIS.get(r['to'], "")
        await msg.answer(f"📦 <b>{name} [{tier}.{enc}]</b>\n🛒 {from_emoji} {r['from']}: <b>{r['buy']:,}</b> (⏳{format_time(r['bd'])})\n💰 {to_emoji} {r['to']}: <b>{r['sell']:,}</b> (⏳{format_time(r['sd'])})\n👑 П: <b>{r['p_p']:,}</b> | 💀: <b>{r['p_n']:,}</b>", parse_mode=ParseMode.HTML)

@dp.message(F.text == "🔄 Перезавантаження", StateFilter('*'))
async def btn_restart(message: types.Message, state: FSMContext):
    if message.from_user.id == ADMIN_ID:
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📥 Оновити БД", callback_data="admin_upd")],[InlineKeyboardButton(text="🔄 Скинути", callback_data="conf_res")]])
        await message.answer("🛠 Адмін панель:", reply_markup=kb)
    else:
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Так", callback_data="conf_res"), InlineKeyboardButton(text="❌ Ні", callback_data="cancel_res")]])
        await message.answer("⚠️ Скинути твої налаштування?", reply_markup=kb)

@dp.callback_query(F.data == "admin_upd")
async def adm_upd(cb: types.CallbackQuery):
    if cb.from_user.id == ADMIN_ID:
        asyncio.create_task(download_items()); await cb.message.edit_text("✅ Запущено оновлення!")
    await cb.answer()

@dp.callback_query(F.data == "conf_res")
async def conf_res(cb: types.CallbackQuery, state: FSMContext):
    await state.clear(); await cb.message.answer("🔄 Скинуто!", reply_markup=get_start_kb())
    await cb.answer()

@dp.message(F.text == "🧮 Калькулятор", StateFilter('*'))
async def calc_start(m: types.Message, state: FSMContext):
    await state.set_state(BotState.calc_count); await m.answer("📦 Кількість:", reply_markup=ReplyKeyboardRemove())

@dp.message(StateFilter(BotState.calc_count))
async def calc_1(m: types.Message, state: FSMContext):
    try:
        await state.update_data(c=int(m.text)); await m.answer("💰 Ціна КУПІВЛІ (1 шт):"); await state.set_state(BotState.calc_buy)
    except: await m.answer("Введи число!")

@dp.message(StateFilter(BotState.calc_buy))
async def calc_2(m: types.Message, state: FSMContext):
    try:
        await state.update_data(b=int(m.text)); await m.answer("📤 Ціна ПРОДАЖУ (1 шт):"); await state.set_state(BotState.calc_sell)
    except: await m.answer("Введи число!")

@dp.message(StateFilter(BotState.calc_sell))
async def calc_3(m: types.Message, state: FSMContext):
    try:
        s = int(m.text); d = await state.get_data(); cnt, b = d['c'], d['b']
        data = await state.get_data()
        await m.answer(f"📊 <b>Результат:</b>\n👑 П: {(int(s*0.935-b)*cnt):,}\n💀 Б: {(int(s*0.895-b)*cnt):,}", reply_markup=get_main_kb(data), parse_mode=ParseMode.HTML)
        await state.set_state(None)
    except: await m.answer("Введи число!")

async def main():
    try: await bot.delete_webhook(drop_pending_updates=True)
    except: pass
    asyncio.create_task(download_items()); await dp.start_polling(bot)

if __name__ == "__main__": asyncio.run(main())
