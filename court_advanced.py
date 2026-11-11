"""Расширенный суд Юбары: драматизация, таймери и ролевая логика."""

import random
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.constants import ChatType
from telegram.ext import ContextTypes

import database

COURT_LAWS = (
    "1. Доказательство — это не только слова, но и факты в переписке.",
    "2. Если ответчик молчит, суд принимает историю чата как основной аргумент защиты.",
    "3. Чай, мемы и поздние ночи могут смягчить приговор, но не отменить логику.",
    "4. В суде Юбары важнее последовательность событий, чем громкость обвинения.",
    "5. Истинный виновник — тот, кто не смог объяснить свои действия перед трибуналом.",
)

_COURT_RE = re.compile(r"\bсуд\b|\bтрибунал\b|\bсудеб\w*\b", re.IGNORECASE)


def is_court_request(text: str) -> bool:
    normalized = " ".join(text.casefold().split())
    return bool(_COURT_RE.search(normalized))


def format_laws() -> str:
    return "\n".join(COURT_LAWS)


async def _user_name(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> str:
    try:
        user = await context.bot.get_chat(user_id)
        return user.first_name or user.username or f"user_{user_id}"
    except Exception:
        return f"user_{user_id}"


async def start_advanced_court(message: Message, context: ContextTypes.DEFAULT_TYPE) -> None:
    if message.reply_to_message is None:
        await message.reply_text(
            "Чтобы открыть суд, ответь на сообщение. Истец — тот, кто пишет 'суд', ответчик — тот, на кого ответили."
        )
        return

    plaintiff = message.from_user
    defendant = message.reply_to_message.from_user
    if plaintiff is None or defendant is None:
        await message.reply_text("Не удалось определить стороны процесса.")
        return

    case_id = f"court-{message.chat_id}-{message.message_id}-{random.randint(1000, 9999)}"
    case = {
        "group_id": message.chat_id,
        "plaintiff_id": plaintiff.id,
        "defendant_id": defendant.id,
        "plaintiff_story": None,
        "defendant_story": None,
        "status": "waiting_defendant",
        "created_at": datetime.now(timezone.utc),
        "expires_at": datetime.now(timezone.utc) + timedelta(minutes=15),
        "judge_text": None,
    }
    context.bot_data.setdefault("advanced_court_cases", {})[case_id] = case

    await context.bot.send_message(
        chat_id=defendant.id,
        text=(
            "⚖️ Тебя вызвали на суд Юбары.\n\n"
            "Ты можешь принять участие и объяснить свои действия. "
            "Если ответ не придёт до таймера, суд будет опираться на историю чата."
        ),
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "✅ Принять участие",
                        callback_data=f"court_adv:accept:{case_id}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "❌ Пропустить",
                        callback_data=f"court_adv:skip:{case_id}",
                    )
                ],
            ]
        ),
    )

    await context.bot.send_message(
        chat_id=plaintiff.id,
        text=(
            "🧾 Истец зарегистрирован.\n\n"
            "Напиши в ЛС свою версию событий."
        ),
    )

    await message.reply_text(
        "⚖️ Дело открыто. Судья Юбара ждёт позиции обеих сторон. Таймер: 15 минут."
    )


async def advanced_court_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    await query.answer()

    data = query.data or ""
    if not data.startswith("court_adv:"):
        return

    _, action, case_id = data.split(":", 2)
    cases = context.bot_data.setdefault("advanced_court_cases", {})
    case = cases.get(case_id)
    if case is None:
        await query.edit_message_text("Дело уже закрыто или не найдено.")
        return

    if action == "accept":
        if query.from_user and query.from_user.id == case["defendant_id"]:
            case["status"] = "defendant_ready"
            await query.edit_message_text(
                "✅ Ответчик подтвердил участие. Напиши краткую защиту в ЛС."
            )
        return

    if action == "skip":
        if query.from_user and query.from_user.id == case["defendant_id"]:
            case["status"] = "declined"
            await query.edit_message_text("❌ Ответчик отказался от участия. Суд будет рассматривать историю чата.")
            await _finalize_advanced_court(case, context)
        return


async def record_advanced_court_story(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    message = update.effective_message
    if message is None or message.chat.type == ChatType.GROUP:
        return False

    text = (message.text or message.caption or "").strip()
    if not text and not any((message.sticker, message.photo, message.video, message.voice, message.audio, message.document, message.animation)):
        return False
    if not text:
        text = "[медиа]"

    user = message.from_user
    if user is None:
        return False

    cases = context.bot_data.setdefault("advanced_court_cases", {})
    for case in cases.values():
        if case["status"] == "closed":
            continue
        if user.id == case["plaintiff_id"] and case["plaintiff_story"] is None:
            case["plaintiff_story"] = text
            await message.reply_text("🧾 Истец передал позицию. Ожидаем защиту ответчика.")
            if case["defendant_story"] is not None or case["status"] == "declined":
                await _finalize_advanced_court(case, context)
            return True

        if user.id == case["defendant_id"] and case["defendant_story"] is None:
            case["defendant_story"] = text
            await message.reply_text("🛡️ Защита получена. Судьи Юбары готовят вердикт.")
            if case["plaintiff_story"] is not None:
                await _finalize_advanced_court(case, context)
            return True

    return False


async def _finalize_advanced_court(case: dict[str, Any], context: ContextTypes.DEFAULT_TYPE) -> None:
    if case["status"] == "closed":
        return

    plaintiff_story = case.get("plaintiff_story") or "Истец не успел подготовить позицию."
    defendant_story = case.get("defendant_story") or "Ответчик не ответил. Суд использует историю чата."

    if case.get("defendant_story") is None:
        try:
            history_rows = await database.get_summary_history_rows(case["group_id"], 180)
            history_snippet = "\n".join(
                f"- {row.get('sender_name')}: {row.get('text')}"
                for row in history_rows[-6:]
                if row.get("text")
            )
        except Exception:
            history_snippet = "История чата недоступна."
        defendant_story = (
            "Ответчик молчал. Защита восстановлена по истории чата:\n"
            f"{history_snippet}"
        )

    plaintiff_name = await _user_name(context, case["plaintiff_id"])
    defendant_name = await _user_name(context, case["defendant_id"])
    verdict = (
        "⚖️ Вердикт королевского трибунала Юбары\n\n"
        f"Истец: {plaintiff_name}\n"
        f"Ответчик: {defendant_name}\n\n"
        f"Позиция истца:\n{plaintiff_story}\n\n"
        f"Позиция ответчика:\n{defendant_story}\n\n"
        f"Правовой принцип: {random.choice(COURT_LAWS)}\n\n"
        f"Решение: {random.choice(['виновен', 'частично виновен', 'виновен, но со смягчением', 'невиновен в части'])}.\n\n"
        "Суд завершён. Резолюция записана в королевский архив."
    )

    await context.bot.send_message(chat_id=case["group_id"], text=verdict)
    case["judge_text"] = verdict
    case["status"] = "closed"


async def court_advanced_help(message: Message) -> None:
    await message.reply_text(
        "⚖️ Суд Юбары\n\n"
        "1. Истец вызывает суд в ответ на сообщение.\n"
        "2. Ответчик получает приглашение в ЛС.\n"
        "3. Обе стороны пишут свои версии.\n"
        "4. Если ответчик молчит, суд опирается на историю чата.\n"
        "5. Вердикт выходит в группу."
    )
