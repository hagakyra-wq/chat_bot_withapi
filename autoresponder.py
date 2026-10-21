"""Ночная рассылка в общих группах и автоответ user-аккаунта в личных чатах."""

import asyncio
import logging
from datetime import datetime, time as datetime_time, timedelta, timezone
from typing import Any

from telegram.constants import ChatMemberStatus
from telegram.error import TelegramError
from telegram.ext import Application, ContextTypes
from telethon import TelegramClient, events, types, utils
from telethon.tl.types import MessageEntitySpoiler

import database
import personal_account
from llm import has_goodnight_message

_NIGHT_CHECK_INTERVAL_SECONDS = 60
_INACTIVITY_INTERVAL = timedelta(minutes=45)
_PRIVATE_REPLY_INTERVAL = timedelta(minutes=15)
_NIGHT_MESSAGE = (
    "Всем сладких снов!!! "
    "(хоть это и автоответчик, я мысленно пожелал вам это <3!)"
)
_PRIVATE_REPLY = (
    "Это автоответчик, ожидайте, это не займет слишком много времени "
    "(около 5 лет)"
)
_application: Application | None = None
_event_handler_installed = False
_private_event_builder: events.NewMessage | None = None
_night_job_running = False
_night_job_lock = asyncio.Lock()
_private_chat_locks: dict[int, asyncio.Lock] = {}


def _spoiler_entity(text: str) -> MessageEntitySpoiler:
    start = text.index("(")
    end = text.index(")", start) + 1
    offset = len(text[:start].encode("utf-16-le")) // 2
    length = len(text[start:end].encode("utf-16-le")) // 2
    return MessageEntitySpoiler(offset=offset, length=length)


def _message_datetime(message: Any) -> datetime:
    message_date = message.date
    if message_date.tzinfo is None:
        return message_date.replace(tzinfo=timezone.utc)
    return message_date.astimezone(timezone.utc)


async def list_common_groups(bot: Any) -> list[tuple[int, str]]:
    """Перечисляет группы из белого списка, где состоят оба аккаунта."""
    client = personal_account.get_authorized_client()
    if client is None:
        raise RuntimeError("Личный Telegram-аккаунт не подключён.")

    allowed_group_ids = set(await database.list_allowed_groups())
    if not allowed_group_ids:
        return []

    user_groups: dict[int, str] = {}
    async for dialog in client.iter_dialogs(limit=None):
        entity = dialog.entity
        if isinstance(entity, types.Channel) and not entity.megagroup:
            continue
        if not isinstance(entity, (types.Chat, types.Channel)):
            continue
        group_id = utils.get_peer_id(entity)
        if group_id in allowed_group_ids:
            user_groups[group_id] = str(
                getattr(entity, "title", None) or group_id
            )

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


def _night_done_key(local_date: str, group_id: int) -> str:
    return f"autoresponder_night_done:{local_date}:{group_id}"


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
) -> None:
    done_key = _night_done_key(local_date, group_id)
    if await database.get_meta(done_key) == "1":
        return

    last_outgoing, outgoing_texts = await _evening_user_messages(
        client,
        group_id,
        window_start,
    )
    if last_outgoing is not None and now_utc - last_outgoing < _INACTIVITY_INTERVAL:
        return

    if await has_goodnight_message(outgoing_texts):
        await database.set_meta(done_key, "1")
        logging.info(
            "Ночная рассылка пропущена: пользователь уже пожелал спокойной ночи "
            "в группе %s.",
            group_id,
        )
        return

    await client.send_message(
        group_id,
        _NIGHT_MESSAGE,
        parse_mode=None,
        formatting_entities=[_spoiler_entity(_NIGHT_MESSAGE)],
    )
    await database.set_meta(done_key, "1")
    logging.info("Ночная рассылка отправлена в группу %s.", group_id)


async def night_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Проверяет выбранные группы в интервале 21:00–23:59 локального времени."""
    if _night_job_lock.locked():
        return
    async with _night_job_lock:
        client = personal_account.get_authorized_client()
        if client is None or not personal_account.is_enabled():
            return
        try:
            timezone_offset, night_enabled, _ = (
                await database.get_autoresponder_settings()
            )
            if not night_enabled:
                return
            local_timezone = timezone(timedelta(hours=timezone_offset))
            now_utc = datetime.now(timezone.utc)
            local_now = now_utc.astimezone(local_timezone)
            if not 21 <= local_now.hour <= 23:
                return

            group_ids = await database.get_autoresponder_night_groups()
            if not group_ids:
                return
            window_start = datetime.combine(
                local_now.date(),
                datetime_time(21, 0),
                tzinfo=local_timezone,
            ).astimezone(timezone.utc)
            local_date = local_now.date().isoformat()
            for group_id in group_ids:
                try:
                    await _process_night_group(
                        client,
                        group_id,
                        window_start,
                        now_utc,
                        local_date,
                    )
                except Exception:
                    logging.exception(
                        "Не удалось выполнить ночную проверку группы %s.",
                        group_id,
                    )
        except Exception:
            logging.exception("Не удалось выполнить задачу ночного автоответчика.")


async def _on_private_message(event: events.NewMessage.Event) -> None:
    if not event.is_private or event.out or event.chat_id is None:
        return
    chat_id = event.chat_id
    chat_lock = _private_chat_locks.setdefault(chat_id, asyncio.Lock())
    async with chat_lock:
        client = personal_account.get_authorized_client()
        if client is None or not personal_account.is_enabled():
            return
        try:
            _, _, private_enabled = await database.get_autoresponder_settings()
            if not private_enabled:
                return
            sender = await event.get_sender()
            if getattr(sender, "bot", False):
                return

            now_utc = datetime.now(timezone.utc)
            async for outgoing in client.iter_messages(
                chat_id,
                limit=1,
                from_user="me",
            ):
                if now_utc - _message_datetime(outgoing) <= _PRIVATE_REPLY_INTERVAL:
                    return
                break

            await client.send_message(
                chat_id,
                _PRIVATE_REPLY,
                parse_mode=None,
                formatting_entities=[_spoiler_entity(_PRIVATE_REPLY)],
            )
            logging.info("Автоответ user-аккаунта отправлен в личный чат %s.", chat_id)
        except Exception:
            logging.exception(
                "Не удалось обработать входящее сообщение user-аккаунта "
                "в личном чате %s.",
                chat_id,
            )


async def refresh_user_client() -> None:
    """Подключает обработчик личных входящих для активной user-сессии."""
    global _event_handler_installed, _private_event_builder
    client = personal_account.get_authorized_client()
    if client is None:
        await stop_user_client()
        return
    if _event_handler_installed:
        return
    _private_event_builder = events.NewMessage(incoming=True)
    client.add_event_handler(_on_private_message, _private_event_builder)
    _event_handler_installed = True


async def stop_user_client() -> None:
    global _event_handler_installed, _private_event_builder
    client = personal_account.get_authorized_client()
    if client is not None and _event_handler_installed and _private_event_builder is not None:
        client.remove_event_handler(_on_private_message, _private_event_builder)
    _event_handler_installed = False
    _private_event_builder = None


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
    await refresh_user_client()


async def shutdown() -> None:
    global _application, _night_job_running
    await stop_user_client()
    if _application is not None and _application.job_queue is not None:
        for job in _application.job_queue.get_jobs_by_name("autoresponder-night"):
            job.schedule_removal()
    _night_job_running = False
    _application = None
