"""Persistent countdowns to users' deads coming true."""

from __future__ import annotations

import calendar
import hashlib
import io
import logging
import re
import secrets
from datetime import datetime, timedelta
from pathlib import Path
from typing import Final

import edge_tts
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Update
from telegram.error import TelegramError
from telegram.ext import ContextTypes

import database
import llm
from config import MOSCOW_TIMEZONE

_MIN_DEAD_SECONDS = 2 * 24 * 60 * 60
_RESPONSE_WINDOW_SECONDS = 24 * 60 * 60
_DEAD_CAUSES_PATH = Path(__file__).with_name("1000.txt")
_DEAD_TRIGGER_RE = re.compile(
    r"^\s*(?:сколько мне осталось|сколько до конца моих дней)\s*[?!.]*\s*$",
    re.IGNORECASE,
)
_REPLIC_PATH = Path(__file__).with_name("replic.txt")
_DEFAULT_LAST_WORDS: Final[tuple[str, ...]] = (
    "Сейчас я бы очень хотел, чтобы кто-нибудь забыл про меня и пошёл пить чай.",
    "Если это всё, то пусть хотя бы будет весело.",
    "Надеюсь, это был не самый скучный финал.",
    "Я уходил спокойно — просто кто-то слишком быстро нажал на паузу.",
    "Да, я знаю, это странный момент для шутки, но я и не обещал быть серьёзным.",
    "Пожалуйста, не говорите, что я ничего не успел — я успел опоздать.",
    "Давайте считать это эффектной финальной репликой.",
    "Не волнуйтесь, я ушёл красиво, будто это было частью программы.",
)


def _load_last_words() -> tuple[str, ...]:
    if not _REPLIC_PATH.exists():
        return _DEFAULT_LAST_WORDS
    lines: list[str] = []
    for line in _REPLIC_PATH.read_text("utf-8").splitlines():
        cleaned = line.strip()
        if not cleaned:
            continue
        cleaned = re.sub(r"^\d+\.\s*", "", cleaned)
        cleaned = cleaned.strip('"\'')
        if cleaned:
            lines.append(cleaned)
    return tuple(lines) if lines else _DEFAULT_LAST_WORDS


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


def get_daily_dead_prediction(user_id: int, when: datetime | None = None) -> tuple[str, str]:
    now = when or datetime.now(MOSCOW_TIMEZONE)
    causes = _load_dead_causes()
    last_words = _load_last_words()
    day_key = now.date().isoformat()
    digest = hashlib.sha256(f"{user_id}:{day_key}".encode("utf-8")).digest()
    cause_index = int.from_bytes(digest[:4], "big") % len(causes)
    words_index = int.from_bytes(digest[4:8], "big") % len(last_words)
    return causes[cause_index], last_words[words_index]


def get_dead_cause(user_id: int, when: datetime | None = None) -> str:
    return get_daily_dead_prediction(user_id, when)[0]


def get_last_words(user_id: int, when: datetime | None = None) -> str:
    return get_daily_dead_prediction(user_id, when)[1]


def is_dead_trigger(text: str) -> bool:
    return _DEAD_TRIGGER_RE.fullmatch(text) is not None


def dead_confirmation_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "Живой!",
                    callback_data=f"dead:fulfilled:{user_id}",
                ),
                InlineKeyboardButton(
                    "Придумать историю",
                    callback_data=f"dead:story:{user_id}",
                ),
            ]
        ]
    )


def dead_story_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("Придумать историю", callback_data=f"dead:story:{user_id}")]]
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


async def _generate_dead_story_text(cause: str) -> str:
    prompt = (
        "Сочини короткую комичную историю на русском в духе абсурдного черного юмора. "
        "Начинается ровно со слов: 'Дело было так:'. "
        "История должна быть смешной, но не совсем абсурдной; пусть в ней будут хаос, "
        "неудачные решения и серьёзные лица, которые всё усложняют. "
        f"Обязательно вставь эту деталь: причина смерти героя — {cause}. "
        "История должна звучать живо, эмоционально и занимательно, не длиннее 180 слов."
    )
    messages = [
        {
            "role": "system",
            "content": (
                "Ты — талантливый автор комичных историй. Пиши на русском. "
                "Начинай точно со слов 'Дело было так:' и не добавляй лишних вводных фраз. "
                "Держи темп живым, понятным и слегка дурным, но без жестокости."
            ),
        },
        {"role": "user", "content": prompt},
    ]
    failures: list[str] = []

    for model in dict.fromkeys((llm.MODEL_NAME, *llm.SUMMARY_GROQ_MODELS)):
        try:
            response = await llm._client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=0.9,
            )
            if not response.choices:
                raise ValueError("модель вернула пустой список вариантов")
            choice = response.choices[0]
            text = (choice.message.content or "").strip()
            if not text:
                raise ValueError(
                    "модель вернула пустой текст "
                    f"(finish_reason={choice.finish_reason!r}, "
                    f"refusal={choice.message.refusal!r})"
                )
            if not text.lower().startswith("дело было так"):
                text = f"Дело было так: {text}"
            if model != llm.MODEL_NAME:
                logging.info("История создана запасной моделью Groq (%s).", model)
            return text
        except Exception as exc:
            logging.warning(
                "Не удалось создать историю через Groq (%s): %s",
                model,
                exc,
                exc_info=True,
            )
            failures.append(f"{model} ({type(exc).__name__})")

    raise RuntimeError(
        "Не удалось создать историю ни одной моделью Groq: " + "; ".join(failures)
    )


