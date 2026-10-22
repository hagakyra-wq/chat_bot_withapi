"""Ночная рассылка в общих группах и автоответ user-аккаунта в личных чатах."""

import asyncio
import logging
from datetime import datetime, time as datetime_time, timedelta, timezone
from functools import partial
from typing import Any

from telegram.constants import ChatMemberStatus
from telegram.error import TelegramError
from telegram.ext import Application, ContextTypes
from telethon import TelegramClient, events, types, utils
from telethon.tl.types import MessageEntitySpoiler

import database
import personal_account
from config import ADMIN_ID
from llm import has_goodnight_message

_NIGHT_CHECK_INTERVAL_SECONDS = 60
_INACTIVITY_INTERVAL = timedelta(minutes=45)
_PRIVATE_REPLY_INTERVAL = timedelta(minutes=15)
_DEFAULT_NIGHT_MESSAGE = (
    "Всем сладких снов!!! "
    "(хоть это и автоответчик, я мысленно пожелал вам это <3!)"
)
_PRIVATE_REPLY = (
    "Это автоответчик, ожидайте, это не займет слишком много времени "
    "(около 5 лет)"
)
_application: Application | None = None
_private_event_builders: dict[int, events.NewMessage] = {}
_private_event_handlers: dict[int, Any] = {}
_night_job_running = False
_night_job_lock = asyncio.Lock()
_private_chat_locks: dict[int, asyncio.Lock] = {}


def _spoiler_entities(text: str) -> list[MessageEntitySpoiler]:
    entities: list[MessageEntitySpoiler] = []
    depth = 0
    start: int | None = None
    for index, character in enumerate(text):
        if character == "(":
            if depth == 0:
                start = index
            depth += 1
        elif character == ")" and depth:
            depth -= 1
            if depth == 0 and start is not None:
                before = len(text[:start].encode("utf-16-le")) // 2
                length = len(text[start : index + 1].encode("utf-16-le")) // 2
                entities.append(MessageEntitySpoiler(offset=before, length=length))
                start = None
    return entities


def _message_datetime(message: Any) -> datetime:
    message_date = message.date
    if message_date.tzinfo is None:
        return message_date.replace(tzinfo=timezone.utc)
    return message_date.astimezone(timezone.utc)


def _autoresponder_user_ids(feature_user_ids: list[int] | None = None) -> list[int]:
    return list(dict.fromkeys([ADMIN_ID, *(feature_user_ids or [])]))


async def list_common_groups(
    bot: Any,
    user_id: int = ADMIN_ID,
) -> list[tuple[int, str]]:
    """Перечисляет группы из белого списка, где состоят оба аккаунта."""
    client = personal_account.get_authorized_client(user_id)
    if client is None:
        raise RuntimeError("Личный Telegram-аккаунт не подключён.")

    user_groups: dict[int, str] = {}
    async for dialog in client.iter_dialogs(limit=None):
        entity = dialog.entity
        if isinstance(entity, types.Channel) and not entity.megagroup:
            continue
        if not isinstance(entity, (types.Chat, types.Channel)):
            continue
        group_id = utils.get_peer_id(entity)
        user_groups[group_id] = str(getattr(entity, "title", None) or group_id)

    common_groups: list[tuple[int, str]] = []
    bot_user = await bot.get_me()
    for group_id, title in sorted(user_groups.items(), key=lambda item: item[1].casefold()):
        try:
            bot_member = await bot.get_chat_member(group_id, bot_user.id)
        except TelegramError:
            logging.info(
                "Группа %s недоступна боту; исключаю её из списка авторассылки.",
                group_id,
                exc_info=True,
            )
            continue
        if bot_member.status in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED):
            continue
        if (
            bot_member.status == ChatMemberStatus.RESTRICTED
            and not getattr(bot_member, "is_member", False)
        ):
            continue
        common_groups.append((group_id, title))
    return common_groups


def _night_done_key(local_date: str, group_id: int, user_id: int = ADMIN_ID) -> str:
    return f"autoresponder_night_done:{user_id}:{local_date}:{group_id}"


