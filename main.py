import os
import json
import aiohttp
import asyncio
from datetime import datetime, UTC, timedelta

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.filters import Command, StateFilter
from aiogram.types import (
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

# ================= НАЛАШТУВАННЯ =================
ADMIN_ID = 1052964898  # ⚠️ ВПИШИ СВІЙ ID
ITEMS_CACHE_FILE = "items_cache.json"
ITEMS_CACHE_TTL_HOURS = 24

bot = Bot(token=os.environ.get("BOT_TOKEN"))
dp = Dispatcher(storage=MemoryStorage())

items_data = {}
is_db_ready = False

scan_semaphore = asyncio.Semaphore(3)
user_cooldowns = {}
active_scans = set()

CITIES = [
    "Bridgewatch", "Martlock", "Lymhurst", "Thetford",
    "Fort Sterling", "Caerleon", "Brecilien", "Black Market"
]

CITY_EMOJIS = {
    "Lymhurst": "🟢", "Martlock": "🔵", "Caerleon": "⚫", "Thetford": "🟣",
    "Bridgewatch": "🟠", "Fort Sterling": "⚪", "Brecilien": "🌸", "Black Market": "💀"
}

QUALITY_NAMES = {
    1: "Обычное", 2: "Хорошее", 3: "Выдающееся",
    4: "Отличное", 5: "Шедевр"
}

TRASH = ["Знаток ", "Мастер ", "Великий мастер ", "Старейшина ", "Ученик ", "Новичок "]


class BotState(StatesGroup):
    waiting_for_buy_limit = State()
    waiting_for_profit_limit = State()
    picking_from = State()
    picking_to = State()
    calc_count = State()
    calc_buy = State()
    calc_sell = State()


# ================= КЛАВІАТУРИ =================
def get_start_kb():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="❓ Допомога"),
             KeyboardButton(text="💰 Налаштувати бюджет")]
        ],
        resize_keyboard=True
    )


def get_main_kb(d: dict):
    m = d.get("mode")
    if not m:
        return get_start_kb()

    m_l = "🌍 Охоплення: Всі міста" if m == "all" else "📍 Маршрут: Шлях"
    e_l = "🚫 Вимкнути фільтр 30хв" if d.get("extra") else "⚡ Свіжі ціни (30хв)"

    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🚀 Запустити сканер")],
            [KeyboardButton(text=m_l), KeyboardButton(text=e_l)],
            [KeyboardButton(text="🧮 Калькулятор"),
             KeyboardButton(text="💰 Налаштувати бюджет")],
            [KeyboardButton(text="🔄 Перезавантаження")]
        ],
        resize_keyboard=True
    )


def get_mode_inline():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🎲 Всі міста", callback_data="set_mode_all")],
            [InlineKeyboardButton(text="📍 Конкретний шлях", callback_data="set_mode_custom")]
        ]
    )


