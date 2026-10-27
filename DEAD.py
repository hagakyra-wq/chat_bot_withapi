"""Persistent countdowns to users' deads coming true."""

from __future__ import annotations

import calendar
import hashlib
import logging
import re
import secrets
from datetime import datetime, timedelta
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import ContextTypes

import database
from config import MOSCOW_TIMEZONE

_MIN_DEAD_SECONDS = 2 * 24 * 60 * 60
_RESPONSE_WINDOW_SECONDS = 24 * 60 * 60
_DEAD_CAUSES_PATH = Path(__file__).with_name("1000.txt")
_DEAD_TRIGGER_RE = re.compile(
    r"^\s*(?:сколько мне осталось|сколько до конца моих дней)\s*[?!.]*\s*$",
    re.IGNORECASE,
)


def _load_dead_causes() -> list[str]:
    if not _DEAD_CAUSES_PATH.exists():
        return ["неизвестная причина смерти"]
    causes: list[str] = []
    for line in _DEAD_CAUSES_PATH.read_text("utf-8").splitlines():
        cleaned = line.strip()
        if not cleaned:
            continue
        cleaned = re.sub(r"^\d+\.\s*", "", cleaned)
        if cleaned:
            causes.append(cleaned)
    return causes or ["неизвестная причина смерти"]


def get_dead_cause(user_id: int) -> str:
    causes = _load_dead_causes()
    digest = hashlib.sha256(str(user_id).encode("utf-8")).digest()
    index = int.from_bytes(digest[:4], "big") % len(causes)
    return causes[index]


def is_dead_trigger(text: str) -> bool:
    return _DEAD_TRIGGER_RE.fullmatch(text) is not None


def dead_confirmation_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "Живой!",
                    callback_data=f"dead:fulfilled:{user_id}",
                )
            ]
        ]
    )


def _add_months(value: datetime, months: int) -> datetime:
    month_index = value.year * 12 + value.month - 1 + months
    year, month = divmod(month_index, 12)
    month += 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


def _plural(value: int, forms: tuple[str, str, str]) -> str:
    last_two = value % 100
    if 11 <= last_two <= 14:
        form = forms[2]
    elif value % 10 == 1:
        form = forms[0]
    elif 2 <= value % 10 <= 4:
        form = forms[1]
    else:
        form = forms[2]
    return f"{value} {form}"


def format_dead_countdown(now: datetime, deadline: datetime) -> str:
    remaining = max(timedelta(0), deadline - now)
    target = now + remaining
    total_months = 0
    while _add_months(now, total_months + 12) <= target:
        total_months += 12
    years = total_months // 12
    while _add_months(now, total_months + 1) <= target:
        total_months += 1
    months = total_months % 12
    cursor = _add_months(now, total_months)
    seconds_left = int((target - cursor).total_seconds())
    days, seconds_left = divmod(seconds_left, 24 * 60 * 60)
    hours, seconds_left = divmod(seconds_left, 60 * 60)
    minutes, seconds = divmod(seconds_left, 60)

    return ", ".join(
        (
            _plural(years, ("год", "года", "лет")),
            _plural(months, ("месяц", "месяца", "месяцев")),
            _plural(days, ("день", "дня", "дней")),
            _plural(hours, ("час", "часа", "часов")),
            _plural(minutes, ("минута", "минуты", "минут")),
            _plural(seconds, ("секунда", "секунды", "секунд")),
        )
    )


def _new_deadline() -> datetime:
    now = datetime.now(MOSCOW_TIMEZONE)
    max_seconds = int((_add_months(now, 1200) - now).total_seconds())
    seconds = _MIN_DEAD_SECONDS + secrets.randbelow(
        max_seconds - _MIN_DEAD_SECONDS + 1
    )
    return now + timedelta(seconds=seconds)


