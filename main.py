#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Бот для раздачи подарков и ивентов
Фреймворк: python-telegram-bot 20.x (PTB)
"""

import asyncio
import json
import logging
import os
import random
from collections import defaultdict
from datetime import datetime

from telegram import (
    Bot,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Update,
)
from telegram.constants import ChatType, ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    PreCheckoutQueryHandler,
    filters,
)

# ===================== НАСТРОЙКИ =====================
BOT_TOKEN      = "8888151886:AAG5HT_2w21knElyzBI0WakUfNOZTRho_OQ"
OWNER_ID       = 8675927241
GROUP_USERNAME = "chatlancet"
GROUP_CHAT_ID  = -1003773742747
BALANCE_FILE   = "balance.json"
# =====================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger(__name__)

# ─── Глобальное состояние ────────────────────────────
active_event:  dict | None = None
event_task:    asyncio.Task | None = None
msg_counts     = defaultdict(int)
usernames:     dict[int, str] = {}
pending_prize: str | None = None
pending_stars: int | None = None
waiting_custom_topup: bool = False
# ─────────────────────────────────────────────────────

STAR_AMOUNTS = [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]

EVENT_TYPES = [
    ("last_leader",   "👑 Последний лидер (3 мин без перебива)"),
    ("most_active",   "💬 Самый активный (5 мин)"),
    ("random_win",    "🎲 Случайный победитель"),
    ("first_sticker", "🎯 Первый стикер"),
    ("x2_stars",      "⭐ X2 звёзды (15 мин)"),
    ("quiz",          "🧠 Викторина"),
    ("lottery",       "🎟 Лотерея (5 мин)"),
    ("reaction_win",  "❤️ Больше активности"),
]

QUIZ_QUESTIONS = [
    ("Сколько будет 7 × 8?", "56"),
    ("Столица России?", "москва"),
    ("Сколько дней в году?", "365"),
    ("Сколько цветов у радуги?", "7"),
    ("Как называется наша планета?", "земля"),
    ("Сколько минут в часе?", "60"),
    ("В каком году основан Telegram?", "2013"),
    ("Сколько букв в русском алфавите?", "33"),
    ("Сколько секунд в минуте?", "60"),
    ("Первый президент России?", "ельцин"),
]


# ══════════════════════════════════════════════════════
#  Баланс
# ══════════════════════════════════════════════════════

def load_balance() -> dict:
    if os.path.exists(BALANCE_FILE):
        with open(BALANCE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    data = {"balance": 0, "transactions": []}
    save_balance(data)
    return data


def save_balance(data: dict):
    with open(BALANCE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_balance() -> int:
    return load_balance()["balance"]


def add_balance(amount: int, note: str = ""):
    data = load_balance()
    data["balance"] += amount
    data["transactions"].append({
        "type": "topup",
        "amount": amount,
        "note": note,
        "time": datetime.now().isoformat(),
    })
    save_balance(data)


def deduct_balance(amount: int, winner: str = ""):
    data = load_balance()
    data["balance"] -= amount
    data["transactions"].append({
        "type": "payout",
        "amount": amount,
        "winner": winner,
        "time": datetime.now().isoformat(),
    })
    save_balance(data)


# ══════════════════════════════════════════════════════
#  Утилиты
# ══════════════════════════════════════════════════════

def is_allowed_chat(chat_id: int, username: str | None) -> bool:
    if GROUP_CHAT_ID and chat_id == GROUP_CHAT_ID:
        return True
    if username and username.lower() == GROUP_USERNAME.lower():
        return True
    return False


def mention(user_id: int) -> str:
    uname = usernames.get(user_id)
    return f"@{uname}" if uname else f"[пользователь](tg://user?id={user_id})"


async def send_group(bot: Bot, text: str):
    target = GROUP_CHAT_ID or f"@{GROUP_USERNAME}"
    await bot.send_message(
        chat_id=target,
        text=text,
        parse_mode=ParseMode.MARKDOWN,
    )


def make_kb(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(text=t, callback_data=d) for t, d in row]
        for row in rows
    ])


# ══════════════════════════════════════════════════════
#  /start
# ══════════════════════════════════════════════════════

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if msg.chat.type != ChatType.PRIVATE:
        return

    if msg.from_user.id != OWNER_ID:
        await msg.reply_text(
            "👋 Привет!\n"
            "Общайся в нашем чате и получай возможность выиграть *Мишку* от @godlancet! 🐻\n\n"
            "Переходи: @chatlancet",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    bal = get_balance()
    await msg.reply_text(
        "👑 *Панель владельца*\n\n"
        f"💰 Баланс бота: *{bal} ⭐*\n\n"
        "Команды:\n"
        "/balance — текущий баланс\n"
        "/topup — пополнить баланс звёздами\n"
        "/event `<приз>` — запустить ивент\n"
        "/stop — остановить ивент\n"
        "/announce `<текст>` — анонс в группу\n"
        "/stats — статистика ивента\n"
        "/history — история транзакций\n\n"
        "Или просто пришли текст приза — спрошу тип ивента 🎯",
        parse_mode=ParseMode.MARKDOWN,
    )


# ══════════════════════════════════════════════════════
#  /balance
# ══════════════════════════════════════════════════════

async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.chat.type != ChatType.PRIVATE or update.message.from_user.id != OWNER_ID:
        return
    bal = get_balance()
    await update.message.reply_text(
        f"💰 *Баланс бота*\n\n"
        f"⭐ Доступно: *{bal} звёзд*\n\n"
        f"{'✅ Достаточно для выдачи призов.' if bal >= 10 else '❌ Пополни баланс — /topup'}",
        parse_mode=ParseMode.MARKDOWN,
    )


# ══════════════════════════════════════════════════════
#  /history
# ══════════════════════════════════════════════════════

async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.chat.type != ChatType.PRIVATE or update.message.from_user.id != OWNER_ID:
        return
    data = load_balance()
    txs = data.get("transactions", [])[-10:]
    if not txs:
        await update.message.reply_text("📋 История пуста.")
        return
    lines = []
    for t in reversed(txs):
        dt = t["time"][:16].replace("T", " ")
        if t["type"] == "topup":
            lines.append(f"➕ +{t['amount']}⭐ — {t.get('note', '')} [{dt}]")
        else:
            lines.append(f"➖ -{t['amount']}⭐ → {t.get('winner', '?')} [{dt}]")
    await update.message.reply_text(
        "📋 *Последние транзакции:*\n\n" + "\n".join(lines),
        parse_mode=ParseMode.MARKDOWN,
    )


# ══════════════════════════════════════════════════════
#  /topup
# ══════════════════════════════════════════════════════

async def cmd_topup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.chat.type != ChatType.PRIVATE or update.message.from_user.id != OWNER_ID:
        return

    rows = []
    row = []
    for i, amount in enumerate(STAR_AMOUNTS):
        row.append((f"⭐ {amount}", f"topup:{amount}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([("✏️ Своя сумма", "topup:custom")])

    bal = get_balance()
    await update.message.reply_text(
        f"💰 *Пополнение баланса*\n\n"
        f"Текущий баланс: *{bal} ⭐*\n\n"
        "Выбери сколько звёзд пополнить:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=make_kb(rows),
    )


# ══════════════════════════════════════════════════════
#  /event
# ══════════════════════════════════════════════════════

async def cmd_event(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global pending_prize
    msg = update.message
    if msg.chat.type != ChatType.PRIVATE or msg.from_user.id != OWNER_ID:
        return

    args = msg.text.split(maxsplit=1)
    if len(args) < 2:
        await msg.reply_text("Укажи приз: `/event Eternal Rose #6198`", parse_mode=ParseMode.MARKDOWN)
        return

    pending_prize = args[1]
    await ask_prize_stars(msg)


async def ask_prize_stars(msg):
    bal = get_balance()
    rows = []
    row = []
    for amount in STAR_AMOUNTS:
        emoji = "✅" if bal >= amount else "❌"
        row.append((f"{emoji} {amount}⭐", f"pstars:{amount}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([("🎁 Без звёздного приза", "pstars:0")])

    await msg.reply_text(
        f"⭐ *Сколько звёзд получит победитель?*\n\n"
        f"💰 Баланс бота: *{bal} ⭐*\n"
        f"✅ — хватает   ❌ — недостаточно",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=make_kb(rows),
    )


# ══════════════════════════════════════════════════════
#  /stop
# ══════════════════════════════════════════════════════

async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if msg.chat.type != ChatType.PRIVATE or msg.from_user.id != OWNER_ID:
        return
    if not active_event:
        await msg.reply_text("❌ Нет активного ивента.")
        return
    kb = make_kb([[("✅ Да, стоп", "stop_yes"), ("❌ Отмена", "stop_no")]])
    await msg.reply_text("Остановить текущий ивент?", reply_markup=kb)


# ══════════════════════════════════════════════════════
#  /announce
# ══════════════════════════════════════════════════════

async def cmd_announce(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if msg.chat.type != ChatType.PRIVATE or msg.from_user.id != OWNER_ID:
        return
    args = msg.text.split(maxsplit=1)
    if len(args) < 2:
        await msg.reply_text("Использование: `/announce текст`", parse_mode=ParseMode.MARKDOWN)
        return
    await send_group(context.bot, args[1])
    await msg.reply_text("✅ Отправлено в группу.")


# ══════════════════════════════════════════════════════
#  /stats
# ══════════════════════════════════════════════════════

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if msg.chat.type != ChatType.PRIVATE or msg.from_user.id != OWNER_ID:
        return
    if not active_event:
        await msg.reply_text("Нет активного ивента.")
        return
    elapsed = int((datetime.now() - active_event["started_at"]).total_seconds())
    top = sorted(msg_counts.items(), key=lambda x: x[1], reverse=True)[:5]
    top_text = "\n".join(
        f"{i+1}. {mention(uid)}: {cnt} сообщений"
        for i, (uid, cnt) in enumerate(top)
    ) or "Пока никто не написал"
    await msg.reply_text(
        f"📊 *Статистика ивента*\n"
        f"Тип: {dict(EVENT_TYPES).get(active_event['type'], '?')}\n"
        f"Приз: {active_event['prize']}\n"
        f"⭐ Звёзды победителю: {active_event.get('stars', 0)}\n"
        f"Прошло: {elapsed // 60} мин {elapsed % 60} сек\n\n"
        f"🏆 Топ:\n{top_text}",
        parse_mode=ParseMode.MARKDOWN,
    )


# ══════════════════════════════════════════════════════
#  Callbacks
# ══════════════════════════════════════════════════════

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global pending_prize, pending_stars, waiting_custom_topup

    cb = update.callback_query
    if cb.from_user.id != OWNER_ID:
        await cb.answer("❌ Нет доступа.")
        return

    data = cb.data
    await cb.answer()

    # ── Пополнение ────────────────────────────────────
    if data.startswith("topup:"):
        amount_str = data[6:]
        if amount_str == "custom":
            waiting_custom_topup = True
            await cb.message.edit_text("✏️ Введи сумму пополнения (число звёзд):")
            return

        amount = int(amount_str)
        await cb.message.edit_text(f"💳 Создаю счёт на *{amount} ⭐*...", parse_mode=ParseMode.MARKDOWN)
        try:
            await context.bot.send_invoice(
                chat_id=OWNER_ID,
                title="Пополнение баланса бота",
                description=f"Пополнение на {amount} звёзд для выдачи призов",
                payload=f"topup_{amount}",
                currency="XTR",
                prices=[LabeledPrice(label=f"{amount} звёзд", amount=amount)],
            )
        except Exception as e:
            await cb.message.reply_text(f"❌ Ошибка создания счёта: {e}")
        return

    # ── Выбор звёзд для приза ─────────────────────────
    if data.startswith("pstars:"):
        pending_stars = int(data[7:])
        await cb.message.edit_text(
            f"🎁 Приз: *{pending_prize}*\n"
            f"⭐ Звёзды победителю: *{pending_stars if pending_stars else 'без звёзд'}*\n\n"
            "Выбери тип ивента:",
            parse_mode=ParseMode.MARKDOWN,
        )
        kb = make_kb([[(label, f"etype:{code}")] for code, label in EVENT_TYPES])
        await cb.message.reply_text(
            "🎮 Выбери тип ивента:",
            reply_markup=kb,
        )
        return

    # ── Тип ивента ────────────────────────────────────
    if data.startswith("etype:"):
        etype  = data[6:]
        prize  = pending_prize or "???"
        stars  = pending_stars or 0
        pending_prize = None
        pending_stars = None
        bal = get_balance()

        if stars > 0 and bal < stars:
            await cb.message.edit_text(
                f"❌ *Недостаточно звёзд!*\n\n"
                f"Нужно: {stars}⭐\n"
                f"На балансе: {bal}⭐\n\n"
                f"Пополни баланс: /topup",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        await cb.message.edit_text(
            f"🚀 Запускаю ивент «{prize}» ({stars}⭐)...",
            parse_mode=ParseMode.MARKDOWN,
        )
        await start_event(context.bot, etype, prize, stars)
        return

    # ── Стоп ──────────────────────────────────────────
    if data == "stop_yes":
        await stop_event(context.bot)
        await cb.message.edit_text("✅ Ивент остановлен.")
        return

    if data == "stop_no":
        await cb.message.edit_text("Отмена.")
        return


# ══════════════════════════════════════════════════════
#  Pre-checkout & успешная оплата
# ══════════════════════════════════════════════════════

async def pre_checkout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.pre_checkout_query.answer(ok=True)


async def on_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if msg.from_user.id != OWNER_ID:
        return
    payment = msg.successful_payment
    amount  = payment.total_amount
    payload = payment.invoice_payload

    add_balance(amount, note=f"invoice: {payload}")
    bal = get_balance()

    await msg.reply_text(
        f"✅ *Баланс пополнен!*\n\n"
        f"➕ Зачислено: *{amount} ⭐*\n"
        f"💰 Новый баланс: *{bal} ⭐*",
        parse_mode=ParseMode.MARKDOWN,
    )
    log.info(f"Баланс пополнен на {amount} звёзд. Итого: {bal}")


# ══════════════════════════════════════════════════════
#  Выдача звёзд победителю
# ══════════════════════════════════════════════════════

async def payout_winner(bot: Bot, user_id: int, stars: int, prize_name: str):
    uname = usernames.get(user_id, str(user_id))
    deduct_balance(stars, winner=f"@{uname}")

    caption = (
        f"🎉 *Поздравляю!*\n"
        f"🎁 Ты выиграл *{prize_name}* 🧸 от @godlancet\n"
        f"✅ Подарок отправлен.\n\n"
        f"⭐ Тебе начислено: *{stars} звёзд*\n\n"
        f"‼️ Пишите сообщения в чате, и получайте возможность так же залутать подарки\n\n"
        f"👾 Так же вы можете выбить мишку у @grith в его чате — @PoseidonsGift"
    )
    try:
        await bot.send_photo(
            chat_id=user_id,
            photo="https://i.ibb.co/WN6HrVDx/IMG-20260525-220900-053.jpg",
            caption=caption,
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception:
        try:
            await bot.send_message(
                chat_id=user_id,
                text=caption,
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:
            pass

    bal = get_balance()
    await bot.send_message(
        chat_id=OWNER_ID,
        text=(
            f"📤 *Выплата победителю*\n\n"
            f"👤 Победитель: {mention(user_id)}\n"
            f"⭐ Отправь: *{stars} звёзд*\n"
            f"🎁 Приз: {prize_name}\n\n"
            f"💰 Остаток баланса: *{bal} ⭐*\n\n"
            f"Нажми на имя пользователя и отправь ему {stars}⭐ через Telegram"
        ),
        parse_mode=ParseMode.MARKDOWN,
    )


# ══════════════════════════════════════════════════════
#  Запуск ивента
# ══════════════════════════════════════════════════════

async def start_event(bot: Bot, etype: str, prize: str, stars: int = 0):
    global active_event, event_task

    if active_event:
        await stop_event(bot)

    msg_counts.clear()
    active_event = {
        "type":         etype,
        "prize":        prize,
        "stars":        stars,
        "started_at":   datetime.now(),
        "leader":       None,
        "leader_since": None,
        "quiz_answer":  None,
    }

    stars_txt = f" + *{stars} ⭐*" if stars else ""

    texts = {
        "last_leader": (
            f"🐸 *Ивент начался!*\n\n"
            f"⏰ Цель: продержаться *3 мин без перебива.*\n\n"
            f"🎁 Приз: *{prize}*{stars_txt}"
        ),
        "most_active": (
            f"💬 *Ивент «Самый активный» начался!*\n\n"
            f"⏱ *5 минут* — кто напишет больше всех, тот побеждает!\n\n"
            f"🎁 Приз: *{prize}*{stars_txt}"
        ),
        "random_win": (
            f"🎲 *Ивент «Случайный победитель» начался!*\n\n"
            f"Напиши хоть одно сообщение за 3 минуты — попадёшь в розыгрыш!\n\n"
            f"🎁 Приз: *{prize}*{stars_txt}"
        ),
        "first_sticker": (
            f"🎯 *Ивент «Первый стикер» начался!*\n\n"
            f"Первый кто отправит *любой стикер* — побеждает!\n\n"
            f"🎁 Приз: *{prize}*{stars_txt}"
        ),
        "x2_stars": (
            f"⭐⭐ *Ивент X2 начался!*\n\n"
            f"В течение *15 минут* — активный чат!\n\n"
            f"🎁 Бонусный приз: *{prize}*{stars_txt}"
        ),
        "lottery": (
            f"🎟 *Лотерея началась!*\n\n"
            f"Напиши любое сообщение за *5 минут* — получишь лотерейный билет!\n\n"
            f"🎁 Приз: *{prize}*{stars_txt}"
        ),
        "reaction_win": (
            f"❤️ *Ивент «Активность» начался!*\n\n"
            f"Кто напишет *больше всего сообщений* за 5 минут — победит!\n\n"
            f"🎁 Приз: *{prize}*{stars_txt}"
        ),
    }

    if etype == "quiz":
        question, answer = random.choice(QUIZ_QUESTIONS)
        active_event["quiz_answer"] = answer.lower()
        text = (
            f"🧠 *Викторина началась!*\n\n"
            f"❓ Вопрос: *{question}*\n\n"
            f"Первый правильный ответ побеждает!\n"
            f"🎁 Приз: *{prize}*{stars_txt}"
        )
    else:
        text = texts.get(etype, f"🐸 *Ивент начался!*\n\n🎁 Приз: *{prize}*{stars_txt}")

    await send_group(bot, text)

    tasks_map = {
        "last_leader":  lambda: run_last_leader(bot, prize, stars),
        "most_active":  lambda: run_timed(bot, prize, stars, 300, mode="active"),
        "random_win":   lambda: run_timed(bot, prize, stars, 180, mode="random"),
        "x2_stars":     lambda: run_timed(bot, prize, stars, 900, mode="x2"),
        "lottery":      lambda: run_timed(bot, prize, stars, 300, mode="random"),
        "reaction_win": lambda: run_timed(bot, prize, stars, 300, mode="active"),
    }
    if etype in tasks_map:
        event_task = asyncio.create_task(tasks_map[etype]())


# ══════════════════════════════════════════════════════
#  Таймеры ивентов
# ══════════════════════════════════════════════════════

async def run_last_leader(bot: Bot, prize: str, stars: int):
    global active_event
    await asyncio.sleep(3)
    while active_event and active_event["type"] == "last_leader":
        leader = active_event.get("leader")
        since  = active_event.get("leader_since")
        if leader and since:
            if (datetime.now() - since).total_seconds() >= 180:
                await declare_winner(bot, leader, prize, stars)
                return
        await asyncio.sleep(5)


async def run_timed(bot: Bot, prize: str, stars: int, duration: int, mode: str):
    global active_event
    await asyncio.sleep(duration)
    if not active_event:
        return

    if mode == "x2":
        await send_group(bot, "⭐ *Ивент X2 завершён!* Спасибо за активность!")

    if not msg_counts:
        await send_group(bot, "😔 Никто не участвовал. Ивент отменён.")
        active_event = None
        return

    winner_id = (
        max(msg_counts, key=msg_counts.get)
        if mode == "active"
        else random.choice(list(msg_counts.keys()))
    )
    await declare_winner(bot, winner_id, prize, stars)


async def declare_winner(bot: Bot, user_id: int, prize: str, stars: int = 0):
    global active_event
    w = mention(user_id)
    stars_txt = f"\n⭐ Звёзды: *{stars}*" if stars else ""

    await send_group(
        bot,
        f"🎉 *Ивент завершён!*\n\n"
        f"🏆 Победитель: {w}\n"
        f"🎁 Приз: *{prize}*{stars_txt}",
    )

    active_event = None
    msg_counts.clear()

    if stars > 0:
        await payout_winner(bot, user_id, stars, prize)
    else:
        caption = (
            f"🎉 *Поздравляю!*\n"
            f"🎁 Ты выиграл *{prize}* 🧸 от @godlancet\n"
            f"✅ Подарок отправлен.\n\n"
            f"‼️ Пишите сообщения в чате, и получайте возможность так же залутать подарки\n\n"
            f"👾 Так же вы можете выбить мишку у @grith в его чате — @PoseidonsGift"
        )
        try:
            await bot.send_photo(
                chat_id=user_id,
                photo="https://i.ibb.co/WN6HrVDx/IMG-20260525-220900-053.jpg",
                caption=caption,
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:
            try:
                await bot.send_message(chat_id=user_id, text=caption, parse_mode=ParseMode.MARKDOWN)
            except Exception:
                pass
        try:
            await bot.send_message(
                chat_id=OWNER_ID,
                text=f"✅ Ивент завершён!\nПобедитель: {w}\nПриз: {prize}\n\nНе забудь отправить подарок! 🎁",
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:
            pass


async def stop_event(bot: Bot):
    global active_event, event_task
    if event_task and not event_task.done():
        event_task.cancel()
    event_task = None
    if active_event:
        await send_group(bot, "⛔ *Ивент остановлен администратором.*")
    active_event = None
    msg_counts.clear()


# ══════════════════════════════════════════════════════
#  Сообщения в группе
# ══════════════════════════════════════════════════════

async def on_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global active_event

    msg = update.message
    if not msg:
        return

    if not is_allowed_chat(msg.chat.id, msg.chat.username):
        return

    # Пост из канала → приветствие
    if msg.sender_chat and msg.sender_chat.type == "channel":
        try:
            await msg.reply_text(
                "Здарова! ⭐\n\n"
                "Тут ты можешь общаться в комментариях и чате и получить *Мишку* от @godlancet 🐻\n\n"
                "Просто общайся и получай возможность залутать Мишку или НФТ ПОДАРОК 🎁\n\n"
                "Также можешь выбить мишку у @grith в его чате — @PoseidonsGift",
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception as e:
            log.warning(f"Ошибка ответа на пост: {e}")
        return

    user = msg.from_user
    if not user:
        return
    if user.username:
        usernames[user.id] = user.username

    if not active_event:
        return

    msg_counts[user.id] += 1
    etype = active_event["type"]

    if etype == "last_leader":
        old = active_event.get("leader")
        if old != user.id:
            active_event["leader"] = user.id
            active_event["leader_since"] = datetime.now()
            if old:
                await send_group(
                    context.bot,
                    f"🔄 *Перебито!*\n\nНовый лидер: {mention(user.id)}. До конца: 3 мин.",
                )
    elif etype == "first_sticker" and msg.sticker:
        await declare_winner(context.bot, user.id, active_event["prize"], active_event.get("stars", 0))
    elif etype == "quiz":
        ans = active_event.get("quiz_answer", "")
        if (msg.text or "").lower().strip() == ans:
            await declare_winner(context.bot, user.id, active_event["prize"], active_event.get("stars", 0))


# ══════════════════════════════════════════════════════
#  ЛС владельца (кастомная сумма пополнения / новый ивент)
# ══════════════════════════════════════════════════════

async def owner_pm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global pending_prize, waiting_custom_topup

    msg = update.message
    if msg.chat.type != ChatType.PRIVATE or msg.from_user.id != OWNER_ID:
        return

    text = msg.text or ""

    if waiting_custom_topup:
        waiting_custom_topup = False
        if text.isdigit() and int(text) > 0:
            amount = int(text)
            try:
                await context.bot.send_invoice(
                    chat_id=OWNER_ID,
                    title="Пополнение баланса бота",
                    description=f"Пополнение на {amount} звёзд",
                    payload=f"topup_{amount}",
                    currency="XTR",
                    prices=[LabeledPrice(label=f"{amount} звёзд", amount=amount)],
                )
            except Exception as e:
                await msg.reply_text(f"❌ Ошибка: {e}")
        else:
            await msg.reply_text("❌ Введи корректное число (например: 150)")
        return

    pending_prize = text
    await ask_prize_stars(msg)


# ══════════════════════════════════════════════════════
#  ЛС остальных пользователей
# ══════════════════════════════════════════════════════

async def other_pm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Привет!\n"
        "Общайся в нашем чате и получай возможность выиграть *Мишку* от @godlancet! 🐻\n\n"
        "Переходи: @chatlancet",
        parse_mode=ParseMode.MARKDOWN,
    )


# ══════════════════════════════════════════════════════
#  main
# ══════════════════════════════════════════════════════

def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",    cmd_start))
    app.add_handler(CommandHandler("balance",  cmd_balance))
    app.add_handler(CommandHandler("history",  cmd_history))
    app.add_handler(CommandHandler("topup",    cmd_topup))
    app.add_handler(CommandHandler("event",    cmd_event))
    app.add_handler(CommandHandler("stop",     cmd_stop))
    app.add_handler(CommandHandler("announce", cmd_announce))
    app.add_handler(CommandHandler("stats",    cmd_stats))

    app.add_handler(CallbackQueryHandler(on_callback))

    app.add_handler(PreCheckoutQueryHandler(pre_checkout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, on_payment))

    app.add_handler(MessageHandler(
        filters.ChatType.GROUPS,
        on_group_message,
    ))

    app.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & filters.User(OWNER_ID) & ~filters.COMMAND,
        owner_pm,
    ))

    app.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & ~filters.User(OWNER_ID) & ~filters.COMMAND,
        other_pm,
    ))

    log.info("Бот запущен — @chatlancet")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
