"""Операционные события и предупреждения в Telegram-канал."""

import asyncio
import logging
import re

from telegram import Bot
from telegram.error import TelegramError

from config import LOGS_CHAT, TELEGRAM_TOKEN

_LOGGER_NAME = "yubara.channel_log"
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_TOKEN_RE = re.compile(re.escape(TELEGRAM_TOKEN))
_GROQ_KEY_RE = re.compile(r"gsk-[A-Za-z0-9_-]+")
_TELEGRAM_BOT_TOKEN_RE = re.compile(r"\b\d{8,}:[A-Za-z0-9_-]{20,}\b")


def _safe_message(text: str) -> str:
    safe = _URL_RE.sub("[URL скрыт]", text)
    safe = _TOKEN_RE.sub("[токен скрыт]", safe)
    safe = _GROQ_KEY_RE.sub("[ключ Groq скрыт]", safe)
    safe = _TELEGRAM_BOT_TOKEN_RE.sub("[токен скрыт]", safe)
    return safe[:3500]


async def send_channel_event(bot: Bot, text: str) -> None:
    try:
        await bot.send_message(
            chat_id=LOGS_CHAT,
            text=_safe_message(text),
            disable_notification=True,
        )
    except TelegramError:
        logging.getLogger(_LOGGER_NAME).warning(
            "Не удалось отправить операционное событие в канал бэкапов.",
            exc_info=True,
        )


class TelegramChannelLogHandler(logging.Handler):
    """Пересылает WARNING и ERROR в канал без traceback и ссылок с данными."""

    def __init__(self, bot: Bot) -> None:
        super().__init__(level=logging.WARNING)
        self.bot = bot
        self.loop = asyncio.get_running_loop()

    def emit(self, record: logging.LogRecord) -> None:
        if record.name == _LOGGER_NAME or record.name.startswith(_LOGGER_NAME + "."):
            return
        try:
            text = _safe_message(
                f"⚠️ {record.levelname}: {record.name}: {record.getMessage()}"
            )
            self.loop.call_soon_threadsafe(self._schedule_send, text)
        except (RuntimeError, ValueError):
            self.handleError(record)

    def _schedule_send(self, text: str) -> None:
        if not self.loop.is_closed():
            self.loop.create_task(send_channel_event(self.bot, text))


def install_channel_log_handler(bot: Bot) -> TelegramChannelLogHandler:
    handler = TelegramChannelLogHandler(bot)
    logging.getLogger().addHandler(handler)
    return handler


def remove_channel_log_handler(handler: logging.Handler) -> None:
    root_logger = logging.getLogger()
    root_logger.removeHandler(handler)
    handler.close()
