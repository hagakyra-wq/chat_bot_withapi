"""Мини-игра с дуэлью на костях и накоплением монет."""

import asyncio
import logging
import re
from dataclasses import dataclass
from secrets import token_hex
from time import monotonic

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.constants import ChatType, MessageEntityType
from telegram.error import TelegramError
from telegram.ext import ContextTypes

import database
from bot_message_history import record_bot_message
from config import DICE_ANIMATION_DELAY_SECONDS

_DUEL_CHALLENGE_PATTERNS = (
    re.compile(r"\b(?:я\s+)?вызываю\b.{0,40}\bна\s+бат{1,2}л\b", re.IGNORECASE),
    re.compile(r"\b(?:бросаю|кидаю)\s+(?:тебе\s+)?вызов\b.{0,30}\bбат{1,2}л\b", re.IGNORECASE),
    re.compile(r"\b(?:давай|пойд[её]м|выходи)\s+на\s+бат{1,2}л\b", re.IGNORECASE),
    re.compile(r"\b(?:юбара|yubara)\b.{0,30}\b(?:бат{1,2}л|сразимся|побь[её]мся)\b", re.IGNORECASE),
)
_BATTLE_WORD_RE = re.compile(r"\bбат{1,2}л\w*\b", re.IGNORECASE)
_USERNAME_RE = re.compile(r"^@([a-z0-9_]{5,32})$", re.IGNORECASE)
_COIN_LEADERBOARD_REQUEST_RE = re.compile(
    r"\b(?:топ\s+(?:монет|по\s+монетам)|рейтинг\s+монет)\b",
    re.IGNORECASE,
)

_BALANCE_REQUEST_RE = re.compile(r"\bбаланс\b", re.IGNORECASE)
_PLAYER_DUEL_CHALLENGE_TTL_SECONDS = 120


@dataclass(frozen=True)
class PlayerDuelChallenge:
    chat_id: int
    challenger_id: int
    challenger_username: str | None
    challenger_name: str
    target_id: int
    target_username: str | None
    target_name: str
    created_at: float


def is_duel_challenge(text: str) -> bool:
    return any(pattern.search(text) is not None for pattern in _DUEL_CHALLENGE_PATTERNS)


def is_balance_request(text: str) -> bool:
    return _BALANCE_REQUEST_RE.search(text) is not None


def _format_coins(balance_cents: int) -> str:
    return f"{balance_cents // 100}.{balance_cents % 100:02d}"


def _pending_player_duels(context: ContextTypes.DEFAULT_TYPE) -> dict[str, PlayerDuelChallenge]:
    return context.application.bot_data.setdefault("pending_player_duels", {})


def _entity_text(text: str, offset: int, length: int) -> str:
    encoded = text.encode("utf-16-le")
    return encoded[offset * 2 : (offset + length) * 2].decode("utf-16-le")


async def _duel_target(
    message: Message,
    context: ContextTypes.DEFAULT_TYPE,
) -> tuple[int, str | None, str] | None:
    replied_user = message.reply_to_message.from_user if message.reply_to_message else None
    if replied_user is not None:
        return (
            replied_user.id,
            replied_user.username,
            replied_user.first_name or replied_user.username or str(replied_user.id),
        )

    text = message.text or ""
    mention_usernames: list[str] = []
    for entity in message.entities or []:
        if entity.type == MessageEntityType.TEXT_MENTION and entity.user is not None:
            mentioned_user = entity.user
            return (
                mentioned_user.id,
                mentioned_user.username,
                mentioned_user.first_name or mentioned_user.username or str(mentioned_user.id),
            )
        if entity.type == MessageEntityType.MENTION:
            match = _USERNAME_RE.fullmatch(_entity_text(text, entity.offset, entity.length))
            if match:
                mention_usernames.append(match.group(1).casefold())

    if not mention_usernames:
        return None
    bot_username = (context.bot.username or "").casefold()
    if bot_username and bot_username in mention_usernames:
        return context.bot.id, bot_username, "Юбара"
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return None

    participants = await database.get_group_users(message.chat_id)
    for username in mention_usernames:
        for participant in participants:
            if str(participant.get("username") or "").casefold() != username:
                continue
            user_id = int(participant["user_id"])
            return (
                user_id,
                str(participant["username"]) if participant.get("username") else None,
                str(participant.get("display_name") or f"@{username}"),
            )
    return None


