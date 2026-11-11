"""Мини-правовая система для суда Юбары: судья, два адвоката и кодекс."""

import random
import re
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.ext import ContextTypes

import database

COURT_LAWS = (
    "1. Чай в суде — не доказательство, но сильное смягчающее обстоятельство.",
    "2. Суд опирается в первую очередь на историю чата и на то, что реально было сказано, а не на эмоции в момент спора.",
    "3. Если спор подтверждён двумя сторонами и одной важной историей переписки, приоритет отдаётся фактам, а не красноречию.",
    "4. Знаменитое правило: 'я не виновен в том, что меня украли в 3:17'.",
    "5. Если участник объяснил причину, суд учитывает его намерение, а не только результат.",
    "6. Слишком много мемов без согласия — это не преступление, а повод для раскаяния и смягчения наказания.",
    "7. Внутренний кодекс Юбары не обязан совпадать с земным правом — и поэтому особенно справедлив.",
    "8. В случае молчания ответчика адвокат имеет право опереться на переписку, но только на то, что реально видно в истории сообщений.",
    "9. Нельзя скрывать важные факты, если ты сам вызвал суд и приставил к себе адвоката.",
    "10. Судья Юбары может назначить 'королевскую отсрочку', но не может отменить очевидную логику переписки.",
)

_COURT_RE = re.compile(r"\bсуд\b|\bтрибунал\b|\bсудеб\w*\b", re.IGNORECASE)
_LAWS_RE = re.compile(
    r"\b(кодекс|законы|закон|сводка\s+законов|права|правила)\b",
    re.IGNORECASE,
)


def is_court_request(text: str) -> bool:
    normalized = " ".join(text.casefold().split())
    return bool(_COURT_RE.search(normalized))


def is_laws_request(text: str) -> bool:
    normalized = " ".join(text.casefold().split())
    return bool(_LAWS_RE.search(normalized))


def format_laws() -> str:
    return "\n".join(COURT_LAWS)


async def send_laws_summary(message: Message) -> None:
    await message.reply_text(
        "📜 Кодекс Юбари\n\n"
        + format_laws()
        + "\n\nЭти нормы не копируют чужое право. Это свод правил, который работает здесь и сейчас."
    )


async def start_court_case(message: Message, context: ContextTypes.DEFAULT_TYPE) -> None:
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
        "judge_text": None,
        "created_at": message.date.isoformat() if message.date else None,
    }

    bot_data = context.bot_data.setdefault("court_cases", {})
    bot_data[case_id] = case

    invite_text = (
        "⚖️ Тебя вызвали в суд Юбары как ответчика.\n\n"
        "Ты можешь принять участие и изложить свою версию событий в личном чате.\n"
        "Если не ответишь, суд будет опираться на историю чата и на то, что удалось восстановить." 
        "\n\nЭто не государственный суд, а внутренний королевский трибунал Юбары."
    )
    invite_keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Принять участие",
                    callback_data=f"court:accept:{case_id}:{defendant.id}",
                )
            ],
            [
                InlineKeyboardButton(
                    "❌ Отказаться",
                    callback_data=f"court:decline:{case_id}:{defendant.id}",
                )
            ],
        ]
    )

    try:
        await context.bot.send_message(
            chat_id=defendant.id,
            text=invite_text,
            reply_markup=invite_keyboard,
        )
    except Exception:
        await message.reply_text(
            "⚠️ Ответчик не получил приглашение в ЛС, но дело зарегистрировано. "
            "В таком случае суд будет опираться на историю чата и известные факты."
        )

    await context.bot.send_message(
        chat_id=plaintiff.id,
        text=(
            "🧾 Истец зарегистрирован.\n\n"
            "Напиши в ЛС короткую версию претензии в 3–5 предложениях.\n"
            "Если ответчик молчит, адвокат Юбары восстановит позицию по истории переписки."
        ),
    )

    await message.reply_text(
        f"⚖️ Суд начат. Истец: {plaintiff.first_name or 'Неизвестно'}, ответчик: {defendant.first_name or 'Неизвестно'}.\n"
        "Приглашения отправлены в ЛС."
    )


