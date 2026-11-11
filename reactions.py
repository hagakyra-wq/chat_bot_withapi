"""Выбор уместной реакции для сообщений в группе."""

import logging

from telegram import Message, ReactionTypeEmoji
from telegram.constants import ChatType
from telegram.error import TelegramError

from llm import choose_message_reaction


async def maybe_react(
    message: Message,
    history: list[tuple[int, str, int, str]],
) -> None:
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return

    recent = history[-4:]
    recent_ids = {row[0] for row in recent}
    lines = ["Последние сообщения чата:"]
    for message_id, sender_name, _sender_id, text in recent:
        lines.append(f"[{message_id}] {sender_name}: {text[:1200]}")

    replied = message.reply_to_message
    if replied is not None and replied.message_id not in recent_ids:
        sender = replied.from_user
        sender_name = (
            "Юбара"
            if sender is not None and sender.is_bot
            else (sender.first_name or sender.username if sender else "Участник")
        )
        replied_text = replied.text or replied.caption or "[сообщение без текста]"
        lines.append(
            "Сообщение, на которое отвечает текущее (может быть старше истории): "
            f"[{replied.message_id}] {sender_name}: {replied_text[:1200]}"
        )

    current_text = message.text or message.caption or "[сообщение без текста]"
    sender = message.from_user
    sender_name = sender.first_name or sender.username if sender else "Участник"
    lines.append(f"Текущее сообщение [{message.message_id}] {sender_name}: {current_text[:1200]}")
    reaction = await choose_message_reaction("\n".join(lines))
    if reaction is None:
        return
    try:
        await message.set_reaction(reaction=[ReactionTypeEmoji(reaction)])
    except TelegramError:
        logging.exception("Telegram не смог установить реакцию на сообщение %s.", message.message_id)
