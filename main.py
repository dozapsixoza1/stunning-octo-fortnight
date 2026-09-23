import asyncio
import hashlib
import hmac
import json
import logging
import os
import secrets
import string
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import aiosqlite
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, ChatMemberStatus
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message, LabeledPrice, PreCheckoutQuery, ChatMemberUpdated,
    WebAppInfo, ReplyKeyboardMarkup, KeyboardButton,
)

# ============================================================
# OTDEL — bot + Mini App in ONE Python file.
#
# REQUIRED ENV:
#   BOT_TOKEN=8650738832:AAEd6RIeS-lDFJH99t3KkjE_jymKiIS7aQE
#   WEBAPP_URL=https://bot-1790200911-5714-wave-dipsize.bothost.tech
#   MASTER_OWNER_ID=8302336447
#
# OPTIONAL:
#   WEB_HOST=0.0.0.0
#   WEB_PORT=8080
#   DB_PATH=otdel.db
#
# IMPORTANT:
# The Telegram bot token that was pasted into the previous code
# should be revoked in @BotFather and replaced with a new token.
# Never publish the token in HTML/JS or GitHub.
# ============================================================

BOT_TOKEN = "8650738832:AAEd6RIeS-lDFJH99t3KkjE_jymKiIS7aQE"
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://bot-1790200911-5714-wave-dipsize.bothost.tech").rstrip("/")
MASTER_OWNER_ID = 76222784
DEVELOPER_ID = 8302336447
WEB_HOST = "0.0.0.0"
WEB_PORT = int(os.getenv("PORT", os.getenv("WEB_PORT", "8080")))
DB_PATH = "otdel.db"
STATIC_DIR = Path(__file__).parent

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")
if not MASTER_OWNER_ID:
    raise RuntimeError("MASTER_OWNER_ID is not set")

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("otdel")

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

SHOP = {
    "prefix": ("Префикс в чате", 50),
    "title": ("Название чата", 80),
    "desc": ("Описание чата", 60),
    "photo": ("Фото чата", 70),
    "tag": ("Тэг в чате", 100),
    "freezebuy": ("Заморозка чата", 150),
    "banbuy": ("Бан пользователя", 120),
    "mutebuy": ("Мут пользователя", 90),
}

RENT_DAYS = {
    "30": 30,
    "90": 90,
    "365": 365,
}


# ------------------------- DB -------------------------

async def db():
    return aiosqlite.connect(DB_PATH)


async def init_db():
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.executescript("""
        CREATE TABLE IF NOT EXISTS users(
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            photo_url TEXT,
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS chats(
            chat_id INTEGER PRIMARY KEY,
            title TEXT NOT NULL,
            owner_id INTEGER NOT NULL,
            added_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS rentals(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE NOT NULL,
            owner_id INTEGER,
            days INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            activated_at TEXT,
            expires_at TEXT,
            active INTEGER NOT NULL DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS members(
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            username TEXT,
            first_name TEXT,
            last_seen TEXT NOT NULL,
            PRIMARY KEY(chat_id, user_id)
        );

        CREATE TABLE IF NOT EXISTS warnings(
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(chat_id, user_id)
        );

        CREATE TABLE IF NOT EXISTS purchases(
            payload TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            chat_id INTEGER NOT NULL,
            item_id TEXT NOT NULL,
            price INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            paid INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS settings(
            chat_id INTEGER PRIMARY KEY,
            prefix TEXT DEFAULT '',
            tag TEXT DEFAULT ''
        );
        """)
        await conn.commit()


async def upsert_user(user: dict):
    uid = int(user["id"])
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            """INSERT INTO users(user_id,username,first_name,last_name,photo_url,first_seen,last_seen)
               VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(user_id) DO UPDATE SET
               username=excluded.username, first_name=excluded.first_name,
               last_name=excluded.last_name, photo_url=excluded.photo_url,
               last_seen=excluded.last_seen""",
            (uid, user.get("username"), user.get("first_name"),
             user.get("last_name"), user.get("photo_url"), now, now),
        )
        await conn.commit()


async def has_access(user_id: int) -> bool:
    # Главный владелец и разработчик имеют служебный доступ без аренды.
    if user_id in (MASTER_OWNER_ID, DEVELOPER_ID):
        return True
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            """SELECT 1 FROM rentals
               WHERE owner_id=? AND active=1 AND expires_at IS NOT NULL AND expires_at > ?
               LIMIT 1""",
            (user_id, now),
        )
        return await cur.fetchone() is not None


