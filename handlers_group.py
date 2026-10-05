"""Обработчики групповых сообщений Юбары."""

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
    DEFAULT_CONTEXT_SIZE,
    KNOWN_PEOPLE_TRIGGERS,
    PROFILE_TRIGGERS,
    UNKNOWN_GROUP_NOTICE_SECONDS,
)
from duel import handle_balance_request, handle_duel_challenge
from images import extract_image_request, image_manager, is_image_command
from llm import (
    build_prompt,
    extract_reported_age,
    generate_reply,
    is_called,
    is_summary_request,
    parse_meta,
)
from message_deletion import handle_delete_bot_messages
from reactions import maybe_react
from voice_transcription import handle_voice_transcription, is_transcription_request

UNKNOWN_GROUP_NOTICE = (
    "Если хотите воспользоваться функциями бота, обратитесь за помощью "
    "к моему создателю @BIGBACA"
)
_last_unknown_group_notice: dict[int, float] = {}
_COMMAND_RE = re.compile(r"^/([a-z_]+)(?:@\w+)?", re.IGNORECASE)
_PROFILE_GROUP_ID_RE = re.compile(
    r"(?:профиль\s+(?:группы|чата)|(?:группа|чат)\s+профиль)"
    r"(?:\s+(?:(?:по\s+)?(?:id|айди)\s*[:#]?\s*)?)\s*(-?\d+)",
    re.IGNORECASE,
)
_PROFILE_COMMAND_RE = re.compile(r"^/profile(?:@\w+)?(?:\s+(-?\d+))?\s*$", re.IGNORECASE)


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


def _format_person_profile(person: dict[str, object]) -> str:
    lines = [
        f"Имя: {person.get('display_name') or person.get('username') or 'не указано'}",
        f"Telegram ID: {person['user_id']}",
        f"Username: @{person['username']}" if person.get("username") else "Username: не указан",
    ]
    callsign = person.get("callsign")
    lines.append(f"Кличка: {callsign or 'не задана'}")
    lines.append(f"Возраст: {person.get('age') or 'не указан'}")
    gender = person.get("gender")
    lines.append(f"Пол: {gender if gender and gender != 'неизвестен' else 'не указан'}")
    if person.get("group_id") is not None:
        lines.append(f"ID группы: {person['group_id']}")
    notes = str(person.get("notes") or "").strip()
    lines.append("Факты:")
    lines.extend(f"• {note}" for note in notes.splitlines() if note.strip())
    if not notes:
        lines.append("• пока не записаны")
    return "\n".join(lines)


def _format_profile_response(
    person: dict[str, object],
    participants: list[dict[str, object]] | None,
    *,
    group_id: int | None = None,
    group_title: str | None = None,
) -> str:
    if participants is None:
        return (
            "Мой королевский архив о тебе:\n"
            f"{_format_person_profile(person)}\n"
            "Будешь делиться фактами — я внесу их в свои записи."
        )

    total = len(participants)
    title = f"«{group_title}»" if group_title else "эта группа"
    lines = [
        f"Профили участников {title} (ID группы: {group_id if group_id is not None else 'текущая'}):",
    ]
    if not total:
        lines.append("Участников пока нет в моём реестре.")
        return "\n".join(lines)
    for index, member in enumerate(participants, start=1):
        lines.append(f"\n{index}.")
        lines.append(_format_person_profile(member))
    lines.append(f"\nВсего участников в реестре: {total}.")
    return "\n".join(lines)


def _parse_profile_group_id(text: str, current_group_id: int) -> int:
    command_match = _PROFILE_COMMAND_RE.fullmatch(text.strip())
    if command_match and command_match.group(1):
        return int(command_match.group(1))
    phrase_match = _PROFILE_GROUP_ID_RE.search(text)
    return int(phrase_match.group(1)) if phrase_match else current_group_id


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


async def _send_profile_messages(
    message: object,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
) -> None:
    chunks: list[str] = []
    current = ""
    for line in text.splitlines():
        if len(current) + len(line) + 1 > 3500 and current:
            chunks.append(current)
            current = ""
        current += ("\n" if current else "") + line
    if current:
        chunks.append(current)
    if not chunks:
        chunks = [text]
    for chunk in chunks:
        sent = await message.reply_text(chunk)
        await database.save_message(
            message.chat_id,
            context.bot.id,
            sent.message_id,
            "Юбара",
            chunk,
        )


