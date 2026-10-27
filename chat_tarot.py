"""Telegram command for unrestricted chat tarot readings."""

import logging

from telegram import Update
from telegram.ext import ContextTypes

import llm
from tarot_deck import draw_spread


async def tarot_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    message = update.effective_message
    if message is None:
        return

    question = " ".join(context.args or []).strip() or "Общий расклад"
    if len(question) > 500:
        await message.reply_text("Сократи вопрос до 500 символов.")
        return

    cards = draw_spread()
    card_lines = [
        f"{card['position']}: {card['symbol']} {card['name']}"
        f"{' (перевёрнутая)' if card['reversed'] else ''}"
        for card in cards
    ]
    cards_text = "\n".join(card_lines)
    try:
        interpretation = await llm.generate_tarot_interpretation(question, cards)
    except Exception:
        logging.exception("Не удалось подготовить расклад Таро.")
        await message.reply_text(
            "Не удалось истолковать расклад сейчас. Попробуй выполнить /tarot ещё раз."
        )
        return

    await message.reply_text(
        f"🔮 Расклад Таро\nВопрос: {question}\n\n"
        f"{cards_text}\n\n{interpretation}\n\n"
        "Таро — развлечение и повод для размышления, а не предсказание."
    )
