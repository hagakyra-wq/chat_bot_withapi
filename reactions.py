"""Реакции на сообщения, на которые Юбара ответила."""

import logging
import random

from telegram import Message, ReactionTypeEmoji
from telegram.error import TelegramError

from config import AVAILABLE_REACTIONS, REACTION_PROBABILITY


async def maybe_react(message: Message) -> None:
    if random.random() >= REACTION_PROBABILITY:
        return
    try:
        await message.set_reaction(
            reaction=[ReactionTypeEmoji(random.choice(AVAILABLE_REACTIONS))]
        )
    except TelegramError:
        logging.exception("Telegram не смог установить реакцию на сообщение Юбары.")