async def get_rental(user_id: int):
    if user_id == MASTER_OWNER_ID:
        return {"master": True, "developer": False, "days_left": None, "expires_at": None}
    if user_id == DEVELOPER_ID:
        return {"master": False, "developer": True, "days_left": None, "expires_at": None}
    now = datetime.now(timezone.utc)
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            """SELECT expires_at, days FROM rentals
               WHERE owner_id=? AND active=1
               ORDER BY expires_at DESC LIMIT 1""",
            (user_id,),
        )
        row = await cur.fetchone()
    if not row or not row[0]:
        return None
    exp = datetime.fromisoformat(row[0])
    seconds = max(0, int((exp - now).total_seconds()))
    return {
        "master": False,
        "days_left": seconds // 86400 + (1 if seconds % 86400 else 0),
        "expires_at": row[0],
        "days": row[1],
    }


async def save_chat(chat_id: int, title: str, owner_id: int):
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            """INSERT INTO chats(chat_id,title,owner_id,added_at)
               VALUES(?,?,?,?)
               ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title, owner_id=excluded.owner_id""",
            (chat_id, title, owner_id, datetime.now(timezone.utc).isoformat()),
        )
        await conn.commit()


async def remove_chat(chat_id: int):
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute("DELETE FROM chats WHERE chat_id=?", (chat_id,))
        await conn.commit()


async def get_owner_chats(user_id: int):
    async with aiosqlite.connect(DB_PATH) as conn:
        if user_id in (MASTER_OWNER_ID, DEVELOPER_ID):
            cur = await conn.execute("SELECT chat_id,title,owner_id FROM chats ORDER BY title")
        else:
            cur = await conn.execute("SELECT chat_id,title,owner_id FROM chats WHERE owner_id=? ORDER BY title", (user_id,))
        rows = await cur.fetchall()
    return [{"id": str(r[0]), "name": r[1], "role": ("Сервис" if user_id in (MASTER_OWNER_ID, DEVELOPER_ID) else "Владелец")} for r in rows]


async def owns_chat(user_id: int, chat_id: int) -> bool:
    if user_id in (MASTER_OWNER_ID, DEVELOPER_ID):
        return True
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            "SELECT 1 FROM chats WHERE chat_id=? AND owner_id=?",
            (chat_id, user_id),
        )
        return await cur.fetchone() is not None


async def remember_member(message: Message):
    if not message.from_user or not message.chat:
        return
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            """INSERT INTO members(chat_id,user_id,username,first_name,last_seen)
               VALUES(?,?,?,?,?)
               ON CONFLICT(chat_id,user_id) DO UPDATE SET
               username=excluded.username, first_name=excluded.first_name,
               last_seen=excluded.last_seen""",
            (
                message.chat.id,
                message.from_user.id,
                message.from_user.username,
                message.from_user.first_name,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        await conn.commit()


async def resolve_target(chat_id: int, raw: str) -> Optional[int]:
    raw = (raw or "").strip()
    if raw.isdigit() or (raw.startswith("-") and raw[1:].isdigit()):
        return int(raw)
    username = raw.lstrip("@").lower()
    if not username:
        return None
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            "SELECT user_id FROM members WHERE chat_id=? AND lower(username)=? LIMIT 1",
            (chat_id, username),
        )
        row = await cur.fetchone()
    return int(row[0]) if row else None


# ------------------------- Telegram WebApp auth -------------------------

def validate_init_data(init_data: str) -> dict:
    if not init_data:
        raise ValueError("Нет Telegram initData")

    from urllib.parse import parse_qsl
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = pairs.pop("hash", None)
    if not received_hash:
        raise ValueError("Нет hash")

    data_check_string = "\n".join(
        f"{k}={pairs[k]}" for k in sorted(pairs)
    )
    secret = hmac.new(
        b"WebAppData",
        BOT_TOKEN.encode(),
        hashlib.sha256,
    ).digest()
    calculated = hmac.new(
        secret,
        data_check_string.encode(),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(calculated, received_hash):
        raise ValueError("Неверная подпись Telegram")

    auth_date = int(pairs.get("auth_date", "0") or 0)
    if not auth_date or time.time() - auth_date > 86400:
        raise ValueError("initData устарел")

    user = json.loads(pairs.get("user", "{}"))
    if not user.get("id"):
        raise ValueError("Пользователь Telegram не найден")
    return {"user": user, "raw": pairs}


async def web_user(request: web.Request):
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    try:
        auth = validate_init_data(init_data)
        await upsert_user(auth["user"])
        return auth
    except Exception as e:
        raise web.HTTPUnauthorized(text=str(e))


async def require_access(request: web.Request):
    auth = await web_user(request)
    user_id = int(auth["user"]["id"])
    if not await has_access(user_id):
        raise web.HTTPForbidden(
            text=json.dumps({
                "ok": False,
                "error": "RENT_REQUIRED",
                "message": "Доступ к OTDEL доступен только по активной аренде."
            }, ensure_ascii=False),
            content_type="application/json",
        )
    return auth


# ------------------------- Bot keyboards -------------------------

def main_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[
            KeyboardButton(
                text="🎛 Открыть OTDEL",
                web_app=WebAppInfo(url=WEBAPP_URL),
            )
        ]],
        resize_keyboard=True,
    )