async def _handle_player_duel(
    message: Message,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    username: str | None,
    display_name: str,
    target_id: int,
    target_username: str | None,
    target_name: str,
) -> bool:
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text("Батл между участниками можно начать только в групповом чате.")
        return True
    if target_id == user_id:
        await message.reply_text("Самого себя на батл не вызывают. Выбери соперника.")
        return True

    try:
        balances = {
            user_id: await database.get_duel_balance(user_id, username),
            target_id: await database.get_duel_balance(target_id, target_username),
        }
    except Exception:
        logging.exception("Не удалось подготовить баланс для дуэли двух участников.")
        await message.reply_text("Не удалось подготовить батл. Попробуй позже.")
        return True
    if balances[user_id] < 100 or balances[target_id] < 100:
        await message.reply_text("Для батла каждому участнику нужна хотя бы 1 монета.")
        return True

    now = monotonic()
    pending = _pending_player_duels(context)
    for challenge_id, pending_challenge in tuple(pending.items()):
        if now - pending_challenge.created_at > _PLAYER_DUEL_CHALLENGE_TTL_SECONDS:
            del pending[challenge_id]

    challenge_id = token_hex(8)
    challenge = PlayerDuelChallenge(
        chat_id=message.chat_id,
        challenger_id=user_id,
        challenger_username=username,
        challenger_name=display_name,
        target_id=target_id,
        target_username=target_username,
        target_name=target_name,
        created_at=now,
    )
    keyboard = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("Принять батл", callback_data=f"pvp:accept:{challenge_id}"),
            InlineKeyboardButton("Отказаться", callback_data=f"pvp:decline:{challenge_id}"),
        ]]
    )
    try:
        await database.save_message(
            message.chat_id,
            user_id,
            message.message_id,
            display_name,
            message.text or "Батл",
        )
        started = await message.reply_text(
            f"{display_name} вызывает {target_name} на батл. Ставка — по 1 монете. "
            "Вызов действует 2 минуты.",
            reply_markup=keyboard,
        )
        pending[challenge_id] = challenge
        await record_bot_message(started)
    except Exception:
        logging.exception("Не удалось отправить вызов на батл между участниками.")
        await message.reply_text("Не удалось отправить вызов. Попробуй позже.")
        return True
    return True


async def handle_player_duel_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    user = update.effective_user
    if query is None or user is None:
        return

    match = re.fullmatch(r"pvp:(accept|decline):([a-f0-9]{16})", query.data or "")
    if match is None:
        await query.answer()
        return

    action, challenge_id = match.groups()
    pending = _pending_player_duels(context)
    challenge = pending.get(challenge_id)
    if challenge is None or monotonic() - challenge.created_at > _PLAYER_DUEL_CHALLENGE_TTL_SECONDS:
        pending.pop(challenge_id, None)
        await query.answer("Вызов уже истёк или был отменён.", show_alert=True)
        if query.message:
            await query.edit_message_reply_markup(reply_markup=None)
        return
    if user.id != challenge.target_id:
        await query.answer("Этот батл адресован другому участнику.", show_alert=True)
        return

    del pending[challenge_id]
    await query.answer()
    if action == "decline":
        await query.edit_message_text("Вызов отклонён.")
        return

    await query.edit_message_text("Вызов принят. Бросаем кости!")
    await _run_player_duel(context, challenge)


