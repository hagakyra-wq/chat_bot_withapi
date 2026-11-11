"""Минимальные обработчики личных сообщений: меню, саммари, транскрипция, суд и удаление."""

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatType
from telegram.ext import CallbackContext, CommandHandler, ContextTypes, MessageHandler, filters

from config import ADMIN_ID
import database
from court import (
    is_court_request,
    is_laws_request,
    record_court_story,
    send_laws_summary,
    start_court_case,
)
from court_advanced import (
    advanced_court_callback,
    court_advanced_help,
    is_court_request as is_advanced_court_request,
    record_advanced_court_story,
    start_advanced_court,
)
from death_fate import (
    get_death_time_reply,
    is_death_reason_request,
    is_death_time_request,
    random_death_cause,
)
from llm import extract_summary_period_minutes, generate_summary, is_summary_request
from message_deletion import handle_delete_bot_messages, handle_delete_replied_message
from voice_transcription import handle_voice_transcription, is_transcription_request


def _menu_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🧾 Саммари", callback_data="menu:summary"),
                InlineKeyboardButton("🗑️ Удалить", callback_data="menu:delete"),
            ],
            [
                InlineKeyboardButton("⚖️ Суд", callback_data="menu:court"),
                InlineKeyboardButton("📜 Законы", callback_data="menu:law"),
            ],
            [
                InlineKeyboardButton("🏛️ Суд-2", callback_data="menu:advanced_court"),
                InlineKeyboardButton("🎙️ Транскрибация", callback_data="menu:transcribe"),
            ],
            [
                InlineKeyboardButton("👑 Админ", callback_data="menu:admin"),
                InlineKeyboardButton("⬅️ Назад", callback_data="menu:main"),
            ],
        ]
    )


def _admin_help_text() -> str:
    return (
        "⚙️ Админ-панель\n\n"
        "Доступные команды:\n"
        "• /adm — открыть панель администратора\n"
        "• /admin — тот же вход\n"
        "• /adminhelp — справка с учётом прав\n"
        "• /helpadmin — алиас справки\n\n"
        "Опасные действия требуют подтверждения.\n"
        "Сейчас в панели доступен сброс истории чата для текущего чата."
    )


def _admin_panel_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🧹 Сбросить историю чата", callback_data="menu:reset_history"),
                InlineKeyboardButton("ℹ️ Справка", callback_data="menu:admin_help"),
            ],
            [
                InlineKeyboardButton("⬅️ Назад в меню", callback_data="menu:main"),
            ],
        ]
    )


def _court_help_text() -> str:
    return (
        "⚖️ Суд Юбары\n\n"
        "1. Ответь на сообщение командой 'суд'.\n"
        "2. Истец и ответчик получают приглашения в ЛС.\n"
        "3. Каждая сторона пишет свою версию событий.\n"
        "4. Если ответчик молчит, суд опирается на историю чата.\n"
        "5. Вердикт публикуется в группе."
    )


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    message = update.effective_message
    if message is None:
        return
    await message.reply_text(
        "Главное меню\n\n"
        "• Напиши 'саммари' или 'что я пропустил'\n"
        "• Ответь на аудио текстом 'расшифруй'\n"
        "• Используй 'дэл' или 'удали 5' для удаления сообщений\n"
        "• Напиши 'суд' в ответ на сообщение, чтобы начать судебный процесс\n"
        "• Напиши 'кодекс' или 'законы', чтобы посмотреть свод правил Юбары\n"
        "• Напиши 'суд' в личке, чтобы получить краткую инструкцию по процессу",
        reply_markup=_menu_markup(),
    )


