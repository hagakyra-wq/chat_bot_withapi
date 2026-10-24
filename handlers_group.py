"""Обработчики групповых сообщений Юбары."""

import html
import logging
import hashlib
import random
import re
import time
from io import BytesIO
from typing import Any

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    MessageReactionUpdated,
    ReactionTypeEmoji,
    Update,
)
from telegram.constants import ChatAction, ChatType, MessageEntityType
from telegram.error import TelegramError
from telegram.ext import ContextTypes

import database
from banner_store import get_profile_banner
from casino import (
    handle_coin_promo_message,
    handle_game_message,
)
from config import (
    ADMIN_ID,
    AVAILABLE_REACTIONS,
    DEFAULT_CONTEXT_SIZE,
    KNOWN_PEOPLE_TRIGGERS,
    PROFILE_TRIGGERS,
)
from duel import (
    handle_balance_request,
    handle_coin_leaderboard_request,
    handle_duel_challenge,
)
from images import create_profile_card, extract_image_request, image_manager, is_image_command
from llm import (
    build_prompt,
    extract_reported_age,
    extract_reported_nickname,
    extract_summary_period_minutes,
    generate_reply,
    generate_summary,
    is_called,
    is_summary_request,
    parse_meta,
)
from message_deletion import (
    handle_delete_bot_messages,
    handle_delete_replied_message,
)
from reactions import maybe_react
from voice_transcription import handle_voice_transcription, is_transcription_request

_pending_welcome_ages: dict[tuple[int, int], float] = {}
_WELCOME_MESSAGES = (
    "Надеюсь, здесь тебе будет уютно — располагайся.",
    "Добро пожаловать! Пусть разговоры здесь будут приятными.",
    "Рада новому лицу. Осваивайся, а я присмотрю за порядком.",
    "Теперь нас больше — добро пожаловать в нашу компанию!",
)
_COMMAND_RE = re.compile(r"^/([a-z_]+)(?:@\w+)?", re.IGNORECASE)
_PROFILE_GROUP_ID_RE = re.compile(
    r"(?:профиль\s+(?:группы|чата)|(?:группа|чат)\s+профиль)"
    r"(?:\s+(?:(?:по\s+)?(?:id|айди)\s*[:#]?\s*)?)\s*(-?\d+)",
    re.IGNORECASE,
)
_PROFILE_COMMAND_RE = re.compile(r"^/profile(?:@\w+)?(?:\s+(-?\d+))?\s*$", re.IGNORECASE)
_GROUP_PROMO_COMMAND_RE = re.compile(
    r"^/activategroup(?:@\w+)?\s+(?P<code>YUBARA-[A-F0-9]{12})$",
    re.IGNORECASE,
)
_TOP_COMMAND_RE = re.compile(r"^/top(?:@\w+)?(?:\s+(\S+))?\s*$", re.IGNORECASE)
_TOP_TEXT_RE = re.compile(
    r"^топ(?:\s+сообщений)?(?:\s+(?:за\s+)?(\S+))?$",
    re.IGNORECASE,
)
_TOP_PERIODS = {
    "d": "d",
    "day": "d",
    "today": "d",
    "день": "d",
    "сегодня": "d",
    "w": "w",
    "week": "w",
    "неделя": "w",
    "неделю": "w",
    "m": "m",
    "month": "m",
    "месяц": "m",
    "месяце": "m",
}
_TOP_PERIOD_LABELS = {"d": "за день", "w": "за неделю", "m": "за месяц"}


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


def _parse_message_top_request(text: str) -> tuple[bool, str | None]:
    match = _TOP_COMMAND_RE.fullmatch(text.strip())
    if match is None:
        match = _TOP_TEXT_RE.fullmatch(text.strip())
    if match is None:
        return False, None
    raw_period = (match.group(1) or "d").casefold()
    return True, _TOP_PERIODS.get(raw_period)


async def on_group_activity(
    update: Update,
    _context: ContextTypes.DEFAULT_TYPE,
) -> None:
    del _context
    message = update.effective_message
    user = update.effective_user
    if (
        message is None
        or user is None
        or user.is_bot
        or message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP)
        or not (message.text or message.caption or message.effective_attachment)
    ):
        return
    if message.text and _parse_message_top_request(message.text)[0]:
        return
    try:
        if await database.is_group_allowed(message.chat_id):
            await database.record_group_message_activity(message.chat_id, user.id)
    except Exception:
        logging.exception(
            "Не удалось учесть сообщение user_id=%s в статистике группы %s.",
            user.id,
            message.chat_id,
        )