def rental_kb():
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="➕ 30 дней", callback_data="rent:30"),
            InlineKeyboardButton(text="➕ 90 дней", callback_data="rent:90"),
        ],
        [InlineKeyboardButton(text="➕ 365 дней", callback_data="rent:365")],
        [InlineKeyboardButton(text="📋 Активные аренды", callback_data="rent:list")],
    ])


def code_kb(code: str):
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Скопировать код", copy_text=None)]
    ])


# ------------------------- Start / rental -------------------------

@dp.message(CommandStart())
async def on_start(message: Message):
    if message.from_user:
        await upsert_user({"id":message.from_user.id,"username":message.from_user.username,"first_name":message.from_user.first_name,"last_name":message.from_user.last_name,"photo_url":None})
    if await has_access(message.from_user.id):
        rental = await get_rental(message.from_user.id)
        suffix = (
            "Главный владелец"
            if rental and rental.get("master")
            else f"Активная аренда: {rental['days_left']} дн."
        )
        await message.answer(
            f"👑 <b>OTDEL</b>\n\n"
            f"Панель управления Telegram-чатами.\n"
            f"Статус: <b>{suffix}</b>\n\n"
            f"Открывай Mini App кнопкой ниже.",
            reply_markup=main_kb(),
        )
    else:
        await message.answer(
            "🔒 <b>OTDEL</b>\n\n"
            "Доступ к Mini App выдаётся только по активной аренде.\n\n"
            "Если у тебя уже есть код аренды — отправь:\n"
            "<code>/rent КОД</code>\n\n"
            "После активации снова нажми /start."
        )


@dp.message(Command("rent"))
async def activate_rent(message: Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        if message.from_user.id == MASTER_OWNER_ID:
            await message.answer(
                "👑 <b>Управление арендой</b>\n\n"
                "Создай код для клиента:",
                reply_markup=rental_kb(),
            )
        else:
            await message.answer("Использование: <code>/rent КОД</code>")
        return

    code = parts[1].strip().upper()
    if message.from_user.id == MASTER_OWNER_ID:
        await message.answer("Главному владельцу код аренды не нужен.")
        return

    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            "SELECT id,days,owner_id,expires_at,active FROM rentals WHERE code=?",
            (code,),
        )
        row = await cur.fetchone()
        if not row:
            await message.answer("❌ Код не найден.")
            return
        rid, days, owner_id, expires_at, active = row
        if not active:
            await message.answer("❌ Этот код уже отключён.")
            return
        if owner_id and owner_id != message.from_user.id:
            await message.answer("❌ Код уже активирован другим пользователем.")
            return

        now = datetime.now(timezone.utc)
        if expires_at and datetime.fromisoformat(expires_at) > now:
            new_exp = datetime.fromisoformat(expires_at) + timedelta(days=days)
        else:
            new_exp = now + timedelta(days=days)

        await conn.execute(
            """UPDATE rentals SET owner_id=?, activated_at=?, expires_at=?, active=1
               WHERE id=?""",
            (
                message.from_user.id,
                now.isoformat(),
                new_exp.isoformat(),
                rid,
            ),
        )
        await conn.commit()

    await message.answer(
        f"✅ Аренда активирована.\n\n"
        f"Срок: <b>{days} дней</b>\n"
        f"До: <b>{new_exp.strftime('%d.%m.%Y %H:%M UTC')}</b>\n\n"
        f"Теперь нажми /start и открой OTDEL."
    )


