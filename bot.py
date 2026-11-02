"""
Telegram-бот менеджер студии звукозаписи.
Запись по часовым слотам, просмотр занятости, "кто сейчас в студии".
Стек: Python 3.10+, aiogram 3.x, SQLite.
"""
import asyncio
import hashlib
import hmac
import html
import json
import logging
import os
import sqlite3
from pathlib import Path
from urllib.parse import parse_qsl
from contextlib import contextmanager
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError, TelegramUnauthorizedError
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, MenuButtonWebApp, Message, WebAppInfo
from aiogram.utils.keyboard import InlineKeyboardBuilder

# ======================= НАСТРОЙКИ =======================
BOT_TOKEN = os.getenv("BOT_TOKEN", "8985130432:AAExS-GZmL3JQDbvFQIUcplPg4FlGe9T5mg")
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "7518460854").split(",") if x.strip()}
STUDIO_NAME = os.getenv("STUDIO_NAME", "Студия звукозаписи")
TZ = ZoneInfo(os.getenv("TZ_NAME", "Asia/Yekaterinburg"))   # часовой пояс студии (Екатеринбург, UTC+5)
# Прокси для доступа к Telegram, формат: http://логин:пароль@host:port (или socks5://...)
PROXY_URL = os.getenv("PROXY_URL") or os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY") or ""
# Публичный HTTPS-адрес Mini App (обязательно https). Если пусто — работает только обычное меню.
WEBAPP_URL = os.getenv("WEBAPP_URL", "").rstrip("/")
WEB_PORT = int(os.getenv("WEB_PORT", "8080"))
OPEN_HOUR = int(os.getenv("OPEN_HOUR", "10"))          # открытие (включительно)
CLOSE_HOUR = int(os.getenv("CLOSE_HOUR", "22"))        # закрытие (последний слот заканчивается в это время)
DAYS_AHEAD = int(os.getenv("DAYS_AHEAD", "14"))        # на сколько дней вперёд можно записаться
MAX_ACTIVE = int(os.getenv("MAX_ACTIVE", "5"))         # максимум активных записей на человека
SHOW_NAMES = os.getenv("SHOW_NAMES", "1") == "1"       # показывать имена других клиентов
DB_PATH = os.getenv("DB_PATH", "studio.db")
# =========================================================

WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
HOURS = list(range(OPEN_HOUR, CLOSE_HOUR))
router = Router()


