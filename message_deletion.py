"""Удаление последних сообщений Юбары по команде пользователя."""

import logging
import re

from telegram import Update
from telegram.error import TelegramError
from telegram.constants import ChatType
from telegram.ext import ContextTypes

import database

_DELETE_COMMAND_RE = re.compile(
    r"^\s*(?P<address>(?:юбара|юбари|юбару|yubara)[,\s:!-]+)?\s*"
    r"(?:(?:дэл\s+(?P<legacy_count>\d+))|"
    r"(?:(?:удали|удалить|сотри|стереть|убери)\s+"
    r"(?:(?:последн\w*)\s+)?(?P<natural_count>\d+)?\s*"
    r"(?:последних\s+)?сообщени\w*))\s*$",
    re.IGNORECASE,
)


def _requested_count(text: str) -> tuple[int, bool] | None:
    match = _DELETE_COMMAND_RE.fullmatch(text)
    if match is None:
        return None
    count = match.group("legacy_count") or match.group("natural_count")
    return int(count) if count else 1, match.group("address") is not None


async def handle_delete_bot_messages(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> bool:
    message = update.effective_message
    if message is None or not message.text:
        return False
    request = _requested_count(message.text)
    if request is None:
        return False
    count, addressed = request
    replied_to_bot = (
        message.reply_to_message is not None
        and message.reply_to_message.from_user is not None
        and message.reply_to_message.from_user.id == context.bot.id
    )
    if not addressed and message.chat.type != ChatType.PRIVATE and not replied_to_bot:
        return False
    if count <= 0:
        await message.reply_text("Укажи положительное количество сообщений.")
        return True

    try:
        message_ids = await database.get_recent_bot_message_ids(
            message.chat_id,
            context.bot.id,
            count,
        )
    except Exception:
        logging.exception("Не удалось получить сообщения Юбары для удаления в чате %s.", message.chat_id)
        await message.reply_text("Не удалось получить список сообщений. Попробуй позже.")
        return True

    deleted_ids: list[int] = []
    failed = 0
    for message_id in message_ids:
        try:
            await context.bot.delete_message(
                chat_id=message.chat_id,
                message_id=message_id,
            )
            deleted_ids.append(message_id)
        except TelegramError:
            failed += 1
            logging.exception(
                "Не удалось удалить сообщение Юбары chat_id=%s message_id=%s.",
                message.chat_id,
                message_id,
            )

    try:
        await database.delete_message_records(
            message.chat_id,
            context.bot.id,
            deleted_ids,
        )
    except Exception:
        logging.exception("Не удалось обновить историю после удаления сообщений в чате %s.", message.chat_id)
        await message.reply_text(
            f"В Telegram удалено {len(deleted_ids)} из {len(message_ids)} сообщений, "
            "но историю диалога обновить не удалось."
        )
        return True

    result = f"Удалено сообщений Юбары: {len(deleted_ids)}."
    if len(message_ids) < count:
        result += f" В истории нашлось только {len(message_ids)}."
    if failed:
        result += f" Не удалось удалить: {failed}."
    await message.reply_text(result)
    return True