@dp.callback_query(F.data.startswith("rent:"))
async def rental_callbacks(call):
    if call.from_user.id != MASTER_OWNER_ID:
        await call.answer("Только главный владелец", show_alert=True)
        return

    action = call.data.split(":", 1)[1]
    if action == "list":
        async with aiosqlite.connect(DB_PATH) as conn:
            cur = await conn.execute(
                """SELECT code,owner_id,days,expires_at,active
                   FROM rentals ORDER BY id DESC LIMIT 20"""
            )
            rows = await cur.fetchall()

        if not rows:
            text = "Аренд пока нет."
        else:
            lines = ["📋 <b>Последние аренды</b>\n"]
            for code, owner, days, exp, active in rows:
                who = str(owner) if owner else "не активирована"
                status = "🟢" if active else "🔴"
                lines.append(
                    f"{status} <code>{code}</code> · {days} дн. · {who}\n"
                    f"   до: {exp or '—'}"
                )
            text = "\n".join(lines)
        await call.message.edit_text(text, reply_markup=rental_kb())
        await call.answer()
        return

    days = RENT_DAYS.get(action)
    if not days:
        await call.answer("Неизвестный срок", show_alert=True)
        return

    code = "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(10))
    now = datetime.now(timezone.utc).isoformat()

    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            """INSERT INTO rentals(code,days,created_at,active)
               VALUES(?,?,?,1)""",
            (code, days, now),
        )
        await conn.commit()

    await call.message.answer(
        f"🎟 <b>Код аренды создан</b>\n\n"
        f"Срок: <b>{days} дней</b>\n"
        f"Код: <code>{code}</code>\n\n"
        f"Клиент должен отправить боту:\n"
        f"<code>/rent {code}</code>"
    )
    await call.answer("Создано")


# ------------------------- Bot chat registration -------------------------

async def register_chat_from_chat(chat, actor_id: int):
    if chat.type not in ("group", "supergroup"):
        return False, "Это не группа/супергруппа."
    try:
        admins = await bot.get_chat_administrators(chat.id)
        creator = next((a for a in admins if a.status == ChatMemberStatus.CREATOR), None)
        owner_id = creator.user.id if creator else actor_id
    except Exception:
        owner_id = actor_id
    if actor_id not in (MASTER_OWNER_ID, DEVELOPER_ID) and not await has_access(owner_id):
        return False, "У владельца группы нет активной аренды OTDEL."
    me = await bot.get_me()
    try:
        member = await bot.get_chat_member(chat.id, me.id)
        if member.status != ChatMemberStatus.ADMINISTRATOR:
            return False, "Сделай OTDEL администратором группы."
    except Exception:
        return False, "Не удалось проверить права OTDEL в группе."
    await save_chat(chat.id, chat.title or str(chat.id), owner_id)
    return True, owner_id

@dp.message(Command("connect"), F.chat.type.in_({"group", "supergroup"}))
async def connect_chat(message: Message):
    actor = message.from_user.id if message.from_user else 0
    try:
        member = await bot.get_chat_member(message.chat.id, actor)
        if actor not in (MASTER_OWNER_ID, DEVELOPER_ID) and member.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR):
            await message.reply("❌ Только администратор может подключить чат.")
            return
    except Exception:
        await message.reply("❌ Не удалось проверить права администратора.")
        return
    ok, info = await register_chat_from_chat(message.chat, actor)
    if ok:
        await message.reply("✅ <b>Чат подключён к OTDEL.</b>\nОткрой /start у бота и зайди в Mini App.")
    else:
        await message.reply(f"❌ {info}")

@dp.my_chat_member()
async def on_bot_membership_change(event: ChatMemberUpdated):
    new_status = event.new_chat_member.status
    if new_status in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR):
        try:
            ok, info = await register_chat_from_chat(event.chat, event.from_user.id if event.from_user else 0)
            if ok:
                log.info("Chat registered: %s owner=%s", event.chat.id, info)
        except Exception:
            log.exception("Failed to register chat %s", event.chat.id)
    elif new_status in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED):
        await remove_chat(event.chat.id)


@dp.message(F.chat.type.in_({"group", "supergroup"}))
async def track_messages(message: Message):
    await remember_member(message)
    if message.from_user:
        await upsert_user({"id":message.from_user.id,"username":message.from_user.username,"first_name":message.from_user.first_name,"last_name":message.from_user.last_name,"photo_url":None})


# ------------------------- Stars -------------------------

async def send_stars_invoice(user_id: int, payload: str, title: str, stars: int):
    await bot.send_invoice(
        chat_id=user_id,
        title=title[:32],
        description=f"OTDEL: {title}"[:255],
        payload=payload,
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(label=title[:32], amount=stars)],
    )