def _is_night_check_time(local_now: datetime) -> bool:
    return (local_now.hour == 23 and local_now.minute >= 30) or local_now.hour < 3


def _night_cycle_date(local_now: datetime) -> str:
    night_date = local_now.date()
    if local_now.hour < 3:
        night_date -= timedelta(days=1)
    return night_date.isoformat()


async def _last_outgoing_message(client: TelegramClient) -> datetime | None:
    async for message in client.iter_messages(None, limit=1, from_user="me"):
        return _message_datetime(message)
    return None


async def _evening_user_messages(
    client: TelegramClient,
    group_id: int,
    window_start: datetime,
) -> tuple[datetime | None, list[str]]:
    last_outgoing: datetime | None = None
    outgoing_texts: list[str] = []
    async for message in client.iter_messages(group_id, limit=None):
        message_date = _message_datetime(message)
        if message.out and last_outgoing is None:
            last_outgoing = message_date
        if message_date < window_start:
            break
        if not message.out:
            continue
        text = getattr(message, "message", None)
        if text and text.strip():
            outgoing_texts.append(text.strip())
    return last_outgoing, outgoing_texts


async def _process_night_group(
    client: TelegramClient,
    group_id: int,
    window_start: datetime,
    now_utc: datetime,
    local_date: str,
    user_id: int = ADMIN_ID,
) -> None:
    done_key = _night_done_key(local_date, group_id, user_id)
    if await database.get_meta(done_key) == "1":
        return

    last_outgoing, outgoing_texts = await _evening_user_messages(
        client,
        group_id,
        window_start,
    )
    if last_outgoing is not None and now_utc - last_outgoing <= _INACTIVITY_INTERVAL:
        return

    if await has_goodnight_message(outgoing_texts):
        await database.set_meta(done_key, "1")
        logging.info(
            "Ночная рассылка пропущена: пользователь уже пожелал спокойной ночи "
            "в группе %s.",
            group_id,
        )
        return

    night_text = await database.get_autoresponder_night_text(user_id)
    await client.send_message(
        group_id,
        night_text,
        parse_mode=None,
        formatting_entities=_spoiler_entities(night_text),
    )
    await database.set_meta(done_key, "1")
    logging.info("Ночная рассылка отправлена в группу %s.", group_id)