# ================= КЕШ items.json =================
def load_items_from_cache():
    if not os.path.exists(ITEMS_CACHE_FILE):
        return None
    try:
        mtime = datetime.fromtimestamp(os.path.getmtime(ITEMS_CACHE_FILE), tz=UTC)
        if datetime.now(UTC) - mtime > timedelta(hours=ITEMS_CACHE_TTL_HOURS):
            return None
        with open(ITEMS_CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return None


def save_items_to_cache(data):
    try:
        with open(ITEMS_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except:
        pass


async def download_items():
    global items_data, is_db_ready

    cached = load_items_from_cache()
    if cached:
        items_data = cached
        is_db_ready = True
        return

    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                "https://raw.githubusercontent.com/ao-data/ao-bin-dumps/master/formatted/items.json",
                timeout=60
            ) as r:
                if r.status == 200:
                    data = await r.json(content_type=None)
                    allowed = [
                        "weapon", "armor", "plate", "leather", "cloth",
                        "bag", "cape", "potion", "meal", "mount",
                        "tool", "shapeshifter", "offhand"
                    ]
                    items_data = {
                        i["UniqueName"]: i
                        for i in data
                        if i.get("UniqueName", "").startswith(("T4_", "T5_", "T6_", "T7_", "T8_"))
                        and any(x in i.get("UniqueName", "").lower() for x in allowed)
                    }
                    save_items_to_cache(items_data)
    except:
        pass
    finally:
        is_db_ready = True


# ================= ХЕЛПЕРИ =================
def fmt_t(s: str):
    if not s or s.startswith("0001"):
        return "???"
    try:
        dt = datetime.fromisoformat(s.split(".")[0].replace("Z", "")).replace(tzinfo=UTC)
        m = int((datetime.now(UTC) - dt).total_seconds() / 60)
        return f"{m}м" if m < 60 else f"{m // 60}г"
    except:
        return "???"


# ================= СКАНЕР =================
async def scan_logic(d: dict, f_c=None, t_c=None):
    res = []
    b_l = d.get("buy_limit", 0)
    p_l = d.get("profit_limit", 4000)
    ext = d.get("extra", False)

    if not items_data:
        return res

    i_list = list(items_data.keys())
    cities = [f_c, t_c] if f_c and t_c else CITIES

    async with aiohttp.ClientSession() as s:
        for i in range(0, len(i_list), 50):
            chunk = i_list[i:i + 50]
            url = (
                "https://europe.albion-online-data.com/api/v2/stats/prices/"
                f"{','.join(chunk)}?locations={','.join(cities)}"
            )

            try:
                async with s.get(url, timeout=20) as resp:
                    if resp.status != 200:
                        continue
                    data = await resp.json()
                if not data:
                    continue
            except:
                continue

            now = datetime.now(UTC)
            grouped = {}

            for e in data:
                try:
                    k = f"{e['item_id']}|{e['quality']}"
                    grouped.setdefault(k, {})[e["city"]] = e
                except:
                    continue

            for k, c_d in grouped.items():
                i_id, q = k.split("|")
                srcs = [f_c] if f_c else [c for c in c_d if c != "Black Market"]

                for sc in srcs:
                    if sc not in c_d:
                        continue

                    buy = c_d[sc].get("sell_price_min", 0)
                    if buy <= 500 or buy > b_l:
                        continue

                    try:
                        b_dt = datetime.fromisoformat(
                            c_d[sc]["sell_price_min_date"].split(".")[0].replace("Z", "")
                        ).replace(tzinfo=UTC)
                    except:
                        continue

                    if (now - b_dt).total_seconds() / 60 > 180:
                        continue

                    targets = [t_c] if t_c else [c for c in c_d if c != sc]

                    for tc in targets:
                        if tc not in c_d:
                            continue

                        sk = "buy_price_max_date" if tc == "Black Market" else "sell_price_min_date"
                        price_key = "buy_price_max" if tc == "Black Market" else "sell_price_min"

                        sell = c_d[tc].get(price_key, 0)
                        if sell <= buy or (sell / buy) > 10:
                            continue

                        try:
                            s_dt = datetime.fromisoformat(
                                c_d[tc][sk].split(".")[0].replace("Z", "")
                            ).replace(tzinfo=UTC)
                        except:
                            continue

                        if (now - s_dt).total_seconds() / 60 > 180:
                            continue

                        p_n = int(sell * 0.895 - buy)
                        if p_n < p_l:
                            continue

                        if ext and (
                            (now - b_dt).total_seconds() / 60 > 30 or
                            (now - s_dt).total_seconds() / 60 > 30
                        ):
                            continue

                        res.append({
                            "id": i_id,
                            "q": int(q),
                            "from": sc,
                            "to": tc,
                            "buy": buy,
                            "sell": sell,
                            "p_p": int(sell * 0.935 - buy),
                            "p_n": p_n,
                            "bd": c_d[sc]["sell_price_min_date"],
                            "sd": c_d[tc][sk],
                        })

            if i % 300 == 0:
                await asyncio.sleep(0.2)

    return res
# ================= ВИВІД РЕЗУЛЬТАТІВ (ОДНЕ ПОВІДОМЛЕННЯ, НУМЕРАЦІЯ) =================
async def disp_res(msg, res: list[dict]):
    if not res:
        return

    res.sort(key=lambda x: x["p_n"], reverse=True)
    res = res[:15]

    lines = []
    for idx, r in enumerate(res, start=1):
        b_id = r["id"].split("@")[0]
        enc = r["id"].split("@")[1] if "@" in r["id"] else "0"
        name = items_data.get(b_id, {}).get("LocalizedNames", {}).get("RU-RU", b_id)

        for t in TRASH:
            name = name.replace(t, "")

        block = (
            f"{idx}) 📦 <b>{name.upper()}</b> <code>[{b_id.split('_')[0][1:]}.{enc}]</code>\n"
            f"   ✨ Якість: <b>{QUALITY_NAMES.get(r['q'], 'Обычное')}</b>\n"
            f"   ──────────────────\n"
            f"   📥 <b>КУПІВЛЯ:</b> {CITY_EMOJIS[r['from']]} {r['from']}\n"
            f"   💰 Ціна: <code>{r['buy']:,}</code>\n"
            f"   ⏳ Оновлено: <b>{fmt_t(r['bd'])}</b> тому\n\n"
            f"   📤 <b>ПРОДАЖ:</b> {CITY_EMOJIS[r['to']]} {r['to']}\n"
            f"   💰 Ціна: <code>{r['sell']:,}</code>\n"
            f"   ⏳ Оновлено: <b>{fmt_t(r['sd'])}</b> тому\n"
            f"   ──────────────────\n"
            f"   💵 <b>ЧИСТИЙ ПРИБУТОК:</b>\n"
            f"   👑 Преміум: <code>+{r['p_p']:,}</code>\n"
            f"   💀 Без преміуму: <code>+{r['p_n']:,}</code>\n"
            f"   ──────────────────"
        )
        lines.append(block)

    text = "\n\n".join(lines)
    await msg.answer(text, parse_mode=ParseMode.HTML)


# ================= ОБРОБНИКИ =================
@dp.message(F.text == "🚀 Запустити сканер", StateFilter("*"))
async def main_search(m, state: FSMContext):
    u_id, now = m.from_user.id, datetime.now()
    is_admin = u_id == ADMIN_ID
    d = await state.get_data()

    if not is_admin:
        if u_id in active_scans:
            return await m.answer("⚠️ Твій запит ще обробляється!")
        if u_id in user_cooldowns and (now - user_cooldowns[u_id]).total_seconds() < 25:
            return await m.answer(
                f"⏳ Зачекай {int(25 - (now - user_cooldowns[u_id]).total_seconds())} сек."
            )

    if not is_db_ready:
        return await m.answer("⏳ База вантажиться...")

    if d.get("buy_limit", 0) <= 0:
        return await m.answer("💰 Спочатку встанови бюджет!")

    if not d.get("mode"):
        return await m.answer("🗺️ Обери режим!", reply_markup=get_mode_inline())

    if is_admin:
        s_msg = await m.answer(
            "⚡ <b>Адмін-сканування...</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=ReplyKeyboardRemove(),
        )
        res = await scan_logic(d, d.get("f_c"), d.get("t_c"))
        await s_msg.delete()
        await disp_res(m, res)
        return await m.answer(f"✅ Угод: {len(res)}", reply_markup=get_main_kb(d))

    async with scan_semaphore:
        active_scans.add(u_id)
        user_cooldowns[u_id] = now

        s_msg = await m.answer("🔍 Сканую Європу...", reply_markup=ReplyKeyboardRemove())
        res = await scan_logic(d, d.get("f_c"), d.get("t_c"))
        await s_msg.delete()

        if not res:
            await m.answer("📭 Нічого не знайдено.")
        else:
            await disp_res(m, res)

        await m.answer(f"✅ Готово! Знайдено: {len(res)}", reply_markup=get_main_kb(d))
        active_scans.remove(u_id)


# ================= РЕЖИМИ =================
@dp.callback_query(F.data.startswith("set_mode_"), StateFilter("*"))
async def set_mode_cb(cb, state: FSMContext):
    await cb.answer()
    mode = cb.data.split("_")[2]
    await state.update_data(mode=mode)
    d = await state.get_data()
    await cb.message.delete()

    if mode == "all":
        return await cb.message.answer(
            "🌍 Режим: Всі міста встановлено!\n"
            "👉 Тепер натисни <b>\"🚀 Запустити сканер\"</b>.",
            reply_markup=get_main_kb(d),
            parse_mode=ParseMode.HTML,
        )

    await state.set_state(BotState.picking_from)
    await cb.message.answer(
        "📍 Звідки веземо товар?",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=f"{CITY_EMOJIS[c]} {c}", callback_data=f"city_{c}")]
                for c in CITIES if c != "Black Market"
            ]
        ),
    )