async def _send_message_leaderboard(
    message: Message,
    period: str,
) -> None:
    try:
        leaderboard = await database.get_group_message_leaderboard(
            message.chat_id,
            period,
        )
    except Exception:
        logging.exception(
            "Не удалось загрузить топ сообщений группы %s period=%s.",
            message.chat_id,
            period,
        )
        await message.reply_text("Не удалось загрузить топ сообщений. Попробуй позже.")
        return
    title = _TOP_PERIOD_LABELS[period]
    lines = [f"🏆 Топ сообщений {title}"]
    if leaderboard:
        for rank, member in enumerate(leaderboard, start=1):
            name = re.sub(r"\s+", " ", str(member.get("display_name") or "Участник")).strip()
            username = str(member.get("username") or "").strip().lstrip("@")
            if username and username.casefold() not in name.casefold():
                name = f"{name} (@{username})"
            lines.append(f"{rank}. {name[:80]} — {int(member['message_count'])}")
    else:
        lines.append("За этот период пока нет сообщений.")
    lines.append("\nПериод: текущий день, неделя или месяц по Москве.")
    await message.reply_text("\n".join(lines))


def _format_person_profile(
    person: dict[str, object],
    activity: dict[str, int] | None = None,
    *,
    stats_period: str | None = None,
) -> str:
    display_name = str(person.get("display_name") or "").strip()
    callsign = str(person.get("callsign") or "").strip()
    name = display_name or callsign or "Участник"
    if callsign and callsign.casefold() != name.casefold():
        name = f"{name} · кличка: {callsign}"
    details = [html.escape(name)]
    username = str(person.get("username") or "").strip().lstrip("@")
    if username:
        details.append(f"@{html.escape(username)}")
    age = person.get("age")
    gender = str(person.get("gender") or "").strip()
    personal_details = []
    if isinstance(age, int) and not isinstance(age, bool) and age > 0:
        personal_details.append(str(age))
    if gender and gender != "неизвестен":
        personal_details.append(gender)
    if personal_details:
        details.append(", ".join(html.escape(part) for part in personal_details))
    lines = [f"<b>{' · '.join(details)}</b>"]
    facts = [
        line.strip()
        for line in str(person.get("notes") or "").splitlines()
        if line.strip()
    ]
    if facts:
        fact_text = " · ".join(facts)
        if len(fact_text) > 600:
            fact_text = fact_text[:597].rsplit(" ", 1)[0] + "…"
        lines.append(
            f"<blockquote expandable>{html.escape(fact_text)}</blockquote>"
        )
    if activity is not None and stats_period is not None:
        period_labels = {"d": "D · сегодня", "w": "W · неделя", "m": "M · месяц"}
        if stats_period not in period_labels:
            raise ValueError(f"Неизвестный период статистики группы: {stats_period!r}.")
        lines.append(
            f"<i>Сообщения в группе · {period_labels[stats_period]}: "
            f"{activity.get(stats_period, 0)}</i>"
        )
    elif activity is not None:
        lines.append(
            "<i>Сообщения · D/W/M: "
            f"{activity.get('d', 0)} / {activity.get('w', 0)} / "
            f"{activity.get('m', 0)}</i>"
        )
    return "\n".join(lines)


def _format_profile_response(
    person: dict[str, object],
    participants: list[dict[str, object]] | None,
    *,
    group_title: str | None = None,
    activity_by_user: dict[int, dict[str, int]] | None = None,
    stats_period: str = "d",
) -> str:
    if participants is None:
        user_id = person.get("user_id")
        activity = (
            activity_by_user.get(int(user_id))
            if activity_by_user is not None and user_id is not None
            else None
        )
        return "Профиль участника:\n" + _format_person_profile(
            person,
            activity,
            stats_period=stats_period,
        )

    total = len(participants)
    title = f"«{html.escape(group_title)}»" if group_title else "эта группа"
    lines = [
        f"Профили участников {title}:",
    ]
    if not total:
        lines.append("Участников пока нет в моём реестре.")
        return "\n".join(lines)
    for index, member in enumerate(participants, start=1):
        user_id = member.get("user_id")
        activity = (
            activity_by_user.get(int(user_id))
            if activity_by_user is not None and user_id is not None
            else None
        )
        lines.append(f"\n{index}. {_format_person_profile(member, activity)}")
    lines.append(f"\nВсего участников в реестре: {total}.")
    return "\n".join(lines)