# ----------------------- База данных -----------------------
@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with db() as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS bookings(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                display TEXT NOT NULL,
                day TEXT NOT NULL,
                hour INTEGER NOT NULL,
                kind TEXT NOT NULL DEFAULT 'booking',   -- booking | block
                created_at TEXT NOT NULL,
                UNIQUE(day, hour)
            )"""
        )


# ----------------------- Хелперы -----------------------
def now() -> datetime:
    return datetime.now(TZ)


def slot_label(h: int) -> str:
    return f"{h:02d}:00–{h + 1:02d}:00"


def fmt_day(d: date) -> str:
    return f"{WEEKDAYS[d.weekday()]} {d:%d.%m.%Y}"


def user_display(u) -> str:
    return f"@{u.username}" if u.username else (u.full_name or str(u.id))


def esc(s: str) -> str:
    return html.escape(s)


def is_past(d: date, h: int) -> bool:
    n = now()
    return d < n.date() or (d == n.date() and h < n.hour)


def day_rows(d: date) -> dict:
    with db() as c:
        rows = c.execute("SELECT * FROM bookings WHERE day=?", (d.isoformat(),)).fetchall()
    return {r["hour"]: r for r in rows}


def taken_map(start: date, end: date) -> dict:
    with db() as c:
        rows = c.execute(
            "SELECT day, hour FROM bookings WHERE day BETWEEN ? AND ?",
            (start.isoformat(), end.isoformat()),
        ).fetchall()
    res: dict = {}
    for r in rows:
        res.setdefault(r["day"], set()).add(r["hour"])
    return res


def owner_label(row, viewer_id: int, admin: bool = False) -> str:
    if row["kind"] == "block":
        return "закрыто"
    if admin or row["user_id"] == viewer_id:
        return esc(row["display"]) + (" (вы)" if row["user_id"] == viewer_id else "")
    return esc(row["display"]) if SHOW_NAMES else "занято"


def day_text(d: date, viewer_id: int, admin: bool = False) -> str:
    rows = day_rows(d)
    lines = [f"📅 <b>{fmt_day(d)}</b>", ""]
    for h in HOURS:
        if h in rows:
            r = rows[h]
            extra = f"  <i>#{r['id']}</i>" if admin else ""
            lines.append(f"❌ {slot_label(h)} — {owner_label(r, viewer_id, admin)}{extra}")
        elif is_past(d, h):
            lines.append(f"⏳ {slot_label(h)} — прошло")
        else:
            lines.append(f"✅ {slot_label(h)} — свободно")
    return "\n".join(lines)


def active_count(user_id: int) -> int:
    n = now()
    with db() as c:
        return c.execute(
            """SELECT COUNT(*) FROM bookings WHERE user_id=? AND kind='booking'
               AND (day > ? OR (day = ? AND hour >= ?))""",
            (user_id, n.date().isoformat(), n.date().isoformat(), n.hour),
        ).fetchone()[0]


async def notify_admins(bot: Bot, text: str):
    for aid in ADMIN_IDS:
        try:
            await bot.send_message(aid, text)
        except Exception:
            logging.warning("Не удалось уведомить админа %s", aid)


async def show(cb: CallbackQuery, text: str, kb: InlineKeyboardMarkup):
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest:
        pass  # сообщение не изменилось
    await cb.answer()


# ----------------------- Клавиатуры -----------------------
def menu_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    if WEBAPP_URL:
        b.button(text="🗓 Открыть календарь", web_app=WebAppInfo(url=WEBAPP_URL))
    b.button(text="📅 Записаться", callback_data="m:book")
    b.button(text="🗓 Расписание / свободные дни", callback_data="m:view")
    b.button(text="🎙 Кто сейчас в студии", callback_data="m:now")
    b.button(text="📋 Мои записи", callback_data="m:my")
    b.adjust(1)
    return b.as_markup()


def days_kb(mode: str) -> InlineKeyboardMarkup:
    today = now().date()
    taken = taken_map(today, today + timedelta(days=DAYS_AHEAD - 1))
    b = InlineKeyboardBuilder()
    for i in range(DAYS_AHEAD):
        d = today + timedelta(days=i)
        t = taken.get(d.isoformat(), set())
        free = sum(1 for h in HOURS if h not in t and not is_past(d, h))
        icon = "🔴" if free == 0 else ("🟡" if free < len(HOURS) / 2 else "🟢")
        b.button(text=f"{icon} {fmt_day(d)[:-5]} · свободно {free}", callback_data=f"d:{d.isoformat()}:{mode}")
    b.button(text="⬅️ Меню", callback_data="m:menu")
    b.adjust(1)
    return b.as_markup()


def slots_kb(d: date) -> InlineKeyboardMarkup:
    rows = day_rows(d)
    b = InlineKeyboardBuilder()
    for h in HOURS:
        if h not in rows and not is_past(d, h):
            b.button(text=slot_label(h), callback_data=f"s:{d.isoformat()}:{h}")
    b.adjust(2)
    b2 = InlineKeyboardBuilder()
    b2.button(text="⬅️ К дням", callback_data="m:book")
    b.attach(b2)
    return b.as_markup()


def back_kb(target: str = "m:menu", text: str = "⬅️ Меню") -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text=text, callback_data=target)
    return b.as_markup()


# ----------------------- Хендлеры: меню -----------------------
@router.message(CommandStart())
async def start(m: Message):
    await m.answer(
        f"🎧 <b>{esc(STUDIO_NAME)}</b>\n\n"
        "Я менеджер студии: покажу свободное время, запишу вас на сессию "
        "и подскажу, кто сейчас в студии.",
        reply_markup=menu_kb(),
    )


@router.callback_query(F.data == "m:menu")
async def cb_menu(cb: CallbackQuery):
    await show(cb, f"🎧 <b>{esc(STUDIO_NAME)}</b>\nВыберите действие:", menu_kb())


@router.callback_query(F.data == "m:book")
async def cb_book(cb: CallbackQuery):
    await show(cb, "📅 Выберите день для записи:", days_kb("b"))


@router.callback_query(F.data == "m:view")
async def cb_view(cb: CallbackQuery):
    await show(cb, "🗓 Выберите день, чтобы посмотреть занятость:\n🟢 много места · 🟡 мало · 🔴 всё занято", days_kb("v"))


@router.callback_query(F.data.startswith("d:"))
async def cb_day(cb: CallbackQuery):
    _, day_s, mode = cb.data.split(":")
    d = date.fromisoformat(day_s)
    text = day_text(d, cb.from_user.id, cb.from_user.id in ADMIN_IDS)
    if mode == "b":
        text += "\n\nВыберите свободное время:"
        await show(cb, text, slots_kb(d))
    else:
        await show(cb, text, back_kb("m:view", "⬅️ К дням"))


# ----------------------- Запись -----------------------
@router.callback_query(F.data.startswith("s:"))
async def cb_slot(cb: CallbackQuery, bot: Bot):
    _, day_s, hour_s = cb.data.split(":")
    d, h = date.fromisoformat(day_s), int(hour_s)
    today = now().date()

    if h not in HOURS or d < today or d > today + timedelta(days=DAYS_AHEAD - 1) or is_past(d, h):
        return await cb.answer("Это время недоступно", show_alert=True)
    if active_count(cb.from_user.id) >= MAX_ACTIVE:
        return await cb.answer(f"Максимум активных записей: {MAX_ACTIVE}. Отмените одну из прошлых.", show_alert=True)

    name = user_display(cb.from_user)
    try:
        with db() as c:
            c.execute(
                "INSERT INTO bookings(user_id, display, day, hour, kind, created_at) VALUES(?,?,?,?, 'booking', ?)",
                (cb.from_user.id, name, d.isoformat(), h, now().isoformat()),
            )
    except sqlite3.IntegrityError:
        await cb.answer("Это время только что заняли 😕", show_alert=True)
        text = day_text(d, cb.from_user.id) + "\n\nВыберите другое время:"
        return await show(cb, text, slots_kb(d))

    await cb.answer("Запись создана ✅")
    text = day_text(d, cb.from_user.id) + f"\n\n✅ Вы записаны на <b>{fmt_day(d)}, {slot_label(h)}</b>.\nМожно выбрать ещё слот:"
    await show(cb, text, slots_kb(d))
    await notify_admins(bot, f"🆕 Новая запись: {esc(name)} — {fmt_day(d)}, {slot_label(h)}")


# ----------------------- Мои записи -----------------------
@router.callback_query(F.data == "m:my")
async def cb_my(cb: CallbackQuery):
    n = now()
    with db() as c:
        rows = c.execute(
            """SELECT * FROM bookings WHERE user_id=? AND kind='booking'
               AND (day > ? OR (day = ? AND hour >= ?)) ORDER BY day, hour""",
            (cb.from_user.id, n.date().isoformat(), n.date().isoformat(), n.hour),
        ).fetchall()
    b = InlineKeyboardBuilder()
    if not rows:
        text = "У вас нет активных записей."
    else:
        text = "📋 <b>Ваши записи</b> (нажмите, чтобы отменить):"
        for r in rows:
            d = date.fromisoformat(r["day"])
            b.button(text=f"❌ {fmt_day(d)[:-5]} {slot_label(r['hour'])}", callback_data=f"c:{r['id']}")
    b.button(text="⬅️ Меню", callback_data="m:menu")
    b.adjust(1)
    await show(cb, text, b.as_markup())


@router.callback_query(F.data.startswith("c:"))
async def cb_cancel(cb: CallbackQuery, bot: Bot):
    bid = int(cb.data.split(":")[1])
    with db() as c:
        row = c.execute("SELECT * FROM bookings WHERE id=? AND kind='booking'", (bid,)).fetchone()
        if not row or row["user_id"] != cb.from_user.id:
            return await cb.answer("Запись не найдена", show_alert=True)
        c.execute("DELETE FROM bookings WHERE id=?", (bid,))
    d = date.fromisoformat(row["day"])
    await cb.answer("Запись отменена")
    await notify_admins(bot, f"🚫 Отмена: {esc(row['display'])} — {fmt_day(d)}, {slot_label(row['hour'])}")
    await cb_my(cb)


# ----------------------- Кто сейчас в студии -----------------------
@router.callback_query(F.data == "m:now")
async def cb_now(cb: CallbackQuery):
    n = now()
    today, h = n.date().isoformat(), n.hour
    with db() as c:
        cur = c.execute("SELECT * FROM bookings WHERE day=? AND hour=?", (today, h)).fetchone()
        nxt = c.execute(
            """SELECT * FROM bookings WHERE kind='booking'
               AND (day > ? OR (day = ? AND hour > ?)) ORDER BY day, hour LIMIT 1""",
            (today, today, h),
        ).fetchone()

    if not (OPEN_HOUR <= h < CLOSE_HOUR):
        text = f"🌙 Студия сейчас закрыта (работаем {OPEN_HOUR:02d}:00–{CLOSE_HOUR:02d}:00)."
    elif cur is None:
        text = "🟢 <b>Студия свободна</b> — можно записаться прямо сейчас!"
    elif cur["kind"] == "block":
        text = "🔒 Студия закрыта администратором на это время."
    else:
        who = esc(cur["display"]) if SHOW_NAMES or cur["user_id"] == cb.from_user.id else "клиент"
        text = f"🎙 <b>Сейчас в студии:</b> {who}\n🕒 {slot_label(h)}"

    if nxt:
        nd = date.fromisoformat(nxt["day"])
        who = esc(nxt["display"]) if SHOW_NAMES else "клиент"
        text += f"\n\n⏭ Следующая сессия: {fmt_day(nd)}, {slot_label(nxt['hour'])} — {who}"
    b = InlineKeyboardBuilder()
    b.button(text="🔄 Обновить", callback_data="m:now")
    b.button(text="⬅️ Меню", callback_data="m:menu")
    b.adjust(1)
    await show(cb, text, b.as_markup())


# ----------------------- Админ-команды -----------------------
def admin_only(m: Message) -> bool:
    return m.from_user.id in ADMIN_IDS


@router.message(Command("admin"))
async def admin_help(m: Message):
    if not admin_only(m):
        return
    await m.answer(
        "<b>Команды администратора</b>\n"
        "/schedule [ГГГГ-ММ-ДД] — расписание дня с ID записей\n"
        "/cancel ID — отменить любую запись\n"
        "/block ГГГГ-ММ-ДД ЧАС — закрыть слот (напр. /block 2026-11-05 14)\n"
        "/blockday ГГГГ-ММ-ДД — закрыть весь день\n"
        "/unblock ГГГГ-ММ-ДД ЧАС — открыть слот\n"
        "/book ГГГГ-ММ-ДД ЧАС Имя — записать клиента вручную"
    )


@router.message(Command("schedule"))
async def admin_schedule(m: Message):
    if not admin_only(m):
        return
    parts = m.text.split()
    try:
        d = date.fromisoformat(parts[1]) if len(parts) > 1 else now().date()
    except ValueError:
        return await m.answer("Формат даты: ГГГГ-ММ-ДД")
    await m.answer(day_text(d, m.from_user.id, admin=True))


@router.message(Command("cancel"))
async def admin_cancel(m: Message, bot: Bot):
    if not admin_only(m):
        return
    try:
        bid = int(m.text.split()[1])
    except (IndexError, ValueError):
        return await m.answer("Использование: /cancel ID")
    with db() as c:
        row = c.execute("SELECT * FROM bookings WHERE id=?", (bid,)).fetchone()
        if not row:
            return await m.answer("Запись не найдена")
        c.execute("DELETE FROM bookings WHERE id=?", (bid,))
    await m.answer("Запись удалена ✅")
    if row["kind"] == "booking" and row["user_id"] > 0:
        try:
            d = date.fromisoformat(row["day"])
            await bot.send_message(row["user_id"], f"⚠️ Ваша запись на {fmt_day(d)}, {slot_label(row['hour'])} отменена администратором.")
        except Exception:
            pass


def _insert(day: str, hour: int, user_id: int, display: str, kind: str) -> bool:
    try:
        with db() as c:
            c.execute(
                "INSERT INTO bookings(user_id, display, day, hour, kind, created_at) VALUES(?,?,?,?,?,?)",
                (user_id, display, day, hour, kind, now().isoformat()),
            )
        return True
    except sqlite3.IntegrityError:
        return False


@router.message(Command("block"))
async def admin_block(m: Message):
    if not admin_only(m):
        return
    try:
        _, day_s, hour_s = m.text.split()[:3]
        d, h = date.fromisoformat(day_s), int(hour_s)
        assert h in HOURS
    except Exception:
        return await m.answer("Использование: /block ГГГГ-ММ-ДД ЧАС")
    ok = _insert(d.isoformat(), h, 0, "Закрыто", "block")
    await m.answer("Слот закрыт 🔒" if ok else "Слот уже занят — сначала отмените запись (/cancel ID)")


@router.message(Command("blockday"))
async def admin_blockday(m: Message):
    if not admin_only(m):
        return
    try:
        d = date.fromisoformat(m.text.split()[1])
    except Exception:
        return await m.answer("Использование: /blockday ГГГГ-ММ-ДД")
    closed = sum(_insert(d.isoformat(), h, 0, "Закрыто", "block") for h in HOURS)
    await m.answer(f"Закрыто слотов: {closed}. Уже занятые слоты не тронуты.")


@router.message(Command("unblock"))
async def admin_unblock(m: Message):
    if not admin_only(m):
        return
    try:
        _, day_s, hour_s = m.text.split()[:3]
        d, h = date.fromisoformat(day_s), int(hour_s)
    except Exception:
        return await m.answer("Использование: /unblock ГГГГ-ММ-ДД ЧАС")
    with db() as c:
        c.execute("DELETE FROM bookings WHERE day=? AND hour=? AND kind='block'", (d.isoformat(), h))
    await m.answer("Слот открыт ✅")


@router.message(Command("book"))
async def admin_book(m: Message):
    if not admin_only(m):
        return
    try:
        parts = m.text.split(maxsplit=3)
        d, h, name = date.fromisoformat(parts[1]), int(parts[2]), parts[3]
        assert h in HOURS
    except Exception:
        return await m.answer("Использование: /book ГГГГ-ММ-ДД ЧАС Имя клиента")
    ok = _insert(d.isoformat(), h, -1, name, "booking")
    await m.answer("Клиент записан ✅" if ok else "Слот уже занят")


# ----------------------- Mini App: API -----------------------
def check_init_data(init_data: str):
    """Проверяет подпись Telegram. Возвращает данные пользователя или None."""
    try:
        data = dict(parse_qsl(init_data, keep_blank_values=True))
        got = data.pop("hash")
        check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, got):
            return None
        if now().timestamp() - int(data["auth_date"]) > 86400:
            return None
        return json.loads(data["user"])
    except Exception:
        return None


def auth(request: web.Request) -> dict:
    user = check_init_data(request.headers.get("X-Init-Data", ""))
    if not user:
        raise web.HTTPUnauthorized(text=json.dumps({"error": "Откройте приложение через Telegram"}),
                                   content_type="application/json")
    return user


def err(text: str, status: int = 400):
    return web.json_response({"error": text}, status=status)


async def web_index(request: web.Request):
    return web.FileResponse(Path(__file__).with_name("index.html"))


async def api_now(request: web.Request):
    auth(request)
    n = now()
    if not (OPEN_HOUR <= n.hour < CLOSE_HOUR):
        return web.json_response({"state": "closed"})
    row = day_rows(n.date()).get(n.hour)
    if row is None:
        return web.json_response({"state": "free"})
    if row["kind"] == "block":
        return web.json_response({"state": "blocked"})
    name = row["display"] if SHOW_NAMES else None
    return web.json_response({"state": "busy", "name": name, "slot": slot_label(n.hour)})


async def api_month(request: web.Request):
    auth(request)
    y, mth = map(int, request.query["month"].split("-"))
    first = date(y, mth, 1)
    last = date(y + mth // 12, mth % 12 + 1, 1) - timedelta(days=1)
    taken = taken_map(first, last)
    today = now().date()
    horizon = today + timedelta(days=DAYS_AHEAD - 1)
    days = {}
    for i in range((last - first).days + 1):
        d = first + timedelta(days=i)
        t = taken.get(d.isoformat(), set())
        free = sum(1 for h in HOURS if h not in t and not is_past(d, h))
        days[d.isoformat()] = {"free": free, "open": today <= d <= horizon}
    return web.json_response({"days": days, "total": len(HOURS)})


async def api_day(request: web.Request):
    u = auth(request)
    d = date.fromisoformat(request.query["date"])
    rows = day_rows(d)
    slots = []
    for h in HOURS:
        r = rows.get(h)
        if r is None:
            slots.append({"hour": h, "status": "past" if is_past(d, h) else "free"})
        elif r["kind"] == "block":
            slots.append({"hour": h, "status": "blocked"})
        else:
            mine = r["user_id"] == u["id"]
            slots.append({
                "hour": h,
                "status": "mine" if mine else "busy",
                "id": r["id"] if mine else None,
                "name": r["display"] if (mine or SHOW_NAMES) else None,
            })
    return web.json_response({"slots": slots})


async def api_book(request: web.Request):
    u = auth(request)
    body = await request.json()
    d, h = date.fromisoformat(body["date"]), int(body["hour"])
    today = now().date()
    if h not in HOURS or d < today or d > today + timedelta(days=DAYS_AHEAD - 1) or is_past(d, h):
        return err("Это время недоступно")
    if active_count(u["id"]) >= MAX_ACTIVE:
        return err(f"Максимум активных записей: {MAX_ACTIVE}. Отмените одну из текущих.")
    name = f"@{u['username']}" if u.get("username") else (u.get("first_name") or str(u["id"]))
    if not _insert(d.isoformat(), h, u["id"], name, "booking"):
        return err("Это время только что заняли", 409)
    await notify_admins(request.app["bot"], f"🆕 Новая запись: {esc(name)} — {fmt_day(d)}, {slot_label(h)}")
    try:
        await request.app["bot"].send_message(u["id"], f"✅ Вы записаны: {fmt_day(d)}, {slot_label(h)}")
    except Exception:
        pass
    return web.json_response({"ok": True})


async def api_cancel(request: web.Request):
    u = auth(request)
    bid = int((await request.json())["id"])
    with db() as c:
        row = c.execute("SELECT * FROM bookings WHERE id=? AND kind='booking'", (bid,)).fetchone()
        if not row or row["user_id"] != u["id"]:
            return err("Запись не найдена", 404)
        c.execute("DELETE FROM bookings WHERE id=?", (bid,))
    d = date.fromisoformat(row["day"])
    await notify_admins(request.app["bot"], f"🚫 Отмена: {esc(row['display'])} — {fmt_day(d)}, {slot_label(row['hour'])}")
    return web.json_response({"ok": True})


async def start_web(bot: Bot) -> web.AppRunner:
    app = web.Application()
    app["bot"] = bot
    app.add_routes([
        web.get("/", web_index),
        web.get("/api/now", api_now),
        web.get("/api/month", api_month),
        web.get("/api/day", api_day),
        web.post("/api/book", api_book),
        web.post("/api/cancel", api_cancel),
    ])
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", WEB_PORT).start()
    return runner


# ----------------------- Запуск -----------------------
async def main():
    logging.basicConfig(level=logging.INFO)
    if not BOT_TOKEN:
        raise SystemExit("Укажите BOT_TOKEN в переменных окружения")
    init_db()

    print("Прокси:", PROXY_URL.split("@")[-1] if PROXY_URL else "НЕ ЗАДАН (бот пойдёт напрямую)")
    session = AiohttpSession(proxy=PROXY_URL) if PROXY_URL else None
    bot = Bot(BOT_TOKEN, session=session, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher()
    dp.include_router(router)

    runner = None
    try:
        me = await bot.get_me()
        logging.info("Бот запущен: @%s | часовой пояс: %s | прокси: %s",
                     me.username, TZ.key, "да" if PROXY_URL else "нет")
        if WEBAPP_URL:
            runner = await start_web(bot)
            await bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(text="📅 Календарь", web_app=WebAppInfo(url=WEBAPP_URL)))
            logging.info("Mini App: %s (локальный порт %s)", WEBAPP_URL, WEB_PORT)
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    except TelegramUnauthorizedError:
        print("\n❌ Неверный токен бота. Проверьте BOT_TOKEN (возможно, он был перевыпущен).")
    except TelegramNetworkError as e:
        print(f"\n❌ Нет связи с Telegram: {e}\n"
              "Проверьте прокси (PROXY_URL): логин/пароль, адрес, порт и тип (http / socks5).")
    finally:
        if runner:
            await runner.cleanup()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