@dp.callback_query(StateFilter(BotState.picking_from), F.data.startswith("city_"))
async def from_cb(cb, state: FSMContext):
    await cb.answer()
    c = cb.data.split("_")[1]
    await state.update_data(f_c=c)
    await state.set_state(BotState.picking_to)
    await cb.message.delete()

    await cb.message.answer(
        f"✅ Звідки: {c}\n📍 Тепер обери пункт Б:",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=f"{CITY_EMOJIS[x]} {x}", callback_data=f"city_{x}")]
                for x in CITIES if x != c and x != "Black Market"
            ]
        ),
    )


@dp.callback_query(StateFilter(BotState.picking_to), F.data.startswith("city_"))
async def to_cb(cb, state: FSMContext):
    await cb.answer()
    t = cb.data.split("_")[1]
    await state.update_data(t_c=t, mode="custom")
    d = await state.get_data()
    await state.set_state(None)
    await cb.message.delete()

    await cb.message.answer(
        f"🚀 Маршрут <b>{d['f_c']} ➔ {t}</b> встановлено!\n"
        "👉 Тепер натисни <b>\"🚀 Запустити сканер\"</b>, щоб почати пошук.",
        reply_markup=get_main_kb(d),
        parse_mode=ParseMode.HTML,
    )


# ================= БЮДЖЕТ =================
@dp.message(F.text == "💰 Налаштувати бюджет", StateFilter("*"))
async def limit_menu(m, state: FSMContext):
    await state.set_state(None)
    d = await state.get_data()
    b = d.get("buy_limit", 0)
    p = d.get("profit_limit", 4000)

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"💰 Бюджет ({b:,})", callback_data="set_limit_buy")],
            [InlineKeyboardButton(text=f"📈 Профіт ({p:,})", callback_data="set_limit_profit")],
        ]
    )

    await m.answer("⚙️ Налаштування бюджету:", reply_markup=kb)