async def dead_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None or user.is_bot:
        return

    try:
        timer = await database.create_or_get_dead_timer(
            user.id,
            user.username,
            message.chat_id,
            _new_deadline().timestamp(),
        )
    except Exception:
        logging.exception("Не удалось создать или загрузить таймер конца жизни user_id=%s.", user.id)
        await message.reply_text("Не удалось загрузить таймер конца жизни. Попробуй позже.")
        return

    status = timer["status"]
    if status == "counting":
        now = datetime.now(MOSCOW_TIMEZONE)
        deadline = datetime.fromtimestamp(float(timer["deadline"]), MOSCOW_TIMEZONE)
        if deadline <= now:
            await message.reply_text(
                "Срок настал — скоро отправлю кнопку для подтверждения.\n"
                f"Причина смерти: {get_dead_cause(user.id)}."
            )
            return
        await message.reply_text(
            "До конца жизни осталось:\n"
            f"{format_dead_countdown(now, deadline)}\n"
            f"Причина смерти: {get_dead_cause(user.id)}."
        )
    elif status == "awaiting_confirmation":
        await message.reply_text(
            f"Срок настал. Причина смерти: {get_dead_cause(user.id)}.\nЖивой?",
            reply_markup=dead_confirmation_keyboard(user.id),
        )
    elif status == "fulfilled":
        await message.reply_text("Живой! — награда 100 монет уже получена.")
    else:
        await message.reply_text("Срок истёк, возможно его больше нет с нами.")


async def handle_dead_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    user = update.effective_user
    if query is None or user is None:
        return
    if query.data != f"dead:fulfilled:{user.id}":
        await query.answer("Эта кнопка предназначена другому участнику.", show_alert=True)
        return

    try:
        status, balance = await database.fulfill_dead_timer(
            user.id,
            user.username,
            datetime.now(MOSCOW_TIMEZONE).timestamp(),
        )
    except Exception:
        logging.exception(
            "Не удалось подтвердить признаки жизни user_id=%s.",
            user.id,
        )
        await query.answer("Не удалось сохранить ответ. Попробуй ещё раз.", show_alert=True)
        return

    if status == "fulfilled":
        await query.answer("Поздравляю! Начислено 100 монет.")
        await query.edit_message_text(
            "Ты смог победить судьбу! Тебе начислено 100 монет. "
            f"Баланс: {int(balance or 0) / 100:.2f} монет."
        )
    elif status == "not_fulfilled":
        await query.answer("Время для подтверждения закончилось.", show_alert=True)
        await query.edit_message_text("Срок истёк, возможно его больше нет с нами.")
    else:
        await query.answer("Эта кнопка уже недоступна.", show_alert=True)


async def process_dead_timers(context: ContextTypes.DEFAULT_TYPE) -> None:
    now = datetime.now(MOSCOW_TIMEZONE).timestamp()
    due_timers = await database.get_due_dead_timers(now)
    for timer in due_timers:
        user_id = int(timer["user_id"])
        try:
            sent_message = await context.bot.send_message(
                chat_id=int(timer["chat_id"]),
                text=f"Срок настал. Причина смерти: {get_dead_cause(user_id)}.\nЖивой?",
                reply_markup=dead_confirmation_keyboard(user_id),
            )
        except TelegramError:
            logging.exception(
                "Не удалось отправить подтверждение жизни user_id=%s; повторю позже.",
                user_id,
            )
            continue
        await database.mark_dead_prompt_sent(
            user_id,
            sent_message.message_id,
            now + _RESPONSE_WINDOW_SECONDS,
        )

    expired_prompts = await database.get_expired_dead_prompts(now)
    for timer in expired_prompts:
        user_id = int(timer["user_id"])
        if not await database.mark_dead_not_fulfilled(user_id, now):
            continue
        message_id = timer["prompt_message_id"]
        if message_id is None:
            continue
        try:
            await context.bot.edit_message_text(
                chat_id=int(timer["chat_id"]),
                message_id=int(message_id),
                text=(
                    "Срок подтверждения прошёл — возможно, его больше нет с нами.\n"
                    f"Причина смерти: {get_dead_cause(user_id)}."
                ),
            )
        except TelegramError:
            logging.exception(
                "Не удалось обновить результат таймера конца жизни user_id=%s.",
                user_id,
            )