async def _run_player_duel(
    context: ContextTypes.DEFAULT_TYPE,
    challenge: PlayerDuelChallenge,
) -> None:
    user_id = challenge.challenger_id
    username = challenge.challenger_username
    display_name = challenge.challenger_name
    target_id = challenge.target_id
    target_username = challenge.target_username
    target_name = challenge.target_name

    try:
        balances = {
            user_id: await database.get_duel_balance(user_id, username),
            target_id: await database.get_duel_balance(target_id, target_username),
        }
    except Exception:
        logging.exception("Не удалось обновить баланс перед началом принятого батла.")
        await context.bot.send_message(
            chat_id=challenge.chat_id,
            text="Не удалось подготовить батл. Попробуй позже.",
        )
        return
    if balances[user_id] < 100 or balances[target_id] < 100:
        await context.bot.send_message(
            chat_id=challenge.chat_id,
            text="У одного из участников больше нет монеты для ставки. Батл отменён.",
        )
        return

    try:
        first_announcement = await context.bot.send_message(
            chat_id=challenge.chat_id,
            text=f"Бросок за {display_name}.",
        )
        await record_bot_message(first_announcement)
        first_roll = await context.bot.send_dice(chat_id=challenge.chat_id, emoji="🎲")
        await record_bot_message(first_roll, f"Бросок {display_name} в батле")
        await asyncio.sleep(DICE_ANIMATION_DELAY_SECONDS)
        second_announcement = await context.bot.send_message(
            chat_id=challenge.chat_id,
            text=f"Теперь бросок за {target_name}.",
        )
        await record_bot_message(second_announcement)
        second_roll = await context.bot.send_dice(chat_id=challenge.chat_id, emoji="🎲")
        await record_bot_message(second_roll, f"Бросок {target_name} в батле")
        await asyncio.sleep(DICE_ANIMATION_DELAY_SECONDS)
    except TelegramError:
        logging.exception("Не удалось провести батл между участниками чата %s.", challenge.chat_id)
        await context.bot.send_message(
            chat_id=challenge.chat_id,
            text="Кости не легли на стол. Батл отменён без изменения монет.",
        )
        return
    except Exception:
        logging.exception("Не удалось подготовить батл между участниками.")
        await context.bot.send_message(
            chat_id=challenge.chat_id,
            text="Не удалось подготовить батл. Попробуй позже.",
        )
        return

    first_value = first_roll.dice.value if first_roll.dice else 0
    second_value = second_roll.dice.value if second_roll.dice else 0
    if not 1 <= first_value <= 6 or not 1 <= second_value <= 6:
        await context.bot.send_message(
            chat_id=challenge.chat_id,
            text="Кости показали невозможное. Батл отменён без изменения монет.",
        )
        return
    if first_value == second_value:
        result = (
            f"Ничья: {display_name} и {target_name} выбросили по {first_value}. "
            "Монеты остаются при своих."
        )
    else:
        first_won = first_value > second_value
        winner_id, winner_username = (
            (user_id, username) if first_won else (target_id, target_username)
        )
        loser_id, loser_username = (
            (target_id, target_username) if first_won else (user_id, username)
        )
        winner_name = display_name if first_won else target_name
        loser_name = target_name if first_won else display_name
        try:
            winner_balance, loser_balance = await database.settle_player_duel(
                winner_id,
                winner_username,
                loser_id,
                loser_username,
            )
        except database.InsufficientDuelBalanceError:
            result = (
                f"{display_name}: {first_value}, {target_name}: {second_value}. "
                "Кто-то успел потратить монету; ставка не переведена."
            )
        except Exception:
            logging.exception("Не удалось рассчитаться после дуэли участников.")
            result = (
                "🏁 БАТЛ ЗАВЕРШЁН\n"
                f"{display_name}: {first_value}, {target_name}: {second_value}. "
                "Победитель определён, но баланс не удалось обновить."
            )
        else:
            first_balance = winner_balance if first_won else loser_balance
            second_balance = loser_balance if first_won else winner_balance
            result = (
                "🏁 БАТЛ ЗАВЕРШЁН\n"
                "━━━━━━━━━━━━━━\n"
                f"🎲 {display_name}: {first_value}\n"
                f"🎲 {target_name}: {second_value}\n\n"
                f"🏆 Победитель: {winner_name}\n"
                f"💰 Награда: +1 монета от {loser_name}\n"
                f"💳 Баланс: {display_name} — {_format_coins(first_balance)} | "
                f"{target_name} — {_format_coins(second_balance)}"
            )
    try:
        sent = await context.bot.send_message(chat_id=challenge.chat_id, text=result)
        await record_bot_message(sent, result)
    except TelegramError:
        logging.exception("Не удалось отправить итог батла между участниками.")


