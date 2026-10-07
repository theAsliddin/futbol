
import asyncio
import logging
import os
import sqlite3
from datetime import date, datetime, timedelta
from html import escape

from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (CallbackQuery, InlineKeyboardButton, KeyboardButton,
                           Message, ReplyKeyboardMarkup)
from aiogram.utils.keyboard import InlineKeyboardBuilder

# ======================= SOZLAMALAR =======================
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN environment o'zgaruvchisi berilmagan. "
        "Render'da Environment Variables bo'limiga BOT_TOKEN ni qo'shing."
    )

ADMIN_IDS = {
    int(x)
    for x in os.getenv("ADMIN_IDS", "1833071130").split(",")
    if x.strip() and x.strip().isdigit()
}
OPEN_HOUR, CLOSE_HOUR = 8, 24       # 08:00 dan 24:00 gacha
DAYS_AHEAD = 7                      # necha kun oldindan band qilish mumkin
PRICE = 150_000                     # 1 soat narxi (so'm)
FIELD_NAME = "Mini stadion"
DB_PATH = "bookings.db"
BLOCK_UID = 0                       # yopilgan vaqtlar uchun maxsus user_id
# ==========================================================

user_router = Router()
admin_router = Router()
# Admin router faqat adminlar uchun ishlaydi
admin_router.message.filter(F.from_user.id.in_(ADMIN_IDS))
admin_router.callback_query.filter(F.from_user.id.in_(ADMIN_IDS))


class Booking(StatesGroup):
    phone = State()


class AdminStates(StatesGroup):
    broadcast = State()


def money(n: int) -> str:
    return f"{n:,}".replace(",", " ")


# ======================= BAZA =======================
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS bookings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                name TEXT,
                phone TEXT,
                day TEXT NOT NULL,
                hour INTEGER NOT NULL,
                UNIQUE(day, hour)
            )""")
        c.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                name TEXT,
                joined TEXT
            )""")


def save_user(user):
    with db() as c:
        c.execute(
            "INSERT OR IGNORE INTO users(user_id,name,joined) VALUES(?,?,?)",
            (user.id, user.full_name, datetime.now().isoformat(timespec="seconds")),
        )


def all_user_ids():
    with db() as c:
        return [r["user_id"] for r in c.execute("SELECT user_id FROM users")]


def day_rows(day: str) -> dict:
    """{soat: qator} — shu kundagi barcha bronlar va yopilgan vaqtlar."""
    with db() as c:
        rows = c.execute("SELECT * FROM bookings WHERE day=?", (day,)).fetchall()
    return {r["hour"]: r for r in rows}


def add_booking(user_id, name, phone, day, hour) -> bool:
    try:
        with db() as c:
            c.execute(
                "INSERT INTO bookings(user_id,name,phone,day,hour) VALUES(?,?,?,?,?)",
                (user_id, name, phone, day, hour),
            )
        return True
    except sqlite3.IntegrityError:      # shu vaqt allaqachon band
        return False


def user_bookings(user_id):
    with db() as c:
        return c.execute(
            "SELECT * FROM bookings WHERE user_id=? AND day>=? ORDER BY day,hour",
            (user_id, date.today().isoformat()),
        ).fetchall()


def cancel_own(booking_id, user_id):
    with db() as c:
        row = c.execute("SELECT * FROM bookings WHERE id=? AND user_id=?",
                        (booking_id, user_id)).fetchone()
        if row:
            c.execute("DELETE FROM bookings WHERE id=?", (booking_id,))
        return row


def cancel_any(booking_id):
    with db() as c:
        row = c.execute("SELECT * FROM bookings WHERE id=?", (booking_id,)).fetchone()
        if row:
            c.execute("DELETE FROM bookings WHERE id=?", (booking_id,))
        return row