async def _generate_dead_story_audio(story_text: str) -> bytes | None:
    try:
        speech = edge_tts.Communicate(
            story_text,
            voice="ru-RU-SvetlanaNeural",
        )
        audio = bytearray()
        async for chunk in speech.stream():
            if chunk["type"] == "audio":
                audio.extend(chunk["data"])
        if not audio:
            raise ValueError("Edge TTS вернул пустой аудиофайл.")
        return bytes(audio)
    except Exception:
        logging.exception("Не удалось синтезировать историю через бесплатный Edge TTS.")
        return None


async def handle_dead_story_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    user = update.effective_user
    if query is None or user is None:
        return
    if query.data != f"dead:story:{user.id}":
        await query.answer("Эта кнопка предназначена другому участнику.", show_alert=True)
        return

    try:
        await query.answer("Придумываю историю...", show_alert=False)
        if query.message is not None:
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text="🕵️ Жду экспертизы от дэтэктива...",
                reply_to_message_id=query.message.message_id,
            )
        cause, _last_words = get_daily_dead_prediction(user.id, datetime.now(MOSCOW_TIMEZONE))
        story_text = await _generate_dead_story_text(cause)
        audio_bytes = await _generate_dead_story_audio(story_text)
        if query.message is not None:
            if audio_bytes is not None:
                voice_file = InputFile(
                    io.BytesIO(audio_bytes),
                    filename="dead_story.mp3",
                )
                await context.bot.send_audio(
                    chat_id=query.message.chat_id,
                    audio=voice_file,
                    caption=story_text,
                    reply_to_message_id=query.message.message_id,
                )
            else:
                await context.bot.send_message(
                    chat_id=query.message.chat_id,
                    text=story_text,
                    reply_to_message_id=query.message.message_id,
                )
        if query.message is not None:
            await query.edit_message_text("История готова.")
    except Exception:
        logging.exception("Не удалось придумать историю для user_id=%s.", user.id)
        await query.answer("Не удалось придумать историю. Попробуй ещё раз.", show_alert=True)


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

    now = datetime.now(MOSCOW_TIMEZONE)
    cause, last_words = get_daily_dead_prediction(user.id, now)
    status = timer["status"]
    if status == "counting":
        deadline = datetime.fromtimestamp(float(timer["deadline"]), MOSCOW_TIMEZONE)
        if deadline <= now:
            await message.reply_text(
                "🕯️ Время пришло\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                f"Причина смерти: {cause}\n\n"
                f"Последние слова: «{last_words}»\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "Скоро отправлю кнопку для подтверждения.",
                reply_markup=dead_story_keyboard(user.id),
            )
            return
        await message.reply_text(
            "🕰️ До конца жизни осталось\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"{format_dead_countdown(now, deadline)}\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"Причина смерти: {cause}\n\n"
            f"Последние слова: «{last_words}»",
            reply_markup=dead_story_keyboard(user.id),
        )
    elif status == "awaiting_confirmation":
        await message.reply_text(
            "⚰️ Срок настал\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"Причина смерти: {cause}\n\n"
            f"Последние слова: «{last_words}»\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "Живой?",
            reply_markup=dead_confirmation_keyboard(user.id),
        )
    elif status == "fulfilled":
        await message.reply_text(
            "✅ Живой!\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "Сегодня тебе повезло — без приза, но с хорошим финалом."
        )
    else:
        await message.reply_text(
            "🕯️ Срок истёк\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"Причина смерти: {cause}\n\n"
            f"Последние слова: «{last_words}»"
        )


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
        status, _unused_balance = await database.fulfill_dead_timer(
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
        await query.answer("Живой — и это уже победа.")
        await query.edit_message_text(
            "Ты смог победить судьбу. Ни награды, ни приза — просто ещё один день живой."
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
        cause, last_words = get_daily_dead_prediction(user_id, datetime.now(MOSCOW_TIMEZONE))
        try:
            sent_message = await context.bot.send_message(
                chat_id=int(timer["chat_id"]),
                text=(
                    "⚰️ Срок настал\n"
                    "━━━━━━━━━━━━━━━━━━━━\n"
                    f"Причина смерти: {cause}\n\n"
                    f"Последние слова: «{last_words}»\n"
                    "━━━━━━━━━━━━━━━━━━━━\n"
                    "Живой?"
                ),
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
        cause, last_words = get_daily_dead_prediction(user_id, datetime.now(MOSCOW_TIMEZONE))
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
                    "🕯️ Срок подтверждения прошёл\n"
                    "━━━━━━━━━━━━━━━━━━━━\n"
                    f"Причина смерти: {cause}\n\n"
                    f"Последние слова: «{last_words}»"
                ),
            )
        except TelegramError:
            logging.exception(
                "Не удалось обновить результат таймера конца жизни user_id=%s.",
                user_id,
            )