async def handle_duel_challenge(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> bool:
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None or not message.text:
        return False

    challenge_text = is_duel_challenge(message.text)
    has_battle_word = _BATTLE_WORD_RE.search(message.text) is not None
    if not challenge_text and not has_battle_word:
        return False
    standalone_battle = message.text.strip().casefold().rstrip("!?.;,:") in {
        "батл",
        "баттл",
    }
    try:
        target = await _duel_target(message, context)
    except Exception:
        logging.exception("Не удалось определить соперника дуэли.")
        target = None
    targeted_battle = target is not None and _BATTLE_WORD_RE.search(message.text) is not None
    if not challenge_text and not targeted_battle and not standalone_battle:
        return False

    if target is not None and target[0] != context.bot.id:
        return await _handle_player_duel(
            message,
            context,
            user.id,
            user.username,
            user.first_name or user.username or str(user.id),
            *target,
        )

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
        result = (
            "🏁 БАТЛ ЗАВЕРШЁН\n"
            "━━━━━━━━━━━━━━\n"
            f"🎲 Ты: {user_value}\n"
            f"🎲 Юбара: {yubara_value}\n\n"
            "🏆 Победа за тобой!\n"
            "💰 Награда: +2 монеты\n"
            f"💳 Твой баланс: {_format_coins(balance)} монет.\n\n"
            "У меня ещё несколько миллионов: потеря двух монет — капля в море. "
            "Считай, я просто поддалась."
        )
    elif user_value < yubara_value:
        loss_message = (
            "У тебя уже ноль, ниже не опускаю."
            if starting_balance == 0
            else "С тебя −1 монета."
        )
        result = (
            "🏁 БАТЛ ЗАВЕРШЁН\n"
            "━━━━━━━━━━━━━━\n"
            f"🎲 Ты: {user_value}\n"
            f"🎲 Юбара: {yubara_value}\n\n"
            "👑 Этот раунд за Юбарой.\n"
            f"💰 {loss_message}\n"
            f"💳 Твой баланс: {_format_coins(balance)} монет."
        )
    else:
        result = (
            "🏁 БАТЛ ЗАВЕРШЁН\n"
            "━━━━━━━━━━━━━━\n"
            f"🎲 Ты: {user_value}\n"
            f"🎲 Юбара: {yubara_value}\n\n"
            "🤝 Ничья. Монеты остаются при своих.\n"
            f"💳 Твой баланс: {_format_coins(balance)} монет."
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


async def handle_coin_leaderboard_request(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> bool:
    message = update.effective_message
    user = update.effective_user
    if (
        message is None
        or user is None
        or not message.text
        or _COIN_LEADERBOARD_REQUEST_RE.search(message.text) is None
    ):
        return False

    try:
        players = await database.get_duel_leaderboard(limit=10)
    except Exception:
        logging.exception("Не удалось загрузить топ игроков по монетам.")
        await message.reply_text("Не удалось загрузить рейтинг. Попробуй позже.")
        return True

    lines = ["🏆 ТОП-10 ПО МОНЕТАМ", "━━━━━━━━━━━━━━"]
    medals = ("🥇", "🥈", "🥉")
    for rank, player in enumerate(players, start=1):
        medal = medals[rank - 1] if rank <= len(medals) else f"{rank}."
        name = str(player["display_name"])
        balance = _format_coins(int(player["balance_cents"]))
        lines.append(f"{medal} {name} — {balance} 🪙")
    if not players:
        lines.append("Пока нет игроков. Запусти батл, чтобы попасть в рейтинг.")

    result = "\n".join(lines)
    sent = await message.reply_text(result)
    await record_bot_message(sent, result)
    return True
