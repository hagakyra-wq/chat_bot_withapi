"""Telethon userbot session, automation settings, and scheduled actions."""

import asyncio
import json
import logging
import os
from datetime import datetime, time as datetime_time, timedelta, timezone
from typing import Any

from telegram.constants import ChatMemberStatus
from telegram.error import TelegramError
from telethon import TelegramClient, events, utils
from telethon.errors import SessionPasswordNeededError
from telethon.tl.types import MessageEntitySpoiler

import database
from config import (
    TELEGRAM_API_HASH,
    TELEGRAM_API_ID,
    TELEGRAM_USER_SESSION_PATH,
)
from llm import has_goodnight_message

_SETTINGS_KEY = "userbot_settings"
_NIGHT_DONE_KEY = "userbot_night_done"
_DEFAULT_SETTINGS: dict[str, Any] = {
    "night_enabled": False,
    "dm_enabled": False,
    "timezone_offset": 5,
    "groups": [],
}
_NIGHT_MESSAGE = (
    "Всем сладких снов!!! (хоть это и автоответчик, я мысленно пожелал вам это <3!)"
)
_DM_MESSAGE = "Это автоответчик, ожидайте, это не займет слишком много времени (около 5 лет)"


async def get_settings() -> dict[str, Any]:
    raw = await database.get_meta(_SETTINGS_KEY)
    settings = dict(_DEFAULT_SETTINGS)
    if raw:
        try:
            saved = json.loads(raw)
            if isinstance(saved, dict):
                settings.update(saved)
        except (TypeError, json.JSONDecodeError):
            logging.warning("Настройки userbot повреждены; используются значения по умолчанию.")
    try:
        offset = int(settings["timezone_offset"])
    except (TypeError, ValueError):
        offset = 5
    settings["timezone_offset"] = max(-12, min(offset, 14))
    settings["night_enabled"] = bool(settings["night_enabled"])
    settings["dm_enabled"] = bool(settings["dm_enabled"])
    groups = settings.get("groups")
    settings["groups"] = sorted({int(group_id) for group_id in groups}) if isinstance(groups, list) else []
    return settings


async def update_settings(**changes: Any) -> dict[str, Any]:
    settings = await get_settings()
    if "timezone_offset" in changes:
        offset = int(changes["timezone_offset"])
        if not -12 <= offset <= 14:
            raise ValueError("Часовой пояс должен быть в диапазоне UTC-12…UTC+14.")
        settings["timezone_offset"] = offset
    if "night_enabled" in changes:
        settings["night_enabled"] = bool(changes["night_enabled"])
    if "dm_enabled" in changes:
        settings["dm_enabled"] = bool(changes["dm_enabled"])
    if "groups" in changes:
        settings["groups"] = sorted({int(group_id) for group_id in changes["groups"]})
    await database.set_meta(_SETTINGS_KEY, json.dumps(settings, ensure_ascii=False))
    return settings


def format_utc_offset(offset: int) -> str:
    return f"UTC{offset:+d}"


