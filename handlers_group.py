"""Обработчики групповых сообщений Юбари."""

import logging
import random
import re
import time

from telegram import ReactionTypeEmoji, Update
from telegram.constants import ChatAction, ChatType, MessageEntityType
from telegram.error import TelegramError
from telegram.ext import ContextTypes

import database
from config import (
    AVAILABLE_REACTIONS,
    KNOWN_PEOPLE_TRIGGERS,
    UNKNOWN_GROUP_NOTICE_SECONDS,
)
from llm import build_prompt, generate_reply, is_called, is_summary_request, parse_meta

UNKNOWN_GROUP_NOTICE = (
    "Если хотите воспользоваться функциями бота, обратитесь за помощью "
    "к моему создателю @MEGURSHKA"
)
_last_unknown_group_notice: dict[int, float] = {}
_COMMAND_RE = re.compile(r"^/([a-z_]+)(?:@\w+)?", re.IGNORECASE)


def _user_name(user: object | None) -> str:
    if user is None:
        return "Кто-то"
    return getattr(user, "first_name", None) or getattr(user, "username", None) or "Кто-то"


def _is_reply_to_bot(message: object, bot_id: int) -> bool:
    replied = getattr(message, "reply_to_message", None)
    return bool(replied and replied.from_user and replied.from_user.id == bot_id)


def _is_bot_mentioned(message: object, bot_username: str | None, bot_id: int) -> bool:
    text = getattr(message, "text", "") or ""
    for entity in getattr(message, "entities", None) or []:
        if entity.type == MessageEntityType.TEXT_MENTION:
            if entity.user and entity.user.id == bot_id:
                return True
        elif entity.type == MessageEntityType.MENTION and bot_username:
            mention = text[entity.offset : entity.offset + entity.length].lstrip("@")
            if mention.casefold() == bot_username.casefold():
                return True
    return bool(bot_username and f"@{bot_username}".casefold() in text.casefold())


def _command_name(text: str) -> str | None:
    match = _COMMAND_RE.match(text.strip())
    return match.group(1).casefold() if match else None


async def _send_unknown_group_notice(message: object, group_id: int) -> None:
    now = time.monotonic()
    previous = _last_unknown_group_notice.get(group_id, 0.0)
    if now - previous < UNKNOWN_GROUP_NOTICE_SECONDS:
        return
    _last_unknown_group_notice[group_id] = now
    try:
        await message.reply_text(UNKNOWN_GROUP_NOTICE)
    except TelegramError:
        logging.exception("Не удалось уведомить о закрытом доступе в группе %s", group_id)


async def _apply_meta(
    metadata: dict[str, object] | None,
    *,
    group_id: int,
    author_id: int,
    message: object,
) -> None:
    if not metadata:
        return

    target_value = metadata.get("target_user_id")
    target_id = author_id
    if target_value not in (None, ""):
        if isinstance(target_value, bool):
            target_id = -1
        else:
            try:
                target_id = int(target_value)
            except (TypeError, ValueError):
                target_id = -1
    if target_id != author_id:
        target_record = await database.get_user_data(target_id, group_id)
        if target_record is None:
            logging.warning(
                "META отклонён: target_user_id=%s не известен в группе %s",
                target_id,
                group_id,
            )
            target_id = -1

    reaction = metadata.get("reaction")
    if isinstance(reaction, str) and reaction in AVAILABLE_REACTIONS and random.random() < 0.15:
        try:
            await message.set_reaction(reaction=[ReactionTypeEmoji(reaction)])
        except TelegramError:
            logging.exception("Telegram не установил реакцию META")

    if target_id == -1:
        return
    gender_value = metadata.get("gender")
    gender = (
        gender_value
        if gender_value in ("парень", "девушка") and isinstance(gender_value, str)
        else None
    )
    callsign_value = metadata.get("nickname", metadata.get("callsign"))
    callsign = (
        callsign_value.strip()[:80]
        if isinstance(callsign_value, str) and callsign_value.strip()
        else None
    )
    note_value = metadata.get("note")
    note = note_value.strip()[:500] if isinstance(note_value, str) and note_value.strip() else None
    if gender is not None or callsign is not None or note is not None:
        await database.update_user_profile(
            target_id,
            group_id,
            gender=gender,
            callsign=callsign,
            note=note,
        )