async def court_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    await query.answer()

    data = query.data or ""
    if not data.startswith("court:"):
        return

    _, action, case_id, *_ = data.split(":")
    cases = context.bot_data.setdefault("court_cases", {})
    case = cases.get(case_id)
    if case is None:
        await query.edit_message_text("Судебное дело уже закрыто или не найдено.")
        return

    user_id = query.from_user.id if query.from_user else None
    if action == "accept":
        if user_id != case["defendant_id"]:
            await query.edit_message_text("Это приглашение только для ответчика.")
            return
        case["status"] = "defendant_ready"
        await query.edit_message_text(
            "✅ Участие принято. Напиши своей адвокату короткую историю произошедшего в 3–5 предложениях."
        )
        return

    if action == "decline":
        case["status"] = "declined"
        await query.edit_message_text("❌ Участие в суде отклонено. Дело может быть признано без ответа ответчика.")
        if case.get("plaintiff_story"):
            await _finalize_verdict(case, context)
        return


async def record_court_story(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    message = update.effective_message
    if message is None or message.text is None:
        return False

    user = message.from_user
    if user is None:
        return False

    cases = context.bot_data.setdefault("court_cases", {})
    for case_id, case in list(cases.items()):
        if user.id not in {case["plaintiff_id"], case["defendant_id"]}:
            continue
        if case.get("status") == "declined":
            continue

        if user.id == case["plaintiff_id"] and case.get("plaintiff_story") is None:
            case["plaintiff_story"] = message.text.strip()
            await message.reply_text(
                "🧾 Истец передал историю. Адвокат Юбары записал позицию и ждёт ответчика."
            )
            if case.get("defendant_story") is not None:
                await _finalize_verdict(case, context)
            return True

        if user.id == case["defendant_id"] and case.get("defendant_story") is None:
            case["defendant_story"] = message.text.strip()
            await message.reply_text(
                "🛡️ Защита принята. Юбара готовит вердикт после сверки сторон."
            )
            if case.get("plaintiff_story") is not None:
                await _finalize_verdict(case, context)
            return True

    return False


async def _finalize_verdict(case: dict[str, Any], context: ContextTypes.DEFAULT_TYPE) -> None:
    plaintiff_story = case.get("plaintiff_story") or "Истец не предоставил аргументы."
    defendant_story = case.get("defendant_story") or "Ответчик не ответил и не смог защититься."

    plaintiff_name = await _user_name(context, case["plaintiff_id"])
    defendant_name = await _user_name(context, case["defendant_id"])

    if case.get("defendant_story") is None:
        try:
            history_rows = await database.get_summary_history_rows(case["group_id"], 180)
            history_snippet = "\n".join(
                f"- {row.get('sender_name')}: {row.get('text')}"
                for row in history_rows[-6:]
                if row.get("text")
            )
        except Exception:
            history_snippet = "История чата недоступна, но в деле есть пробелы."
        defense_note = (
            "Ответчик не явился к суду. Адвокат опирался на историю чата и восстановил защиту по последним сообщениям:\n"
            f"{history_snippet}"
        )
        defendant_story = (
            "Ответчик молчал. Защита восстановлена по истории чата, чтобы не оставить обвинение без ответа.\n"
            f"{history_snippet}"
        )
        verdict_result = random.choice([
            "виновен с учетом смягчающих обстоятельств",
            "частично виновен",
            "виновен, но приговор смягчён",
        ])
    else:
        defense_note = (
            "Ответчик представил версию событий. Адвокат усилил объяснение и подчеркнул смягчающие обстоятельства."
        )
        verdict_result = random.choice([
            "виновен",
            "частично виновен",
            "невиновен в части",
            "виновен, но с компенсацией в виде чайной реабилитации",
        ])

    verdict = (
        "⚖️ Вердикт суда Юбары\n\n"
        f"Истец: {plaintiff_name}\n"
        f"Ответчик: {defendant_name}\n\n"
        f"История истца:\n{plaintiff_story}\n\n"
        f"История ответчика:\n{defendant_story}\n\n"
        f"Правовая сводка: {random.choice(COURT_LAWS)}\n\n"
        f"Решение: {verdict_result}.\n"
        + defense_note
        + "\n\nСуд постановил: дело закрыто, но память о нём останется в архиве королевского трибунала."
    )

    try:
        await context.bot.send_message(chat_id=case["group_id"], text=verdict)
    except Exception:
        pass

    case["judge_text"] = verdict
    case["status"] = "closed"


async def _user_name(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> str:
    try:
        user = await context.bot.get_chat(user_id)
        return user.first_name or user.username or f"user_{user_id}"
    except Exception:
        return f"user_{user_id}"