def get_stats() -> dict:
    today = date.today()
    week_end = (today + timedelta(days=6)).isoformat()
    month_prefix = today.strftime("%Y-%m") + "-%"
    real = f"user_id != {BLOCK_UID}"
    with db() as c:
        q = lambda sql, *a: c.execute(sql, a).fetchone()[0]
        top = c.execute(
            f"SELECT hour, COUNT(*) n FROM bookings WHERE {real} "
            "GROUP BY hour ORDER BY n DESC LIMIT 1").fetchone()
        return {
            "total": q(f"SELECT COUNT(*) FROM bookings WHERE {real}"),
            "today": q(f"SELECT COUNT(*) FROM bookings WHERE {real} AND day=?", today.isoformat()),
            "week": q(f"SELECT COUNT(*) FROM bookings WHERE {real} AND day BETWEEN ? AND ?",
                      today.isoformat(), week_end),
            "month": q(f"SELECT COUNT(*) FROM bookings WHERE {real} AND day LIKE ?", month_prefix),
            "blocked": q(f"SELECT COUNT(*) FROM bookings WHERE user_id={BLOCK_UID} AND day>=?",
                         today.isoformat()),
            "users": q("SELECT COUNT(*) FROM users"),
            "top": top,
        }


# ======================= KLAVIATURALAR =======================
def main_menu(user_id: int):
    rows = [[KeyboardButton(text="⚽ Band qilish")],
            [KeyboardButton(text="📋 Mening bronlarim")]]
    if user_id in ADMIN_IDS:
        rows.append([KeyboardButton(text="🛠 Admin panel")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def dates_kb(prefix: str, back_cb: str | None = None):
    kb = InlineKeyboardBuilder()
    for i in range(DAYS_AHEAD):
        d = date.today() + timedelta(days=i)
        label = d.strftime("%d.%m") + (" (bugun)" if i == 0 else "")
        kb.button(text=label, callback_data=f"{prefix}:{d.isoformat()}")
    kb.adjust(2)
    if back_cb:
        kb.row(InlineKeyboardButton(text="⬅️ Orqaga", callback_data=back_cb))
    return kb.as_markup()


def hours_kb(day: str, mode: str = "user"):
    """mode='user' -> bron qilish, mode='block' -> admin vaqtni yopish/ochish."""
    rows = day_rows(day)
    now = datetime.now()
    kb = InlineKeyboardBuilder()
    for h in range(OPEN_HOUR, CLOSE_HOUR):
        if day == date.today().isoformat() and h <= now.hour:
            continue                               # o'tib ketgan vaqtlar
        r = rows.get(h)
        if mode == "user":
            if r:
                kb.button(text=f"❌ {h:02d}:00", callback_data="busy")
            else:
                kb.button(text=f"✅ {h:02d}:00", callback_data=f"h:{day}:{h}")
        else:
            if r is None:
                kb.button(text=f"✅ {h:02d}:00", callback_data=f"abh:{day}:{h}")
            elif r["user_id"] == BLOCK_UID:
                kb.button(text=f"⛔ {h:02d}:00", callback_data=f"abh:{day}:{h}")
            else:
                kb.button(text=f"❌ {h:02d}:00", callback_data="abusy")
    kb.adjust(3)
    back = "back" if mode == "user" else "a:block"
    kb.row(InlineKeyboardButton(text="⬅️ Orqaga", callback_data=back))
    return kb.as_markup()


def phone_kb():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Raqamni yuborish", request_contact=True)]],
        resize_keyboard=True, one_time_keyboard=True,
    )


def admin_menu_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="📅 Bugungi bronlar", callback_data=f"ad:{date.today().isoformat()}")
    kb.button(text="📆 Kun bo'yicha", callback_data="a:days")
    kb.button(text="⛔ Vaqtni yopish/ochish", callback_data="a:block")
    kb.button(text="📊 Statistika", callback_data="a:stats")
    kb.button(text="📣 Xabar yuborish", callback_data="a:bc")
    kb.adjust(1, 2, 2)
    return kb.as_markup()


def to_admin_menu_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Admin menyu", callback_data="a:menu")
    return kb.as_markup()