async def on_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if (
        message is None
        or user is None
        or not message.text
        or user.is_bot
        or message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP)
    ):
        return

    group_id = message.chat_id
    text = message.text
    command = _command_name(text)
    replied_to_bot = _is_reply_to_bot(message, context.bot.id)
    called = is_called(text)
    mentioned = _is_bot_mentioned(message, context.bot.username, context.bot.id)

    try:
        allowed = await database.is_group_allowed(group_id)
    except Exception:
        logging.exception("Не удалось проверить белый список группы %s", group_id)
        return

    if not allowed:
        if called or replied_to_bot or mentioned or command:
            await _send_unknown_group_notice(message, group_id)
        return

    if command == "forget_me":
        try:
            deleted = await database.forget_user(user.id, group_id)
            await message.reply_text(
                "Готово, я забыла сохранённые сведения о тебе в этом чате."
                if deleted
                else "В этом чате мне пока нечего о тебе забывать."
            )
        except Exception:
            logging.exception("Ошибка выполнения /forget_me для группы %s", group_id)
            await message.reply_text("Не получилось удалить данные. Попробуй позже.")
        return

    if command == "adm":
        return

    if command in ("reaction", "react") or text.strip().casefold() in ("реакция", "реакцию"):
        target_message = message.reply_to_message or message
        try:
            await target_message.set_reaction(
                reaction=[ReactionTypeEmoji(random.choice(AVAILABLE_REACTIONS))]
            )
        except TelegramError:
            logging.exception("Не удалось установить реакцию командой в группе %s", group_id)
        return

    if text.strip().casefold() == "дэл" and replied_to_bot:
        try:
            await message.reply_to_message.delete()
            await message.delete()
        except TelegramError:
            logging.exception("Не удалось выполнить «дэл» в группе %s", group_id)
        return

    try:
        await database.ensure_user(user.id, group_id, user.username)
        person = await database.get_user_data(user.id, group_id)
        if person is None:
            logging.error("Профиль пользователя %s не создан в группе %s", user.id, group_id)
            return
        display_name = person["callsign"] or _user_name(user)
        await database.save_message(
            group_id,
            user.id,
            message.message_id,
            display_name,
            text,
        )
    except Exception:
        logging.exception("Ошибка сохранения группового сообщения в SQLite")
        return

    if not (called or replied_to_bot or mentioned or command):
        return

    summary = is_summary_request(text)
    try:
        history = await database.get_recent_history(
            group_id,
            100 if summary else 12,
        )
        replied = message.reply_to_message
        replied_data = None
        if replied and replied.text:
            replied_data = (
                replied.message_id,
                _user_name(replied.from_user),
                replied.from_user.id if replied.from_user else None,
                replied.text,
            )
        known_people = any(phrase in text.casefold() for phrase in KNOWN_PEOPLE_TRIGGERS)
        participants = await database.get_group_users(group_id) if known_people else None
        prompt = build_prompt(
            history=history,
            user_id=user.id,
            user_name=_user_name(user),
            display_name=display_name,
            gender=person["gender"],
            callsign=person["callsign"],
            current_message=text,
            summary=summary,
            replied_message=replied_data,
            participants=participants,
        )
    except Exception:
        logging.exception("Не удалось собрать контекст группы %s", group_id)
        return

    try:
        await context.bot.send_chat_action(group_id, ChatAction.TYPING)
    except TelegramError:
        logging.warning("Не удалось показать typing action в группе %s", group_id, exc_info=True)

    raw_answer = await generate_reply(prompt, summary)
    answer, metadata = parse_meta(raw_answer)
    if not answer:
        answer = "Чего уставился? Я просто немного смутилась..."

    try:
        await _apply_meta(metadata, group_id=group_id, author_id=user.id, message=message)
    except Exception:
        logging.exception("Ошибка обработки META в группе %s", group_id)

    bot_message = None
    try:
        bot_message = await message.reply_text(answer)
    except TelegramError:
        logging.exception("Telegram не отправил ответ reply_text; пробую отправить напрямую")
        try:
            bot_message = await context.bot.send_message(chat_id=group_id, text=answer)
        except TelegramError:
            logging.exception("Telegram не отправил ответ Юбари в группу %s", group_id)

    if bot_message is not None:
        try:
            await database.save_message(
                group_id,
                context.bot.id,
                bot_message.message_id,
                "Бачира",
                answer,
            )
        except Exception:
            logging.exception("Не удалось сохранить ответ бота в истории группы %s", group_id)
