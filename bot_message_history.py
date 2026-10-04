"""Регистрация сообщений, отправленных Юбарой, для последующего удаления."""

import logging

from telegram import Message

import database


async def record_bot_message(message: Message, text: str | None = None) -> None:
    try:
        await database.save_message(
            message.chat_id,
            message.from_user.id if message.from_user else 0,
            message.message_id,
            "Юбара",
            text or message.text or message.caption or "Сообщение Юбары",
        )
    except Exception:
        logging.exception(
            "Не удалось сохранить сообщение Юбары chat_id=%s message_id=%s для команды удаления.",
            message.chat_id,
            message.message_id,
        )