async def safe_edit(msg: Message, text: str, kb=None, html=False):
    try:
        await msg.edit_text(text, reply_markup=kb, parse_mode="HTML" if html else None)
    except TelegramBadRequest:       # "message is not modified" va shu kabilar
        pass


async def notify_admins(bot: Bot, text: str):
    for aid in ADMIN_IDS:
        try:
            await bot.send_message(aid, text)
        except Exception:
            logging.warning("Adminga xabar yuborilmadi: %s", aid)


# ======================= MIJOZ HANDLERLARI =======================
@user_router.message(CommandStart())
async def start(m: Message, state: FSMContext):
    await state.clear()
    save_user(m.from_user)
    await m.answer(
        f"Assalomu alaykum, {escape(m.from_user.first_name)}! 👋\n"
        f"<b>{FIELD_NAME}</b> maydonini band qilish botiga xush kelibsiz.\n"
        f"Narxi: {money(PRICE)} so'm / soat",
        reply_markup=main_menu(m.from_user.id), parse_mode="HTML",
    )


@user_router.message(F.text == "⚽ Band qilish")
async def choose_date(m: Message, state: FSMContext):
    await state.clear()
    save_user(m.from_user)
    await m.answer("📅 Kunni tanlang:", reply_markup=dates_kb("d"))


@user_router.callback_query(F.data == "back")
async def back(c: CallbackQuery):
    await safe_edit(c.message, "📅 Kunni tanlang:", dates_kb("d"))
    await c.answer()


@user_router.callback_query(F.data.startswith("d:"))
async def choose_hour(c: CallbackQuery):
    day = c.data[2:]
    await safe_edit(c.message, f"🕒 {day} uchun vaqtni tanlang:\n✅ bo'sh   ❌ band",
                    hours_kb(day))
    await c.answer()


@user_router.callback_query(F.data == "busy")
async def busy(c: CallbackQuery):
    await c.answer("Bu vaqt band ❌", show_alert=True)


@user_router.callback_query(F.data.startswith("h:"))
async def ask_phone(c: CallbackQuery, state: FSMContext):
    _, day, hour = c.data.split(":")
    hour = int(hour)
    if hour in day_rows(day):
        await c.answer("Afsus, bu vaqt band bo'lib qoldi ❌", show_alert=True)
        await safe_edit(c.message, f"🕒 {day} uchun vaqtni tanlang:", hours_kb(day))
        return
    await state.set_state(Booking.phone)
    await state.update_data(day=day, hour=hour)
    await safe_edit(c.message, f"Tanlandi: <b>{day}, {hour:02d}:00</b>", html=True)
    await c.message.answer(
        "Telefon raqamingizni yuboring (tugma orqali yoki qo'lda yozing):",
        reply_markup=phone_kb(),
    )
    await c.answer()


@user_router.message(Booking.phone)
async def save_booking(m: Message, state: FSMContext):
    phone = m.contact.phone_number if m.contact else (m.text or "").strip()
    digits = "".join(ch for ch in phone if ch.isdigit())
    if len(digits) < 9:
        await m.answer("Raqam noto'g'ri. Qaytadan yuboring (masalan: +998901234567).")
        return

    data = await state.get_data()
    day, hour = data["day"], data["hour"]
    ok = add_booking(m.from_user.id, m.from_user.full_name, phone, day, hour)
    await state.clear()

    if not ok:
        await m.answer("Afsus, bu vaqt boshqa mijoz tomonidan band qilindi ❌",
                       reply_markup=main_menu(m.from_user.id))
        return

    await m.answer(
        f"✅ Band qilindi!\n\n📍 {FIELD_NAME}\n📅 {day}\n🕒 {hour:02d}:00 – {hour+1:02d}:00\n"
        f"💰 {money(PRICE)} so'm",
        reply_markup=main_menu(m.from_user.id),
    )
    await notify_admins(
        m.bot, f"🔔 Yangi bron\n👤 {m.from_user.full_name}\n📞 {phone}\n📅 {day} {hour:02d}:00")


