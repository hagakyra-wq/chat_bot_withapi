"""Удаление последних сообщений Юбары по команде пользователя."""

import logging
import re

from telegram import Update
from telegram.constants import ChatType
from telegram.error import TelegramError
from telegram.ext import ContextTypes

import database
import personal_account

_DELETE_COMMAND_RE = re.compile(
    r"^\s*(?P<address>(?:юбара|юбари|юбару|yubara)[,\s:!-]+)?\s*"
    r"(?:(?:дэл\s+(?P<legacy_count>\d+))|"
    r"(?:(?:удали|удалить|сотри|стереть|убери)\s+"
    r"(?:(?:последн\w*)\s+)?(?P<natural_count>\d+)?\s*"
    r"(?:последних\s+)?сообщени\w*))\s*$",
    re.IGNORECASE,
)
_DELETE_CHAT_COMMAND_RE = re.compile(r"^\s*дэл\s+чат\s+(\d+)\s*$", re.IGNORECASE)


def _requested_count(text: str) -> tuple[int, bool] | None:
    match = _DELETE_COMMAND_RE.fullmatch(text)
    if match is None:
        return None
    count = match.group("legacy_count") or match.group("natural_count")
    return int(count) if count else 1, match.group("address") is not None


async def handle_delete_replied_message(
    update: Update,
) -> bool:
    message = update.effective_message
    if message is None or not message.text or message.text.strip().casefold() != "дэл":
        return False

    replied = message.reply_to_message
    if replied is None:
        return False

    target_deleted = False
    try:
        await replied.delete()
        target_deleted = True
    except TelegramError:
        logging.exception(
            "Не удалось удалить сообщение по команде «дэл» "
            "chat_id=%s message_id=%s.",
            message.chat_id,
            replied.message_id,
        )

    command_deleted = False
    try:
        await message.delete()
        command_deleted = True
    except TelegramError:
        logging.exception(
            "Не удалось удалить команду «дэл» chat_id=%s message_id=%s.",
            message.chat_id,
            message.message_id,
        )

    if not target_deleted:
        await message.reply_text(
            "Не удалось удалить сообщение. В группе боту нужны права "
            "администратора на удаление сообщений; также Telegram ограничивает "
            "удаление старых сообщений."
        )
    elif not command_deleted:
        await message.reply_text("Сообщение удалено, но команду удалить не удалось.")
    return True


async def handle_delete_bot_messages(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> bool:
    message = update.effective_message
    if message is None or not message.text:
        return False
    chat_delete_match = _DELETE_CHAT_COMMAND_RE.fullmatch(message.text)
    if chat_delete_match is not None:
        if message.chat.type == ChatType.PRIVATE:
            return False
        count = int(chat_delete_match.group(1))
        if count <= 0:
            await message.reply_text("Укажи положительное количество сообщений.")
            return True
        message_ids: list[int] = []
        user_client = personal_account.get_authorized_client()
        if user_client is not None:
            try:
                async for chat_message in user_client.iter_messages(
                    message.chat_id,
                    limit=count + 1,
                ):
                    if chat_message.id != message.message_id:
                        message_ids.append(chat_message.id)
                    if len(message_ids) >= count:
                        break
            except Exception:
                logging.exception(
                    "Не удалось получить последние сообщения Telegram для чата %s; "
                    "использую сохранённую историю.",
                    message.chat_id,
                )
                message_ids.clear()
        if len(message_ids) < count:
            try:
                stored_ids = await database.get_recent_chat_message_ids(
                    message.chat_id,
                    count,
                    exclude_message_id=message.message_id,
                )
                seen_ids = set(message_ids)
                message_ids.extend(
                    message_id for message_id in stored_ids if message_id not in seen_ids
                )
                message_ids = message_ids[:count]
            except Exception:
                if not message_ids:
                    logging.exception(
                        "Не удалось получить последние сообщения чата %s для удаления.",
                        message.chat_id,
                    )
                    await message.reply_text("Не удалось получить список сообщений. Попробуй позже.")
                    return True
                logging.exception(
                    "Не удалось дополнить список сообщений чата %s сохранённой историей.",
                    message.chat_id,
                )

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
                    "Не удалось удалить сообщение чата %s с ID %s.",
                    message.chat_id,
                    message_id,
                )
        try:
            await database.delete_chat_message_records(message.chat_id, deleted_ids)
        except Exception:
            logging.exception(
                "Не удалось обновить историю после удаления сообщений чата %s.",
                message.chat_id,
            )
            await message.reply_text(
                f"В Telegram удалено {len(deleted_ids)} из {len(message_ids)} сообщений, "
                "но историю чата обновить не удалось."
            )
            return True
        result = f"Удалено сообщений чата: {len(deleted_ids)}."
        if len(message_ids) < count:
            result += (
                f" Нашлось только {len(message_ids)}. Для доступа к полной истории "
                "нужно подключить личный Telegram-аккаунт Юбары."
            )
        if failed:
            result += f" Не удалось удалить: {failed}."
        await message.reply_text(result)
        return True

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