@dp.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(query.id, ok=True)


@dp.message(F.successful_payment)
async def on_paid(message: Message):
    payload = message.successful_payment.invoice_payload
    stars = message.successful_payment.total_amount

    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            "SELECT chat_id,item_id FROM purchases WHERE payload=? AND user_id=?",
            (payload, message.from_user.id),
        )
        row = await cur.fetchone()
        if row:
            chat_id, item_id = row
            await conn.execute(
                "UPDATE purchases SET paid=1 WHERE payload=?",
                (payload,),
            )
            await conn.commit()
        else:
            chat_id, item_id = None, None

    if item_id in SHOP:
        name = SHOP[item_id][0]
        await message.answer(
            f"✅ Оплата прошла: ⭐ {stars}\n"
            f"Товар: <b>{name}</b>\n"
            f"Чат: <code>{chat_id}</code>\n\n"
            f"Заявка сохранена."
        )
    else:
        await message.answer(f"✅ Оплата прошла: ⭐ {stars}")


# ------------------------- Web API -------------------------

async def api_bootstrap(request):
    auth = await require_access(request)
    user = auth["user"]
    uid = int(user["id"])
    chats = await get_owner_chats(uid)
    rental = await get_rental(uid)
    return web.json_response({
        "ok": True,
        "user": {
            "id": uid,
            "first_name": user.get("first_name", ""),
            "last_name": user.get("last_name", ""),
            "username": user.get("username", ""),
            "photo_url": user.get("photo_url", ""),
            "is_master": uid == MASTER_OWNER_ID,
            "is_developer": uid == DEVELOPER_ID,
        },
        "chats": chats,
        "rental": rental,
        "shop": [
            {"id": k, "name": v[0], "price": v[1]}
            for k, v in SHOP.items()
        ],
    })