async def night_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Проверяет пожелание спокойной ночи ровно в 23:30 местного времени."""
    if _night_job_lock.locked():
        return
    async with _night_job_lock:
        try:
            feature_user_ids = await database.list_feature_user_ids("autoresponder")
            active_user_ids = set(feature_user_ids)
            expired_user_ids = await database.list_feature_user_ids(
                "autoresponder",
                include_expired=True,
            )
            for user_id in expired_user_ids:
                if user_id in active_user_ids or not personal_account.is_enabled(user_id):
                    continue
                personal_account.set_enabled(False, user_id)
                await stop_user_client(user_id)
                await personal_account.disconnect(user_id)
                logging.info(
                    "Отключён автоответчик после истечения доступа user_id=%s.",
                    user_id,
                )

            user_ids = _autoresponder_user_ids(feature_user_ids)
            for user_id in user_ids:
                client = personal_account.get_authorized_client(user_id)
                if client is None or not personal_account.is_enabled(user_id):
                    continue
                try:
                    timezone_offset, night_enabled, _ = (
                        await database.get_autoresponder_settings(user_id)
                    )
                    if not night_enabled:
                        continue
                    local_timezone = timezone(timedelta(hours=timezone_offset))
                    now_utc = datetime.now(timezone.utc)
                    local_now = now_utc.astimezone(local_timezone)
                    if not _is_night_check_time(local_now):
                        continue

                    group_ids = await database.get_autoresponder_night_groups(user_id)
                    if not group_ids:
                        continue
                    local_date = _night_cycle_date(local_now)
                    window_start = datetime.combine(
                        datetime.fromisoformat(local_date).date(),
                        datetime_time(21, 0),
                        tzinfo=local_timezone,
                    ).astimezone(timezone.utc)
                    for group_id in group_ids:
                        try:
                            await _process_night_group(
                                client,
                                group_id,
                                window_start,
                                now_utc,
                                local_date,
                                user_id,
                            )
                        except Exception:
                            logging.exception(
                                "Не удалось выполнить ночную проверку user_id=%s, group_id=%s.",
                                user_id,
                                group_id,
                            )
                except Exception:
                    logging.exception(
                        "Не удалось выполнить задачу ночного автоответчика user_id=%s.",
                        user_id,
                    )
        except Exception:
            logging.exception("Не удалось выполнить задачу ночного автоответчика.")


async def _on_private_message(
    user_id: int,
    event: events.NewMessage.Event,
) -> None:
    if not event.is_private or event.out or event.chat_id is None:
        return
    if user_id != ADMIN_ID and not await database.is_admin_user(user_id):
        if not await database.is_feature_authorized(user_id, "autoresponder"):
            await stop_user_client(user_id)
            personal_account.set_enabled(False, user_id)
            await personal_account.disconnect(user_id)
            return
    chat_id = event.chat_id
    chat_lock = _private_chat_locks.setdefault(chat_id, asyncio.Lock())
    async with chat_lock:
        client = personal_account.get_authorized_client(user_id)
        if client is None or not personal_account.is_enabled(user_id):
            return
        try:
            _, _, private_enabled = await database.get_autoresponder_settings(user_id)
            if not private_enabled:
                return
            sender = await event.get_sender()
            if getattr(sender, "bot", False):
                return

            now_utc = datetime.now(timezone.utc)
            last_outgoing = await _last_outgoing_message(client)
            if (
                last_outgoing is not None
                and now_utc - last_outgoing <= _PRIVATE_REPLY_INTERVAL
            ):
                return

            await client.send_message(
                chat_id,
                _PRIVATE_REPLY,
                parse_mode=None,
                formatting_entities=_spoiler_entities(_PRIVATE_REPLY),
            )
            logging.info("Автоответ user-аккаунта отправлен в личный чат %s.", chat_id)
        except Exception:
            logging.exception(
                "Не удалось обработать входящее сообщение user-аккаунта "
                "в личном чате %s.",
                chat_id,
            )


async def refresh_user_client(user_id: int = ADMIN_ID) -> None:
    """Подключает обработчик личных входящих для активной user-сессии."""
    client = personal_account.get_authorized_client(user_id)
    if client is None:
        await stop_user_client(user_id)
        return
    if user_id in _private_event_handlers:
        return
    handler = partial(_on_private_message, user_id)
    builder = events.NewMessage(incoming=True)
    client.add_event_handler(handler, builder)
    _private_event_handlers[user_id] = handler
    _private_event_builders[user_id] = builder


async def stop_user_client(user_id: int = ADMIN_ID) -> None:
    client = personal_account.get_authorized_client(user_id)
    handler = _private_event_handlers.pop(user_id, None)
    builder = _private_event_builders.pop(user_id, None)
    if client is not None and handler is not None and builder is not None:
        client.remove_event_handler(handler, builder)


async def initialize(application: Application) -> None:
    """Запускает периодическую задачу и восстанавливает private listener."""
    global _application, _night_job_running
    _application = application
    if application.job_queue is None:
        raise RuntimeError("JobQueue недоступен для ночного автоответчика.")
    if not _night_job_running:
        application.job_queue.run_repeating(
            night_job,
            interval=_NIGHT_CHECK_INTERVAL_SECONDS,
            first=5,
            name="autoresponder-night",
        )
        _night_job_running = True
    feature_user_ids = await database.list_feature_user_ids("autoresponder")
    for user_id in _autoresponder_user_ids(feature_user_ids):
        if personal_account.is_enabled(user_id):
            try:
                await personal_account.connect_saved_session(user_id)
            except Exception:
                logging.exception("Не удалось подключить user-сессию user_id=%s.", user_id)
            await refresh_user_client(user_id)


async def shutdown() -> None:
    global _application, _night_job_running
    for user_id in tuple(_private_event_handlers):
        await stop_user_client(user_id)
    if _application is not None and _application.job_queue is not None:
        for job in _application.job_queue.get_jobs_by_name("autoresponder-night"):
            job.schedule_removal()
    _night_job_running = False
    _application = None