def _parse_profile_group_id(text: str, current_group_id: int) -> int:
    command_match = _PROFILE_COMMAND_RE.fullmatch(text.strip())
    if command_match and command_match.group(1):
        return int(command_match.group(1))
    phrase_match = _PROFILE_GROUP_ID_RE.search(text)
    return int(phrase_match.group(1)) if phrase_match else current_group_id


def _group_profile_stats_keyboard(
    target_user_id: int,
    group_id: int,
    stats_period: str,
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    f"{'● ' if period == stats_period else ''}{label}",
                    callback_data=(
                        f"profile:groupstats:{target_user_id}:{group_id}:{period}"
                    ),
                )
                for period, label in (("d", "D"), ("w", "W"), ("m", "M"))
            ]
        ]
    )


async def _send_profile_messages(
    message: object,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    profile_photo: BytesIO | str | None = None,
    profile_animated: bool = False,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    chunks: list[str] = []
    current = ""
    for line in text.splitlines():
        max_length = 950 if profile_photo is not None else 3500
        if len(current) + len(line) + 1 > max_length and current:
            chunks.append(current)
            current = ""
        current += ("\n" if current else "") + line
    if current:
        chunks.append(current)
    if not chunks:
        chunks = [text]
    if profile_photo is not None and chunks:
        caption = chunks.pop(0)
        if profile_animated:
            sent = await message.reply_animation(
                animation=profile_photo,
                caption=caption,
                parse_mode="HTML",
                reply_markup=reply_markup,
            )
        else:
            sent = await message.reply_photo(
                photo=profile_photo,
                caption=caption,
                parse_mode="HTML",
                reply_markup=reply_markup,
            )
        await database.save_message(
            message.chat_id,
            context.bot.id,
            sent.message_id,
            "Юбара",
            sent.caption or "Профиль пользователя",
        )
    for index, chunk in enumerate(chunks):
        sent = await message.reply_text(
            chunk,
            parse_mode="HTML",
            reply_markup=reply_markup if profile_photo is None and index == 0 else None,
        )
        await database.save_message(
            message.chat_id,
            context.bot.id,
            sent.message_id,
            "Юбара",
            sent.text or chunk,
        )


async def on_group_profile_stats_callback(
    update: Update,
    _context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    if query is None:
        return
    answered = False
    try:
        parts = (query.data or "").split(":")
        if len(parts) != 5 or parts[1] != "groupstats":
            await query.answer("Кнопка профиля устарела.", show_alert=True)
            return
        target_user_id = int(parts[2])
        group_id = int(parts[3])
        stats_period = parts[4]
        if stats_period not in ("d", "w", "m"):
            await query.answer("Неизвестный период статистики.", show_alert=True)
            return
        if (
            query.message is None
            or query.message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP)
            or query.message.chat_id != group_id
            or not await database.is_group_allowed(group_id)
        ):
            await query.answer("Эта карточка больше недоступна.", show_alert=True)
            return
        person = await database.get_user_data(target_user_id, group_id)
        if person is None:
            await query.answer("Профиль участника больше недоступен.", show_alert=True)
            return
        activity = (await database.get_group_message_activity(group_id)).get(
            target_user_id,
            {"d": 0, "w": 0, "m": 0},
        )
        await query.answer()
        answered = True
        await query.message.edit_caption(
            caption="Профиль участника:\n"
            + _format_person_profile(
                person,
                activity,
                stats_period=stats_period,
            ),
            parse_mode="HTML",
            reply_markup=_group_profile_stats_keyboard(
                target_user_id,
                group_id,
                stats_period,
            ),
        )
    except Exception:
        logging.exception("Не удалось переключить статистику группового профиля.")
        if not answered:
            await query.answer("Не удалось обновить статистику. Попробуй ещё раз.")


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
    message = update.message
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
    is_top_request, top_period = _parse_message_top_request(text)
    replied_to_bot = _is_reply_to_bot(message, context.bot.id)
    called = is_called(text)
    mentioned = _is_bot_mentioned(message, context.bot.username, context.bot.id)

    promo_match = _GROUP_PROMO_COMMAND_RE.fullmatch(text.strip())
    if promo_match is not None:
        try:
            is_bot_admin = await database.is_admin_user(user.id)
            if user.id != ADMIN_ID and not is_bot_admin:
                member = await context.bot.get_chat_member(group_id, user.id)
                member_status = getattr(member.status, "value", member.status)
                if member_status not in ("creator", "administrator"):
                    await message.reply_text(
                        "Активировать промокод может только администратор этой группы "
                        "или администратор бота."
                    )
                    return
            result = await database.redeem_group_promo_code(
                promo_match.group("code"),
                group_id,
            )
            if result == "activated":
                await message.reply_text(
                    "Промокод принят. Эта группа подключена, теперь можешь обращаться ко мне."
                )
                logging.info(
                    "Группа %s активирована промокодом администратором user_id=%s.",
                    group_id,
                    user.id,
                )
            elif result == "already_allowed":
                await message.reply_text("Эта группа уже подключена.")
            else:
                await message.reply_text(
                    "Промокод недействителен, уже использован или предназначен другой группе."
                )
        except TelegramError:
            logging.exception(
                "Не удалось проверить права user_id=%s для активации группы %s.",
                user.id,
                group_id,
            )
            await message.reply_text(
                "Не удалось проверить права администратора группы. Попробуй позже."
            )
        except Exception:
            logging.exception("Не удалось активировать группу %s промокодом.", group_id)
            await message.reply_text("Не удалось активировать группу. Попробуй позже.")
        return

    try:
        await database.add_allowed_group(group_id)
        await database.set_group_membership(group_id, user.id, is_member=True)
    except Exception:
        logging.exception(
            "Не удалось сохранить доступ или членство пользователя %s в группе %s.",
            user.id,
            group_id,
        )
        await message.reply_text("Не удалось проверить доступ к боту. Попробуй позже.")
        return

    if await handle_coin_promo_message(update):
        return

    if is_top_request:
        if top_period is None:
            await message.reply_text("Период не распознан. Используй /top d, /top w или /top m.")
            return
        await _send_message_leaderboard(message, top_period)
        return

    if await image_manager.consume_setting_input(update, context):
        return

    pending_age_key = (group_id, user.id)
    pending_age_deadline = _pending_welcome_ages.get(pending_age_key)
    if pending_age_deadline is not None:
        if time.monotonic() > pending_age_deadline:
            _pending_welcome_ages.pop(pending_age_key, None)
        else:
            age_match = re.fullmatch(
                r"\s*(\d{1,3})(?:\s*(?:лет|года?|год))?\s*",
                text,
                re.IGNORECASE,
            )
            if age_match:
                age = int(age_match.group(1))
                if 1 <= age <= 120:
                    try:
                        await database.update_user_profile(user.id, group_id, age=age)
                        _pending_welcome_ages.pop(pending_age_key, None)
                    except Exception:
                        logging.exception(
                            "Не удалось сохранить возраст участника %s в группе %s.",
                            user.id,
                            group_id,
                        )

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
    if await handle_coin_leaderboard_request(update, context):
        return
    if await handle_balance_request(update, context):
        return

    if await handle_delete_replied_message(update, context):
        return

    try:
        await database.ensure_user(
            user.id,
            group_id,
            user.username,
            _user_name(user),
            is_bot=user.is_bot,
        )
        reported_nickname = extract_reported_nickname(text)
        if reported_nickname is not None:
            await database.update_user_profile(
                user.id,
                group_id,
                callsign=reported_nickname,
            )
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
            gender=str(person.get("gender") or "неизвестен"),
            age=person.get("age") if isinstance(person.get("age"), int) else None,
            reply_to=(
                f"{_user_name(message.reply_to_message.from_user)}: "
                f"{message.reply_to_message.text or message.reply_to_message.caption or ''}"
                if message.reply_to_message
                else None
            ),
        )
    except Exception:
        logging.exception("Ошибка сохранения группового сообщения в SQLite")
        return

    if await handle_game_message(update, context):
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
    summary_period_minutes = extract_summary_period_minutes(text)
    if summary and summary_period_minutes is not None:
        if summary_period_minutes <= 0:
            await message.reply_text("Укажи положительный период саммари.")
            return
        if summary_period_minutes > 24 * 60:
            await message.reply_text("Саммари можно запросить максимум за последние 24 часа.")
            return
    if summary_period_minutes is None:
        summary_period_minutes = 12 * 60
    participants = None
    participant_genders: dict[int, str] = {}
    summary_records: list[dict[str, Any]] = []
    try:
        if summary:
            summary_records = await database.get_summary_history_rows(
                group_id,
                summary_period_minutes,
            )
            history = [
                (
                    int(row["message_id"]),
                    str(row["sender_name"]),
                    int(row["user_id"]),
                    str(row["text"]),
                )
                for row in summary_records
            ]
        else:
            history = await database.load_chat_history(
                group_id,
                summary=False,
                limit=DEFAULT_CONTEXT_SIZE,
            )
        participant_genders = await database.get_group_user_genders(group_id)
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
            activity_by_user = await database.get_group_message_activity(
                requested_group_id if group_profile_request else group_id
            )
            answer = _format_profile_response(
                person,
                participants,
                group_title=(
                    message.chat.title
                    if group_profile_request and requested_group_id == group_id
                    else None
                ),
                activity_by_user=activity_by_user,
                stats_period="d",
            )
            profile_photo: BytesIO | str | None = None
            profile_animated = False
            if not group_profile_request:
                media = await get_profile_banner(
                    context.bot,
                    user.id,
                    profile=await database.get_global_profile(user.id),
                )
                if media is not None:
                    profile_photo, profile_animated = media
                else:
                    decoration = await database.get_or_create_profile_decoration(user.id)
                    nickname = str(
                        person.get("callsign")
                        or person.get("display_name")
                        or _user_name(user)
                    )
                    profile_photo = create_profile_card(nickname, decoration)
            await _send_profile_messages(
                message,
                context,
                answer,
                profile_photo,
                profile_animated,
                reply_markup=(
                    _group_profile_stats_keyboard(user.id, group_id, "d")
                    if profile_request
                    else None
                ),
            )
            await maybe_react(message)
            return
        prompt = ""
        if not summary:
            prompt = build_prompt(
                history=history,
                user_id=user.id,
                user_name=_user_name(user),
                display_name=display_name,
                gender=person["gender"],
                callsign=person["callsign"],
                current_message=text,
                summary=False,
                profile_request=profile_request,
                current_message_id=message.message_id,
                replied_message=replied_data,
                participants=participants,
                participant_genders=participant_genders,
            )
    except database.HistoryDecryptionError:
        logging.exception("Ключ шифрования не подошёл к истории группы %s.", group_id)
        await message.reply_text(
            "Не удалось прочитать защищённую историю. Проверь HISTORY_ENCRYPTION_KEY."
        )
        return
    except Exception:
        logging.exception("Не удалось собрать контекст группы %s", group_id)
        if summary:
            await message.reply_text(
                "Не удалось прочитать историю для саммари. Проверь ключ шифрования и попробуй позже."
            )
            return
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
                summary_period_minutes=summary_period_minutes,
                current_message_id=message.message_id,
                participant_genders=participant_genders,
            )
        except Exception:
            logging.exception("Не удалось создать запрос без контекста группы %s", group_id)
            await message.reply_text("Не удалось подготовить ответ. Попробуй позже.")
            return

    try:
        await context.bot.send_chat_action(group_id, ChatAction.TYPING)
    except TelegramError:
        logging.warning("Не удалось показать typing action в группе %s", group_id, exc_info=True)

    if summary:
        try:
            answer = await generate_summary(
                summary_records,
                period_minutes=summary_period_minutes,
                participant_genders=participant_genders,
            )
            metadata = None
        except Exception:
            logging.exception("Gemini не смог подготовить саммари для группы %s.", group_id)
            await message.reply_text(
                "Не удалось подготовить саммари. Проверь настройки Gemini и попробуй позже."
            )
            return
    else:
        raw_answer = await generate_reply(prompt, False)
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
                reply_to=f"{display_name}: {text}",
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
        await database.add_allowed_group(message.chat_id)
    except Exception:
        logging.exception("Не удалось зарегистрировать группу %s.", message.chat_id)
        return

    for member in message.new_chat_members:
        if member.is_bot:
            continue
        name = member.first_name or member.username or "новый участник"
        try:
            await database.set_group_membership(
                message.chat_id,
                member.id,
                is_member=True,
            )
            await database.ensure_user(
                member.id,
                message.chat_id,
                member.username,
                name,
                is_bot=False,
            )
            choice = hashlib.sha256(
                f"{message.chat_id}:{member.id}".encode("utf-8")
            ).digest()[0] % len(_WELCOME_MESSAGES)
            welcome_text = (
                f"{name}, добро пожаловать! {_WELCOME_MESSAGES[choice]} "
                "Если хочешь, можно указать пол и возраст — я сохраню их для ответов; "
                "оба вопроса необязательны."
            )
            sent = await message.reply_text(
                welcome_text,
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton("Парень", callback_data=f"wg:{member.id}:m"),
                            InlineKeyboardButton("Девушка", callback_data=f"wg:{member.id}:f"),
                            InlineKeyboardButton("Пропустить", callback_data=f"wg:{member.id}:s"),
                        ]
                    ]
                ),
            )
            await database.save_message(
                message.chat_id,
                context.bot.id,
                sent.message_id,
                "Юбара",
                welcome_text,
            )
        except TelegramError:
            logging.exception(
                "Не удалось поприветствовать пользователя %s в группе %s.",
                member.id,
                message.chat_id,
            )
        except Exception:
            logging.exception(
                "Не удалось подготовить приветствие пользователя %s в группе %s.",
                member.id,
                message.chat_id,
            )