async def menu_callback(update: Update, context: CallbackContext) -> None:
    del context
    query = update.callback_query
    if query is None:
        return
    await query.answer()
    action = query.data or ""
    if action == "menu:main":
        await query.edit_message_text(
            "Главное меню\n\n• Напиши 'саммари'\n• Используй 'суд' в ответ на сообщение\n• Напиши 'кодекс' или 'законы'\n• Ответь на голосовое: 'расшифруй'",
            reply_markup=_menu_markup(),
        )
    elif action == "menu:summary":
        await query.edit_message_text(
            "Для саммари напиши в чате 'саммари' или 'что я пропустил'. Можно указать период: 'саммари за 30 минут'.",
            reply_markup=_menu_markup(),
        )
    elif action == "menu:delete":
        await query.edit_message_text(
            "Чтобы удалить сообщение, ответь на него командой 'дэл'. Для удаления нескольких сообщений: 'дэл 5' или 'удали 5'.",
            reply_markup=_menu_markup(),
        )
    elif action == "menu:transcribe":
        await query.edit_message_text(
            "Ответь на голосовое или аудиофайл текстом: 'расшифруй', 'в текст' или 'транскриб'.",
            reply_markup=_menu_markup(),
        )
    elif action == "menu:court":
        await query.edit_message_text(_court_help_text(), reply_markup=_menu_markup())
    elif action == "menu:law":
        await query.edit_message_text(
            "📜 Кодекс Юбари\n\nКодекс можно посмотреть командой 'кодекс', 'законы' или 'сводка законов'.\n\nСудья учитывает нормы кодекса при вынесении вердикта, особенно для флуда, мемов, стикеров, фото, видео, голосовых и ботов.",
            reply_markup=_menu_markup(),
        )
    elif action == "menu:advanced_court":
        await query.edit_message_text(
            "🏛️ Суд-2 — расширенный процесс. Ответь в группе на сообщение командой 'суд', и бот запустит таймер и приглашения в ЛС.",
            reply_markup=_menu_markup(),
        )
    elif action == "menu:admin":
        user_id = query.from_user.id if query.from_user else None
        if user_id not in (ADMIN_ID,):
            await query.answer("Нет доступа к админ-панели.", show_alert=True)
            return
        await query.edit_message_text(_admin_help_text(), reply_markup=_admin_panel_markup())
    elif action == "menu:admin_help":
        await query.edit_message_text(_admin_help_text(), reply_markup=_admin_panel_markup())
    elif action == "menu:reset_history":
        user_id = query.from_user.id if query.from_user else None
        if user_id not in (ADMIN_ID,):
            await query.answer("Только администратор может сбрасывать историю.", show_alert=True)
            return
        try:
            deleted = await database.clear_chat_history(query.message.chat_id)
            await query.edit_message_text(
                f"🧹 История чата очищена. Удалено записей: {deleted}.",
                reply_markup=_menu_markup(),
            )
        except Exception:
            logging.exception("Не удалось сбросить историю чата %s.", query.message.chat_id)
            await query.edit_message_text("Не удалось очистить историю чата. Попробуй позже.", reply_markup=_menu_markup())


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    message = update.effective_message
    if message is None:
        return
    if message.from_user is None or message.from_user.id not in (ADMIN_ID,):
        await message.reply_text("Админ-панель доступна только владельцу бота.")
        return
    await message.reply_text(_admin_help_text(), reply_markup=_admin_panel_markup())


def _message_content_text(message) -> str:
    if message is None:
        return ""
    if message.text:
        return message.text.strip()
    if message.caption:
        return message.caption.strip()
    if message.sticker:
        return "[стикер]"
    if message.photo:
        return "[фото]"
    if message.video:
        return "[видео]"
    if message.voice:
        return "[голосовое]"
    if message.audio:
        return "[аудио]"
    if message.document:
        return "[документ]"
    if message.animation:
        return "[анимация]"
    return ""


async def handle_plain_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return
    text = _message_content_text(message)
    if not text:
        return

    if await handle_delete_replied_message(update, context):
        return
    if await handle_delete_bot_messages(update, context):
        return

    if await record_advanced_court_story(update, context):
        return
    if await record_court_story(update, context):
        return

    if is_transcription_request(text, message):
        await handle_voice_transcription(message, context)
        return

    if is_laws_request(text):
        await send_laws_summary(message)
        return

    if is_advanced_court_request(text):
        if message.chat.type == ChatType.PRIVATE:
            await court_advanced_help(message)
            return
        await start_advanced_court(message, context)
        return

    if is_court_request(text):
        if message.chat.type == ChatType.PRIVATE:
            await message.reply_text(_court_help_text())
            return
        await start_court_case(message, context)
        return

    if is_death_reason_request(text):
        user_id = message.from_user.id if message.from_user is not None else 0
        await message.reply_text(f"Причина смерти: {random_death_cause(user_id, message.chat_id)}.")
        return

    if is_death_time_request(text):
        user_id = message.from_user.id if message.from_user is not None else 0
        await message.reply_text(get_death_time_reply(message.chat_id, user_id))
        return

    if not is_summary_request(text):
        return

    period_minutes = extract_summary_period_minutes(text) or 60
    rows = await database.get_summary_history_rows(message.chat_id, period_minutes)
    if not rows:
        await message.reply_text("За выбранный период сообщений нет.")
        return

    participant_genders: dict[int, str] = {}
    for row in rows:
        user_id = row.get("user_id")
        if user_id is not None:
            participant_genders[int(user_id)] = str(row.get("gender") or "неизвестен")

    try:
        summary = await generate_summary(
            rows,
            period_minutes=period_minutes,
            participant_genders=participant_genders,
        )
        await message.reply_text(summary)
    except Exception:
        logging.exception("Не удалось сделать саммари для чата %s.", message.chat_id)
        await message.reply_text("Не удалось составить саммари. Попробуй позже.")


async def main() -> None:
    from telegram.ext import Application
    from config import TELEGRAM_TOKEN

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("admin", admin_command))
    app.add_handler(CallbackQueryHandler(menu_callback, pattern="^menu:"))
    app.add_handler(CallbackQueryHandler(advanced_court_callback, pattern="^court_adv:"))
    app.add_handler(MessageHandler(filters.TEXT | filters.PHOTO | filters.VIDEO | filters.VOICE | filters.STICKER | filters.AUDIO | filters.ANIMATION | filters.Document.ALL, handle_plain_message))
    await app.initialize()
    await app.start()
    await app.updater.start_polling()  # type: ignore[attr-defined]
    await app.updater.idle()  # type: ignore[attr-defined]


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
