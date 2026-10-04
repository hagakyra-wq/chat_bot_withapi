"""Мини-игра с дуэлью на костях и накоплением монет."""

import logging
import re

from telegram import Update
from telegram.error import TelegramError
from telegram.ext import ContextTypes

import database

_DUEL_CHALLENGE_RE = re.compile(
    r"\b(?:(?:юбара|yubara)[\s,!.?—-]*)?(?:я\s+)?вызываю\s+(?:тебя\s+)?на\s+дуэль\b",
    re.IGNORECASE,
)
def is_duel_challenge(text: str) -> bool:
    return _DUEL_CHALLENGE_RE.search(text) is not None


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
        await message.reply_text("Вызов принят. Сначала бросок за тебя.")
        user_roll = await context.bot.send_dice(chat_id=message.chat_id, emoji="🎲")
        await context.bot.send_message(chat_id=message.chat_id, text="Теперь мой бросок.")
        yubara_roll = await context.bot.send_dice(chat_id=message.chat_id, emoji="🎲")
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
        await database.save_message(
            message.chat_id,
            context.bot.id,
            sent.message_id,
            "Юбара",
            result,
        )
    except TelegramError:
        logging.exception("Не удалось отправить итог дуэли пользователю %s.", user.id)
    except Exception:
        logging.exception("Не удалось сохранить итог дуэли в истории.")
    return True
