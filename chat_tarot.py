"""Telegram command for unrestricted chat tarot readings."""

import logging
import re

from telegram import Message, Update
from telegram.error import TelegramError
from telegram.ext import ContextTypes

import llm
from tarot_deck import draw_spread
from tarot_images import create_tarot_collage

_TAROT_TRIGGER_RE = re.compile(
    r"^\s*(?:(?:юбара|yubara|@\w+)\s*[,!:—-]?\s*)?"
    r"таро\b(?:\s*[,.:!?—-]\s*|\s+)?(?P<question>.*)$",
    re.IGNORECASE | re.DOTALL,
)


def extract_tarot_trigger_question(text: str) -> str | None:
    match = _TAROT_TRIGGER_RE.fullmatch(text)
    if match is None:
        return None
    return match.group("question").strip()


async def send_tarot_reading(message: Message, question: str) -> None:
    question = question.strip() or "Общий расклад"
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

    reading = (
        f"🔮 Расклад Таро\nВопрос: {question}\n\n"
        f"{cards_text}\n\n{interpretation}\n\n"
        "Таро — развлечение и повод для размышления, а не предсказание."
    )
    caption = f"🔮 Расклад Таро\nВопрос: {question}\n\n{cards_text}"
    try:
        collage = create_tarot_collage(cards)
    except OSError:
        logging.exception("Не удалось создать коллаж для расклада Таро.")
        await message.reply_text(
            f"{reading}\n\nНе удалось создать коллаж карт."
        )
        return

    try:
        await message.reply_photo(photo=collage, caption=caption)
    except TelegramError:
        logging.exception("Не удалось отправить коллаж для расклада Таро.")
        await message.reply_text(
            f"{reading}\n\nНе удалось отправить коллаж карт."
        )
        return

    await message.reply_text(reading)


async def tarot_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    message = update.effective_message
    if message is None:
        return

    await send_tarot_reading(message, " ".join(context.args or []))