@dp.callback_query(F.data.startswith("set_limit_"), StateFilter("*"))
async def set_limit_cb(cb, state: FSMContext):
    await cb.answer()
    t = cb.data.split("_")[2]
    await cb.message.delete()

    await state.set_state(
        BotState.waiting_for_buy_limit if t == "buy" else BotState.waiting_for_profit_limit
    )

    await cb.message.answer("💰 Введи число (наприклад 50000):")


@dp.message(StateFilter(BotState.waiting_for_buy_limit, BotState.waiting_for_profit_limit))
async def h_limits(m, state: FSMContext):
    raw = m.text.replace(" ", "")
    if not raw.isdigit():
        return await m.answer("❌ Введи тільки число.")

    value = int(raw)
    cur = await state.get_state()

    if cur == BotState.waiting_for_buy_limit.state:
        await state.update_data(buy_limit=value)
        await m.answer(f"✅ Бюджет встановлено: {value:,}")
    else:
        await state.update_data(profit_limit=value)
        await m.answer(f"✅ Мінімальний профіт: {value:,}")

    await state.set_state(None)
    d = await state.get_data()
    await m.answer("Готово ✔", reply_markup=get_main_kb(d))


# ================= ФІЛЬТР 30 ХВ =================
@dp.message(F.text.contains("ціни (30хв)") | F.text.contains("Вимкнути фільтр"), StateFilter("*"))
async def toggle_extra(m, state: FSMContext):
    d = await state.get_data()
    new = not d.get("extra", False)
    await state.update_data(extra=new)
    d = await state.get_data()

    await m.answer(
        f"⚡ Фільтр 30хв: {'УВІМК' if new else 'ВИМК'}",
        reply_markup=get_main_kb(d),
    )


# ================= ПЕРЕЗАВАНТАЖЕННЯ =================
@dp.message(F.text == "🔄 Перезавантаження", StateFilter("*"))
async def btn_res(m, state: FSMContext):
    u = m.from_user.id

    if u == ADMIN_ID:
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="📥 Оновити БД", callback_data="adm_upd")],
                [
                    InlineKeyboardButton(text="✅ Скинути все", callback_data="conf_res"),
                    InlineKeyboardButton(text="❌ Ні", callback_data="cancel_res"),
                ],
            ]
        )
        return await m.answer("🛠 Адмін-панель:", reply_markup=kb)

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Скинути", callback_data="conf_res"),
                InlineKeyboardButton(text="❌ Ні", callback_data="cancel_res"),
            ]
        ]
    )
    await m.answer("⚠️ Скинути твої дані?", reply_markup=kb)