class UserbotManager:
    def __init__(self) -> None:
        self.client: TelegramClient | None = None
        self.user_id: int | None = None
        self._phone: str | None = None
        self._phone_code_hash: str | None = None
        self._qr_login: Any = None
        self._event_handler_attached = False
        self._private_locks: dict[int, asyncio.Lock] = {}
        self._night_lock = asyncio.Lock()
        self.available_groups: list[dict[str, Any]] = []

    @property
    def configured(self) -> bool:
        try:
            return int(TELEGRAM_API_ID) > 0 and bool(TELEGRAM_API_HASH)
        except (TypeError, ValueError):
            return False

    @property
    def authorized(self) -> bool:
        return self.client is not None and self.user_id is not None

    async def initialize(self) -> None:
        if not self.configured:
            logging.info("Userbot отключён: не заданы TELEGRAM_API_ID и TELEGRAM_API_HASH.")
            return
        try:
            client = await self._connect_client()
            if not await client.is_user_authorized():
                await client.disconnect()
                self.client = None
                return
            await self._finish_authorization()
            logging.info("Сессия Telegram userbot подключена.")
        except Exception:
            logging.exception("Не удалось подключить сохранённую сессию Telegram userbot.")
            if self.client is not None:
                await self.client.disconnect()
                self.client = None
                self.user_id = None

    def _new_client(self) -> TelegramClient:
        TELEGRAM_USER_SESSION_PATH.parent.mkdir(parents=True, exist_ok=True)
        return TelegramClient(
            str(TELEGRAM_USER_SESSION_PATH),
            int(TELEGRAM_API_ID),
            TELEGRAM_API_HASH,
        )

    async def _connect_client(self) -> TelegramClient:
        client = self._new_client()
        try:
            self._secure_session_file()
            await client.connect()
            self._secure_session_file()
        except Exception:
            await client.disconnect()
            raise
        self.client = client
        return client

    @staticmethod
    def _secure_session_file() -> None:
        try:
            os.chmod(TELEGRAM_USER_SESSION_PATH, 0o600)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise RuntimeError("Не удалось ограничить доступ к файлу Telegram-сессии.") from exc

    async def request_code(self, phone: str) -> None:
        if not self.configured:
            raise RuntimeError("Для userbot не заданы Telegram API credentials.")
        if self.client is None:
            await self._connect_client()
        result = await self.client.send_code_request(phone)
        self._phone = phone
        self._phone_code_hash = result.phone_code_hash

    async def request_qr(self) -> str:
        if not self.configured:
            raise RuntimeError("Для userbot не заданы Telegram API credentials.")
        if self.client is None:
            await self._connect_client()
        self._qr_login = await self.client.qr_login()
        return self._qr_login.url

    async def sign_in_qr(self) -> bool:
        if self.client is None or self._qr_login is None:
            raise RuntimeError("QR-сессия не найдена. Запроси новый QR-код.")
        try:
            await self._qr_login.wait()
        except SessionPasswordNeededError:
            return False
        self._qr_login = None
        await self._finish_authorization()
        return True

    async def sign_in_code(self, code: str) -> bool:
        if self.client is None or self._phone is None or self._phone_code_hash is None:
            raise RuntimeError("Срок авторизации истёк. Запроси код ещё раз.")
        try:
            await self.client.sign_in(
                phone=self._phone,
                code=code,
                phone_code_hash=self._phone_code_hash,
            )
        except SessionPasswordNeededError:
            return False
        await self._finish_authorization()
        return True

    async def sign_in_password(self, password: str) -> None:
        if self.client is None:
            raise RuntimeError("Срок авторизации истёк. Начни подключение заново.")
        await self.client.sign_in(password=password)
        await self._finish_authorization()

    async def cancel_login(self) -> None:
        self._phone = None
        self._phone_code_hash = None
        self._qr_login = None
        self._qr_login = None
        client = self.client
        if client is not None and not await client.is_user_authorized():
            self.client = None
            await client.disconnect()

    async def _finish_authorization(self) -> None:
        if self.client is None:
            raise RuntimeError("Telegram-клиент не подключён.")
        account = await self.client.get_me()
        if account is None:
            raise RuntimeError("Telegram не вернул профиль авторизованного аккаунта.")
        self.user_id = int(account.id)
        self._phone = None
        self._phone_code_hash = None
        self._qr_login = None
        if not self._event_handler_attached:
            self.client.add_event_handler(
                self._on_private_message,
                events.NewMessage(incoming=True),
            )
            self._event_handler_attached = True

    async def _last_outgoing_message_date(self, chat_id: int) -> datetime | None:
        if self.client is None or self.user_id is None:
            return None
        messages = await self.client.get_messages(
            chat_id,
            limit=1,
            from_user=self.user_id,
        )
        if not messages:
            return None
        message_date = messages[0].date
        if message_date.tzinfo is None:
            message_date = message_date.replace(tzinfo=timezone.utc)
        return message_date.astimezone(timezone.utc)

    async def _on_private_message(self, event: events.NewMessage.Event) -> None:
        if not event.is_private or not self.authorized:
            return
        try:
            sender = await event.get_sender()
            if sender is None or getattr(sender, "bot", False):
                return
            settings = await get_settings()
            if not settings["dm_enabled"]:
                return
            lock = self._private_locks.setdefault(int(event.chat_id), asyncio.Lock())
            async with lock:
                last_outgoing = await self._last_outgoing_message_date(int(event.chat_id))
                now = datetime.now(timezone.utc)
                if last_outgoing is not None and now - last_outgoing <= timedelta(minutes=15):
                    return
                text = _DM_MESSAGE
                spoiler = "(около 5 лет)"
                offset = text.index(spoiler)
                await self.client.send_message(
                    event.chat_id,
                    text,
                    formatting_entities=[
                        MessageEntitySpoiler(offset=offset, length=len(spoiler))
                    ],
                )
        except Exception:
            logging.exception("Не удалось отправить userbot-автоответ в личном чате %s.", event.chat_id)

    async def get_common_groups(self, bot: Any) -> list[dict[str, Any]]:
        if not self.authorized or self.client is None:
            return []
        groups: list[dict[str, Any]] = []
        async for dialog in self.client.iter_dialogs():
            if not dialog.is_group:
                continue
            chat_id = utils.get_peer_id(dialog.entity)
            try:
                member = await bot.get_chat_member(chat_id, bot.id)
            except TelegramError:
                continue
            if member.status in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED):
                continue
            if member.status == ChatMemberStatus.RESTRICTED and not member.is_member:
                continue
            groups.append({"id": int(chat_id), "title": dialog.name or str(chat_id)})
        groups.sort(key=lambda item: str(item["title"]).casefold())
        self.available_groups = groups
        return groups

    async def run_night_cycle(self) -> None:
        if not self.authorized or self.client is None:
            return
        settings = await get_settings()
        if not settings["night_enabled"] or not settings["groups"]:
            return
        offset = timezone(timedelta(hours=settings["timezone_offset"]))
        async with self._night_lock:
            for chat_id in settings["groups"]:
                now_utc = datetime.now(timezone.utc)
                local_now = now_utc.astimezone(offset)
                if not 21 <= local_now.hour <= 23:
                    return
                if await self._night_done_today(chat_id, local_now.date().isoformat()):
                    continue
                try:
                    last_outgoing = await self._last_outgoing_message_date(chat_id)
                    if last_outgoing is not None and now_utc - last_outgoing < timedelta(minutes=45):
                        continue
                    local_start = datetime.combine(
                        local_now.date(),
                        datetime_time(21, 0),
                        tzinfo=offset,
                    )
                    start_utc = local_start.astimezone(timezone.utc)
                    account_messages = await self._account_messages_since(chat_id, start_utc, now_utc)
                    has_goodnight = await has_goodnight_message(account_messages)
                    if not has_goodnight:
                        spoiler = "(хоть это и автоответчик, я мысленно пожелал вам это <3!)"
                        spoiler_offset = _NIGHT_MESSAGE.index(spoiler)
                        await self.client.send_message(
                            chat_id,
                            _NIGHT_MESSAGE,
                            formatting_entities=[
                                MessageEntitySpoiler(
                                    offset=spoiler_offset,
                                    length=len(spoiler),
                                )
                            ],
                        )
                    await self._mark_night_done(chat_id, local_now.date().isoformat())
                except Exception:
                    logging.exception("Ночная задача userbot не удалась для группы %s.", chat_id)

    async def _account_messages_since(
        self,
        chat_id: int,
        start_utc: datetime,
        now_utc: datetime,
    ) -> list[str]:
        if self.client is None or self.user_id is None:
            return []
        messages: list[str] = []
        async for message in self.client.iter_messages(
            chat_id,
            offset_date=now_utc + timedelta(seconds=1),
            from_user=self.user_id,
        ):
            message_date = message.date
            if message_date.tzinfo is None:
                message_date = message_date.replace(tzinfo=timezone.utc)
            if message_date.astimezone(timezone.utc) < start_utc:
                break
            if message.out and message.raw_text:
                messages.append(message.raw_text.strip())
        messages.reverse()
        return messages

    async def _night_done_today(self, chat_id: int, day: str) -> bool:
        raw = await database.get_meta(_NIGHT_DONE_KEY)
        if not raw:
            return False
        try:
            state = json.loads(raw)
        except json.JSONDecodeError:
            return False
        return isinstance(state, dict) and state.get(str(chat_id)) == day

    async def _mark_night_done(self, chat_id: int, day: str) -> None:
        raw = await database.get_meta(_NIGHT_DONE_KEY)
        try:
            state = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            state = {}
        if not isinstance(state, dict):
            state = {}
        state = {key: value for key, value in state.items() if value == day}
        state[str(chat_id)] = day
        await database.set_meta(_NIGHT_DONE_KEY, json.dumps(state))

    async def disconnect_and_reset(self) -> None:
        self._phone = None
        self._phone_code_hash = None
        await self.close()
        for suffix in ("", "-journal", "-wal", "-shm"):
            path = TELEGRAM_USER_SESSION_PATH if not suffix else TELEGRAM_USER_SESSION_PATH.with_name(
                TELEGRAM_USER_SESSION_PATH.name + suffix
            )
            try:
                path.unlink(missing_ok=True)
            except OSError:
                logging.exception("Не удалось удалить файл сессии userbot: %s", path.name)
        await update_settings(night_enabled=False, dm_enabled=False, groups=[])

    async def close(self) -> None:
        client = self.client
        self.client = None
        self.user_id = None
        self._event_handler_attached = False
        if client is not None:
            await client.disconnect()
