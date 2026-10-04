"""Мини-игра с дуэлью на костях и накоплением монет."""

import asyncio
import logging
import re

from telegram import Update
from telegram.error import TelegramError
from telegram.ext import ContextTypes

import database
from bot_message_history import record_bot_message
from config import DICE_ANIMATION_DELAY_SECONDS

_DUEL_CHALLENGE_PATTERNS = (
    re.compile(r"\b(?:я\s+)?вызываю\b.{0,40}\bна\s+дуэль\b", re.IGNORECASE),
    re.compile(r"\b(?:бросаю|кидаю)\s+(?:тебе\s+)?вызов\b.{0,30}\bдуэль\b", re.IGNORECASE),
    re.compile(r"\b(?:давай|пойд[её]м|выходи)\s+на\s+дуэль\b", re.IGNORECASE),
    re.compile(r"\b(?:юбара|yubara)\b.{0,30}\b(?:дуэль|сразимся|побь[её]мся)\b", re.IGNORECASE),
)

_BALANCE_REQUEST_RE = re.compile(r"\bбаланс\b", re.IGNORECASE)


def is_duel_challenge(text: str) -> bool:
    return any(pattern.search(text) is not None for pattern in _DUEL_CHALLENGE_PATTERNS)


def is_balance_request(text: str) -> bool:
    return _BALANCE_REQUEST_RE.search(text) is not None


def _format_coins(balance_cents: int) -> str:
    return f"{balance_cents // 100}.{balance_cents % 100:02d}"


async def handle_duel_challenge(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> bool:
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None or not message.text or not is_duel_challenge(message.text):
        return False

    try:
        starting_balance = await database.get_duel_balance(user.id, user.username)
        await database.save_message(
            message.chat_id,
            user.id,
            message.message_id,
            user.first_name or user.username or str(user.id),
            message.text,
        )
    except Exception:
        logging.exception("Не удалось подготовить игровой баланс пользователя %s.", user.id)
        await message.reply_text("Не удалось подготовить дуэль. Попробуй позже.")
        return True

    try:
        accepted = await message.reply_text("Вызов принят. Сначала бросок за тебя.")
        await record_bot_message(accepted)
        user_roll = await context.bot.send_dice(chat_id=message.chat_id, emoji="🎲")
        await record_bot_message(user_roll, "Бросок кубика участника дуэли")
        await asyncio.sleep(DICE_ANIMATION_DELAY_SECONDS)
        announcement = await context.bot.send_message(
            chat_id=message.chat_id,
            text="Теперь мой бросок.",
        )
        await record_bot_message(announcement)
        yubara_roll = await context.bot.send_dice(chat_id=message.chat_id, emoji="🎲")
        await record_bot_message(yubara_roll, "Бросок кубика Юбары")
        await asyncio.sleep(DICE_ANIMATION_DELAY_SECONDS)
    except TelegramError:
        logging.exception("Не удалось бросить кости в дуэли с пользователем %s.", user.id)
        await message.reply_text("Кости не легли на стол. Дуэль пока откладывается.")
        return True

    user_value = user_roll.dice.value if user_roll.dice else 0
    yubara_value = yubara_roll.dice.value if yubara_roll.dice else 0
    if not 1 <= user_value <= 6 or not 1 <= yubara_value <= 6:
        logging.error(
            "Telegram вернул некорректные кости в дуэли: user=%s yubara=%s.",
            user_value,
            yubara_value,
        )
        await message.reply_text("Кости показали невозможное. Дуэль отменена без изменения монет.")
        return True

    try:
        if user_value > yubara_value:
            delta_cents = 200
        elif user_value < yubara_value:
            delta_cents = -100
        else:
            delta_cents = 0
        balance = await database.apply_duel_result(user.id, user.username, delta_cents)
    except Exception:
        logging.exception("Не удалось записать результат дуэли пользователя %s.", user.id)
        await message.reply_text(
            f"Кости брошены: тебе {user_value}, мне {yubara_value}. "
            "Результат есть, но баланс пока не удалось обновить."
        )
        return True

    if user_value > yubara_value:
        outcome = "Ты победил. Забирай +2 монеты; сегодня королева щедра."
    elif user_value < yubara_value:
        outcome = (
            "Я победила, как и следовало ожидать. Баланс уже нулевой, ниже не опускаю."
            if starting_balance == 0
            else "Я победила, как и следовало ожидать. С тебя −1 монета."
        )
    else:
        outcome = "Ничья. Монеты остаются при своих — можно бросить кости ещё раз."

    result = (
        f"Тебе выпало {user_value}, мне — {yubara_value}. {outcome}\n"
        f"Твой баланс: {_format_coins(balance)} монет."
    )
    if user_value > yubara_value:
        result += (
            "\nУ меня ещё несколько миллионов: потеря двух монет — капля в море. "
            "Считай, я просто поддалась."
        )
    try:
        sent = await message.reply_text(result)
        await record_bot_message(sent, result)
    except TelegramError:
        logging.exception("Не удалось отправить итог дуэли пользователю %s.", user.id)
    except Exception:
        logging.exception("Не удалось сохранить итог дуэли в истории.")
    return True


async def handle_balance_request(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> bool:
    del context
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None or not message.text or not is_balance_request(message.text):
        return False
    try:
        balance = await database.get_duel_balance(user.id, user.username)
    except Exception:
        logging.exception("Не удалось получить баланс игрока %s.", user.id)
        await message.reply_text("Не удалось проверить баланс. Попробуй позже.")
        return True
    sent = await message.reply_text(f"У тебя {_format_coins(balance)} монет.")
    await record_bot_message(sent)
    return True