@dp.callback_query(F.data == "adm_upd")
async def adm_upd(cb):
    if cb.from_user.id == ADMIN_ID:
        asyncio.create_task(download_items())
        await cb.message.answer("🔄 Оновлюю базу...")
    await cb.answer()


@dp.callback_query(F.data == "conf_res")
async def conf_res(cb, state: FSMContext):
    await state.clear()
    await cb.answer()
    await cb.message.delete()
    await cb.message.answer("🔄 Все скинуто!", reply_markup=get_start_kb())


@dp.callback_query(F.data == "cancel_res")
async def cancel_res(cb):
    await cb.answer()
    await cb.message.delete()
# ================= КАЛЬКУЛЯТОР =================
@dp.message(F.text == "🧮 Калькулятор", StateFilter("*"))
async def calc_start(m, state: FSMContext):
    await state.set_state(BotState.calc_count)
    await m.answer("🔢 Введи кількість предметів:")


@dp.message(StateFilter(BotState.calc_count))
async def calc_count(m, state: FSMContext):
    if not m.text.isdigit():
        return await m.answer("❌ Введи тільки число.")

    await state.update_data(calc_count=int(m.text))
    await state.set_state(BotState.calc_buy)
    await m.answer("💰 Введи ціну покупки за 1 шт:")


@dp.message(StateFilter(BotState.calc_buy))
async def calc_buy(m, state: FSMContext):
    if not m.text.isdigit():
        return await m.answer("❌ Введи тільки число.")

    await state.update_data(calc_buy=int(m.text))
    await state.set_state(BotState.calc_sell)
    await m.answer("💸 Введи ціну продажу за 1 шт:")


@dp.message(StateFilter(BotState.calc_sell))
async def calc_sell(m, state: FSMContext):
    if not m.text.isdigit():
        return await m.answer("❌ Введи тільки число.")

    d = await state.get_data()
    count = d["calc_count"]
    buy = d["calc_buy"]
    sell = int(m.text)

    profit_no_premium = int((sell * 0.895 - buy) * count)
    profit_premium = int((sell * 0.935 - buy) * count)

    await state.set_state(None)

    await m.answer(
        f"🧮 <b>Розрахунок:</b>\n"
        f"📦 Кількість: <b>{count}</b>\n"
        f"💰 Купівля: <b>{buy:,}</b>\n"
        f"💸 Продаж: <b>{sell:,}</b>\n"
        f"──────────────────\n"
        f"👑 Преміум: <code>+{profit_premium:,}</code>\n"
        f"💀 Без преміуму: <code>+{profit_no_premium:,}</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=get_main_kb(d),
    )


# ================= ДОПОМОГА =================
@dp.message(F.text == "❓ Допомога", StateFilter("*"))
async def help_msg(m, state: FSMContext):
    await state.set_state(None)
    d = await state.get_data()
    await m.answer(
        "ℹ️ <b>Як користуватись ботом:</b>\n\n"
        "1) Натисни «💰 Налаштувати бюджет» і введи свій максимум.\n"
        "2) Обери режим: всі міста або маршрут.\n"
        "3) Натисни «🚀 Запустити сканер».\n\n"
        "Бот покаже найвигідніші угоди з урахуванням податків.",
        parse_mode=ParseMode.HTML,
        reply_markup=get_main_kb(d),
    )


# ================= КНОПКИ РЕЖИМУ =================
@dp.message(F.text.contains("Охоплення") | F.text.contains("Маршрут"), StateFilter("*"))
async def modes_btn(m):
    await m.answer("🗺 Режим сканування:", reply_markup=get_mode_inline())


# ================= СТАРТ =================
@dp.message(Command("start"), StateFilter("*"))
async def cmd_start(m, state: FSMContext):
    await state.clear()
    await m.answer("👋 Бот готовий!", reply_markup=get_start_kb())


# ================= MAIN =================
async def main():
    await bot.delete_webhook(drop_pending_updates=True)
    asyncio.create_task(download_items())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())