async def api_action(request):
    auth = await require_access(request)
    uid = int(auth["user"]["id"])

    try:
        data = await request.json()
    except Exception:
        raise web.HTTPBadRequest(text="JSON required")

    action = str(data.get("action", ""))
    try:
        chat_id = int(data.get("chat_id"))
    except Exception:
        raise web.HTTPBadRequest(text="chat_id required")

    if not await owns_chat(uid, chat_id):
        raise web.HTTPForbidden(text="Этот чат не принадлежит текущему владельцу.")

    # Events
    if action == "event_777":
        await bot.send_message(chat_id, "🎰 <b>ИВЕНТ 777</b>\nНажимайте на слот и ловите удачу!")
        for _ in range(3):
            await bot.send_dice(chat_id, emoji="🎰")
        return web.json_response({"ok": True, "message": "Ивент 777 запущен"})

    if action == "event_spam3":
        await bot.send_message(
            chat_id,
            "⚡ <b>ПЕРЕБИВ</b>\n"
            "Ивент запущен на 3 минуты. Пишите как можно активнее!"
        )
        return web.json_response({"ok": True, "message": "Перебив запущен"})

    if action == "event_guess":
        number = secrets.randbelow(100) + 1
        # Для простоты число живёт в сообщении; полноценный игровой state
        # можно расширить позже.
        await bot.send_message(
            chat_id,
            f"🔢 <b>УГАДАЙ ЧИСЛО</b>\n"
            f"Я загадал число от 1 до 100.\n"
            f"Ивент запущен!"
        )
        return web.json_response({"ok": True, "message": "Угадай цифры запущен"})

    # Moderation
    target_raw = str(data.get("target", "")).strip()
    if action in {"mute", "unmute", "ban", "unban", "warn", "unwarn"}:
        target = await resolve_target(chat_id, target_raw)
        if not target:
            return web.json_response(
                {"ok": False, "message": "Пользователь не найден. Используй числовой Telegram ID или @username участника, которого бот уже видел."},
                status=400,
            )

        if action == "ban":
            await bot.ban_chat_member(chat_id, target)
            msg = "Бан выполнен"
        elif action == "unban":
            await bot.unban_chat_member(chat_id, target, only_if_banned=True)
            msg = "Разбан выполнен"
        elif action == "mute":
            await bot.restrict_chat_member(
                chat_id, target,
                permissions=__import__("aiogram").types.ChatPermissions(
                    can_send_messages=False
                )
            )
            msg = "Мут выполнен"
        elif action == "unmute":
            await bot.restrict_chat_member(
                chat_id, target,
                permissions=__import__("aiogram").types.ChatPermissions(
                    can_send_messages=True,
                    can_send_audios=True,
                    can_send_documents=True,
                    can_send_photos=True,
                    can_send_videos=True,
                    can_send_video_notes=True,
                    can_send_voice_notes=True,
                    can_send_polls=True,
                    can_send_other_messages=True,
                    can_add_web_page_previews=True,
                )
            )
            msg = "Размут выполнен"
        elif action in {"warn", "unwarn"}:
            async with aiosqlite.connect(DB_PATH) as conn:
                cur = await conn.execute(
                    "SELECT count FROM warnings WHERE chat_id=? AND user_id=?",
                    (chat_id, target),
                )
                row = await cur.fetchone()
                count = int(row[0]) if row else 0
                count = max(0, count + (1 if action == "warn" else -1))
                await conn.execute(
                    """INSERT INTO warnings(chat_id,user_id,count) VALUES(?,?,?)
                       ON CONFLICT(chat_id,user_id) DO UPDATE SET count=excluded.count""",
                    (chat_id, target, count),
                )
                await conn.commit()
            msg = f"Варн {'выдан' if action == 'warn' else 'снят'} · всего: {count}"

        return web.json_response({"ok": True, "message": msg})

    if action == "freeze":
        from aiogram.types import ChatPermissions
        await bot.set_chat_permissions(
            chat_id,
            ChatPermissions(can_send_messages=False)
        )
        return web.json_response({"ok": True, "message": "Чат заморожен"})

    if action == "unfreeze":
        from aiogram.types import ChatPermissions
        await bot.set_chat_permissions(
            chat_id,
            ChatPermissions(
                can_send_messages=True,
                can_send_audios=True,
                can_send_documents=True,
                can_send_photos=True,
                can_send_videos=True,
                can_send_video_notes=True,
                can_send_voice_notes=True,
                can_send_polls=True,
                can_send_other_messages=True,
                can_add_web_page_previews=True,
            )
        )
        return web.json_response({"ok": True, "message": "Чат разморожен"})

    if action == "buy":
        item_id = str(data.get("item", ""))
        if item_id not in SHOP:
            return web.json_response({"ok": False, "message": "Товар не найден"}, status=400)

        name, price = SHOP[item_id]
        payload = f"otdel:{uid}:{chat_id}:{item_id}:{secrets.token_hex(6)}"

        async with aiosqlite.connect(DB_PATH) as conn:
            await conn.execute(
                """INSERT INTO purchases(payload,user_id,chat_id,item_id,price,created_at)
                   VALUES(?,?,?,?,?,?)""",
                (
                    payload, uid, chat_id, item_id, price,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            await conn.commit()

        await send_stars_invoice(uid, payload, name, price)
        return web.json_response({
            "ok": True,
            "message": "Счёт отправлен в личные сообщения бота."
        })

    return web.json_response({"ok": False, "message": f"Неизвестное действие: {action}"}, status=400)


# ------------------------- Static site -------------------------

async def index_handler(request):
    return web.FileResponse(STATIC_DIR / "index.html")


async def welcome_handler(request):
    path = STATIC_DIR / "welcome.png"
    if not path.exists():
        raise web.HTTPNotFound()
    return web.FileResponse(path)


async def health_handler(request):
    return web.json_response({"ok": True, "service": "OTDEL"})


@web.middleware
async def cors_middleware(request, handler):
    if request.method == "OPTIONS":
        resp = web.Response(status=204)
    else:
        resp = await handler(request)
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Telegram-Init-Data"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp


def create_app():
    app = web.Application(middlewares=[cors_middleware])
    app.router.add_get("/", index_handler)
    app.router.add_get("/welcome.png", welcome_handler)
    app.router.add_get("/health", health_handler)
    app.router.add_get("/api/bootstrap", api_bootstrap)
    app.router.add_post("/api/action", api_action)
    app.router.add_route("OPTIONS", "/api/{tail:.*}", lambda request: web.Response(status=204))
    return app


async def main():
    await init_db()
    await bot.delete_webhook(drop_pending_updates=True)

    app = create_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, WEB_HOST, WEB_PORT)
    await site.start()

    log.info("OTDEL web: %s:%s", WEB_HOST, WEB_PORT)
    log.info("Mini App URL: %s", WEBAPP_URL)
    log.info("Polling started")

    try:
        await dp.start_polling(bot)
    finally:
        await runner.cleanup()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