async def on_welcome_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    del context
    query = update.callback_query
    user = update.effective_user
    chat = update.effective_chat
    if query is None or user is None or chat is None:
        return
    parts = (query.data or "").split(":")
    if len(parts) != 3 or not parts[1].isdigit():
        await query.answer("Кнопка устарела.", show_alert=True)
        return
    if int(parts[1]) != user.id:
        await query.answer("Эта кнопка предназначена другому участнику.", show_alert=True)
        return
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await query.answer("Эта кнопка доступна только в группе.", show_alert=True)
        return
    try:
        if not await database.is_group_allowed(chat.id):
            await query.answer("Группа не подключена.", show_alert=True)
            return
        if parts[0] == "wg":
            gender = {"m": "парень", "f": "девушка"}.get(parts[2])
            if parts[2] not in ("m", "f", "s"):
                await query.answer("Выбор не распознан.", show_alert=True)
                return
            if gender is not None:
                await database.update_user_profile(user.id, chat.id, gender=gender)
            _pending_welcome_ages[(chat.id, user.id)] = time.monotonic() + 900
            await query.answer("Спасибо, выбор сохранён." if gender else "Хорошо.")
            if query.message is not None:
                try:
                    await query.edit_message_reply_markup(reply_markup=None)
                except TelegramError:
                    logging.info("Не удалось убрать кнопки приветствия в группе %s.", chat.id)
                await query.message.reply_text(
                    f"{_user_name(user)}, если хочешь, напиши возраст числом "
                    "(1–120 лет) или нажми «Пропустить».",
                    reply_markup=InlineKeyboardMarkup(
                        [[InlineKeyboardButton("Пропустить", callback_data=f"wa:{user.id}:s")]]
                    ),
                )
        elif parts[0] == "wa" and parts[2] == "s":
            _pending_welcome_ages.pop((chat.id, user.id), None)
            await query.answer("Возраст не сохраняю.")
            if query.message is not None:
                await query.message.delete()
        else:
            await query.answer("Кнопка устарела.", show_alert=True)
    except Exception:
        logging.exception(
            "Не удалось обработать приветственный выбор пользователя %s в группе %s.",
            user.id,
            chat.id,
        )
        await query.answer("Не удалось сохранить выбор. Попробуй позже.", show_alert=True)


async def on_message_reaction(
    reaction_update: MessageReactionUpdated,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    del context
    if reaction_update.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return
    if reaction_update.user is None:
        return
    try:
        if not await database.is_group_allowed(reaction_update.chat.id):
            return
        reaction = ", ".join(
            getattr(item, "emoji", None)
            or getattr(item, "custom_emoji_id", None)
            or "реакция"
            for item in reaction_update.new_reaction
        )
        await database.update_message_reaction(
            reaction_update.chat.id,
            reaction_update.message_id,
            reaction_update.user.id,
            _user_name(reaction_update.user),
            reaction or None,
        )
    except Exception:
        logging.exception(
            "Не удалось сохранить реакцию на сообщение %s в группе %s.",
            reaction_update.message_id,
            reaction_update.chat.id,
        )