async def _apply_meta(
    metadata: dict[str, object] | None,
    *,
    group_id: int,
    author_id: int,
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
    age_value = metadata.get("age")
    age: int | None = None
    if isinstance(age_value, int) and not isinstance(age_value, bool) and 1 <= age_value <= 120:
        age = age_value
    if gender is not None or callsign is not None or age is not None or note is not None:
        await database.update_user_profile(
            target_id,
            group_id,
            gender=gender,
            callsign=callsign,
            age=age,
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
    normalized_text = text.casefold()
    profile_command = _PROFILE_COMMAND_RE.fullmatch(text.strip())
    profile_request = (
        any(phrase in normalized_text for phrase in PROFILE_TRIGGERS)
        or bool(profile_command and not profile_command.group(1))
    )
    group_profile_request = any(
        phrase in normalized_text for phrase in KNOWN_PEOPLE_TRIGGERS
    ) or bool(profile_command and profile_command.group(1))
    requested_group_id = _parse_profile_group_id(text, group_id)
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
        if (
            called
            or replied_to_bot
            or mentioned
            or command
            or profile_request
            or group_profile_request
        ):
            await _send_unknown_group_notice(message, group_id)
        return

    if await image_manager.consume_setting_input(update, context):
        return

    await image_manager.refresh_status(group_id, context.bot, owner_id=user.id)

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

    if await handle_delete_bot_messages(update, context):
        return

    if await handle_duel_challenge(update, context):
        return
    if await handle_balance_request(update, context):
        return

    if text.strip().casefold() == "дэл" and replied_to_bot:
        try:
            await message.reply_to_message.delete()
            await message.delete()
        except TelegramError:
            logging.exception("Не удалось выполнить «дэл» в группе %s", group_id)
        return

    try:
        await database.ensure_user(user.id, group_id, user.username, _user_name(user))
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

    if is_transcription_request(text, message):
        await handle_voice_transcription(message, context)
        return

    is_image_request, image_prompt = extract_image_request(text)
    if is_image_request and (
        is_image_command(text) or called or replied_to_bot or mentioned
    ):
        await image_manager.offer(update, context, image_prompt)
        return

    if not (
        called
        or replied_to_bot
        or mentioned
        or command
        or profile_request
        or group_profile_request
    ):
        return

    summary = is_summary_request(text)
    participants = None
    try:
        history = await database.load_chat_history(
            group_id,
            summary=summary,
            limit=DEFAULT_CONTEXT_SIZE,
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
        if profile_request or group_profile_request:
            if group_profile_request and not await database.is_group_allowed(requested_group_id):
                await message.reply_text(
                    f"Группа с ID {requested_group_id} не найдена в списке разрешённых."
                )
                return
            participants = (
                await database.get_group_users(requested_group_id)
                if group_profile_request
                else None
            )
            answer = _format_profile_response(
                person,
                participants,
                group_id=requested_group_id if group_profile_request else group_id,
                group_title=(
                    message.chat.title
                    if group_profile_request and requested_group_id == group_id
                    else None
                ),
            )
            await _send_profile_messages(message, context, answer)
            await maybe_react(message)
            return
        prompt = build_prompt(
            history=history,
            user_id=user.id,
            user_name=_user_name(user),
            display_name=display_name,
            gender=person["gender"],
            callsign=person["callsign"],
            current_message=text,
            summary=summary,
            profile_request=profile_request,
            current_message_id=message.message_id,
            replied_message=replied_data,
            participants=participants,
        )
    except Exception:
        logging.exception("Не удалось собрать контекст группы %s", group_id)
        if profile_request or group_profile_request:
            await message.reply_text("Не удалось собрать профиль. Попробуй позже.")
            return
        try:
            prompt = build_prompt(
                history=[],
                user_id=user.id,
                user_name=_user_name(user),
                display_name=display_name,
                gender=person["gender"],
                callsign=person["callsign"],
                current_message=text,
                summary=summary,
                current_message_id=message.message_id,
            )
        except Exception:
            logging.exception("Не удалось создать запрос без контекста группы %s", group_id)
            await message.reply_text("Не удалось подготовить ответ. Попробуй позже.")
            return

    try:
        await context.bot.send_chat_action(group_id, ChatAction.TYPING)
    except TelegramError:
        logging.warning("Не удалось показать typing action в группе %s", group_id, exc_info=True)

    raw_answer = await generate_reply(prompt, summary)
    answer, metadata = parse_meta(raw_answer)
    reported_age = extract_reported_age(text)
    if reported_age is not None:
        metadata = {**(metadata or {}), "target_user_id": user.id, "age": reported_age}
    if not answer:
        answer = "Чего уставился? Я просто немного смутилась..."

    try:
        await _apply_meta(metadata, group_id=group_id, author_id=user.id)
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
            logging.exception("Telegram не отправил ответ Юбары в группу %s", group_id)

    if bot_message is not None:
        try:
            await database.save_message(
                group_id,
                context.bot.id,
                bot_message.message_id,
                "Юбара",
                answer,
            )
        except Exception:
            logging.exception("Не удалось сохранить ответ бота в истории группы %s", group_id)
        await maybe_react(message)
        await image_manager.refresh_status(group_id, context.bot, owner_id=user.id)


async def on_new_members(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if (
        message is None
        or message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP)
        or not message.new_chat_members
    ):
        return
    try:
        if not await database.is_group_allowed(message.chat_id):
            return
    except Exception:
        logging.exception("Не удалось проверить доступ группы %s для приветствия.", message.chat_id)
        return

    names = [
        member.first_name or member.username or "новый участник"
        for member in message.new_chat_members
        if not member.is_bot
    ]
    if not names:
        return
    welcome = ", ".join(names)
    try:
        sent = await message.reply_text(
            f"Добро пожаловать, {welcome}! Устраивайтесь поудобнее — "
            "Юбара милостиво разрешает вам здесь освоиться."
        )
        await database.save_message(
            message.chat_id,
            context.bot.id,
            sent.message_id,
            "Юбара",
            sent.text or "Приветствие новых участников",
        )
    except TelegramError:
        logging.exception("Не удалось поприветствовать новых участников группы %s.", message.chat_id)
    except Exception:
        logging.exception("Не удалось сохранить приветствие в истории группы %s.", message.chat_id)