@user_router.message(F.text == "📋 Mening bronlarim")
async def my_bookings(m: Message):
    rows = user_bookings(m.from_user.id)
    if not rows:
        await m.answer("Sizda faol bronlar yo'q.")
        return
    for r in rows:
        kb = InlineKeyboardBuilder()
        kb.button(text="🗑 Bekor qilish", callback_data=f"c:{r['id']}")
        await m.answer(f"📅 {r['day']}  🕒 {r['hour']:02d}:00", reply_markup=kb.as_markup())


@user_router.callback_query(F.data.startswith("c:"))
async def cancel(c: CallbackQuery):
    row = cancel_own(int(c.data[2:]), c.from_user.id)
    if row:
        await safe_edit(c.message, "🗑 Bron bekor qilindi.")
        await notify_admins(
            c.bot, f"⚠️ Bron bekor qilindi\n👤 {c.from_user.full_name}\n"
                   f"📅 {row['day']} {row['hour']:02d}:00")
    else:
        await c.answer("Bron topilmadi", show_alert=True)
        return
    await c.answer()


# ======================= ADMIN PANEL =======================
@admin_router.message(Command("admin"))
@admin_router.message(F.text == "🛠 Admin panel")
async def admin_panel(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("🛠 <b>Admin panel</b>", reply_markup=admin_menu_kb(), parse_mode="HTML")


@admin_router.callback_query(F.data == "a:menu")
async def admin_menu(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_edit(c.message, "🛠 <b>Admin panel</b>", admin_menu_kb(), html=True)
    await c.answer()


# ---- Kun bo'yicha bronlar ----
def day_view(day: str):
    rows = day_rows(day)
    free = (CLOSE_HOUR - OPEN_HOUR) - len(rows)
    lines = [f"📅 <b>{day}</b>   (bo'sh soatlar: {free})\n"]
    kb = InlineKeyboardBuilder()
    if not rows:
        lines.append("Hozircha bronlar yo'q.")
    for h in sorted(rows):
        r = rows[h]
        if r["user_id"] == BLOCK_UID:
            lines.append(f"{h:02d}:00 — ⛔ yopilgan")
        else:
            lines.append(f"{h:02d}:00 — {escape(r['name'] or '')}, {escape(r['phone'] or '')}")
        kb.button(text=f"🗑 {h:02d}:00", callback_data=f"ax:{r['id']}:{day}")
    kb.adjust(3)
    kb.row(InlineKeyboardButton(text="📆 Boshqa kun", callback_data="a:days"),
           InlineKeyboardButton(text="⬅️ Menyu", callback_data="a:menu"))
    return "\n".join(lines), kb.as_markup()


@admin_router.callback_query(F.data == "a:days")
async def admin_days(c: CallbackQuery):
    await safe_edit(c.message, "📆 Kunni tanlang:", dates_kb("ad", "a:menu"))
    await c.answer()


@admin_router.callback_query(F.data.startswith("ad:"))
async def admin_day(c: CallbackQuery):
    text, kb = day_view(c.data[3:])
    await safe_edit(c.message, text, kb, html=True)
    await c.answer()


@admin_router.callback_query(F.data.startswith("ax:"))
async def admin_cancel(c: CallbackQuery):
    _, bid, day = c.data.split(":")
    row = cancel_any(int(bid))
    if row and row["user_id"] != BLOCK_UID:
        try:
            await c.bot.send_message(
                row["user_id"],
                f"⚠️ Kechirasiz, {row['day']} {row['hour']:02d}:00 dagi broningiz "
                f"administrator tomonidan bekor qilindi.")
        except Exception:
            pass
    await c.answer("Bekor qilindi 🗑" if row else "Topilmadi")
    text, kb = day_view(day)
    await safe_edit(c.message, text, kb, html=True)


# ---- Vaqtni yopish / ochish ----
@admin_router.callback_query(F.data == "a:block")
async def admin_block_days(c: CallbackQuery):
    await safe_edit(c.message, "⛔ Qaysi kun uchun vaqtni yopmoqchisiz?",
                    dates_kb("abd", "a:menu"))
    await c.answer()


@admin_router.callback_query(F.data.startswith("abd:"))
async def admin_block_hours(c: CallbackQuery):
    day = c.data[4:]
    await safe_edit(c.message,
                    f"⛔ {day}\nBo'sh vaqtni bosing — yopiladi, ⛔ ni bosing — qayta ochiladi.\n"
                    f"✅ bo'sh  ⛔ yopiq  ❌ mijoz broni",
                    hours_kb(day, "block"))
    await c.answer()


@admin_router.callback_query(F.data == "abusy")
async def admin_busy(c: CallbackQuery):
    await c.answer("Bu vaqtda mijoz broni bor. Uni kun ro'yxatidan bekor qiling.",
                   show_alert=True)


@admin_router.callback_query(F.data.startswith("abh:"))
async def admin_toggle(c: CallbackQuery):
    _, day, hour = c.data.split(":")
    hour = int(hour)
    r = day_rows(day).get(hour)
    if r is None:
        add_booking(BLOCK_UID, "⛔ Yopilgan", "", day, hour)
        await c.answer("Vaqt yopildi ⛔")
    elif r["user_id"] == BLOCK_UID:
        cancel_any(r["id"])
        await c.answer("Vaqt ochildi ✅")
    else:
        await c.answer("Bu vaqtda mijoz broni bor", show_alert=True)
    try:
        await c.message.edit_reply_markup(reply_markup=hours_kb(day, "block"))
    except TelegramBadRequest:
        pass


# ---- Statistika ----
@admin_router.callback_query(F.data == "a:stats")
async def admin_stats(c: CallbackQuery):
    s = get_stats()
    top = f"{s['top']['hour']:02d}:00 ({s['top']['n']} marta)" if s["top"] else "—"
    text = (
        "📊 <b>Statistika</b>\n\n"
        f"Bugun: <b>{s['today']}</b> ta bron\n"
        f"Keyingi 7 kun: <b>{s['week']}</b> ta\n"
        f"Shu oy: <b>{s['month']}</b> ta  ≈ {money(s['month'] * PRICE)} so'm\n"
        f"Jami: <b>{s['total']}</b> ta  ≈ {money(s['total'] * PRICE)} so'm\n\n"
        f"👥 Foydalanuvchilar: <b>{s['users']}</b>\n"
        f"⛔ Yopilgan vaqtlar: <b>{s['blocked']}</b>\n"
        f"🔥 Eng ommabop soat: <b>{top}</b>"
    )
    await safe_edit(c.message, text, to_admin_menu_kb(), html=True)
    await c.answer()


# ---- Xabar yuborish (broadcast) ----
@admin_router.callback_query(F.data == "a:bc")
async def admin_bc_ask(c: CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.broadcast)
    await safe_edit(
        c.message,
        f"📣 Barcha foydalanuvchilarga ({len(all_user_ids())} ta) yuboriladigan "
        f"xabarni yozing (matn yoki rasm).\nBekor qilish uchun tugmani bosing.",
        to_admin_menu_kb())
    await c.answer()


@admin_router.message(AdminStates.broadcast)
async def admin_bc_send(m: Message, state: FSMContext):
    await state.clear()
    ok = fail = 0
    for uid in all_user_ids():
        try:
            await m.copy_to(uid)
            ok += 1
        except Exception:
            fail += 1
        await asyncio.sleep(0.05)       # Telegram limitlariga tushmaslik uchun
    await m.answer(f"✅ Yuborildi: {ok}\n❌ Xatolik: {fail}", reply_markup=admin_menu_kb())


# ======================= ISHGA TUSHIRISH =======================
async def main():
    logging.basicConfig(level=logging.INFO)
    if not ADMIN_IDS:
        logging.warning("ADMIN_IDS berilmagan — admin panel hech kimga ochiq emas!")
    init_db()
    bot = Bot(BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(admin_router)     # admin birinchi tekshiriladi
    dp.include_router(user_router)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
