"""Обработчики личных сообщений, ключей доступа и админ-панели."""

import logging
import random
import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReactionTypeEmoji, Update
from telegram.constants import ChatAction, ChatType
from telegram.error import TelegramError
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

import database
from config import ADMIN_ID, AVAILABLE_REACTIONS, DEFAULT_CONTEXT_SIZE, MAX_HISTORY_SIZE
from llm import build_prompt, generate_reply, is_summary_request, parse_meta

ADMIN_MENU, WAIT_ADD_GROUP, WAIT_REMOVE_GROUP, WAIT_CREATE_KEY, WAIT_REVOKE_KEY = range(5)
ADMIN_PANEL_USERS: set[int] = set()
ACCESS_MESSAGE = (
    "Чтобы получить доступ к функционалу бота, необходимо ввести пароль, "
    "который можно получить у @BIGBACA"
)
_GROUP_ID_RE = re.compile(r"^-?\d{1,20}$")


def _admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("➕ Добавить группу", callback_data="admin:add_group")],
            [InlineKeyboardButton("➖ Удалить группу", callback_data="admin:remove_group")],
            [InlineKeyboardButton("🔑 Создать ключ доступа", callback_data="admin:create_key")],
            [InlineKeyboardButton("🚫 Отозвать ключ", callback_data="admin:revoke_key")],
            [InlineKeyboardButton("👥 Список людей с доступом", callback_data="admin:people")],
            [InlineKeyboardButton("🚪 Выйти из админ-панели", callback_data="admin:exit")],
        ]
    )


async def _show_admin_menu(update: Update) -> None:
    query = update.callback_query
    if query:
        await query.edit_message_text("Панель администратора Юбары:", reply_markup=_admin_keyboard())
    elif update.effective_message:
        await update.effective_message.reply_text(
            "Панель администратора Юбары:",
            reply_markup=_admin_keyboard(),
        )


async def admin_start(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> int:
    del _context
    user = update.effective_user
    if user is None or user.id != ADMIN_ID or update.effective_chat is None:
        return ConversationHandler.END
    if update.effective_chat.type != ChatType.PRIVATE:
        return ConversationHandler.END
    ADMIN_PANEL_USERS.add(user.id)
    await _show_admin_menu(update)
    return ADMIN_MENU


async def admin_cancel(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> int:
    del _context
    user = update.effective_user
    if user:
        ADMIN_PANEL_USERS.discard(user.id)
    if update.effective_message:
        await update.effective_message.reply_text("Админ-панель закрыта. Диалоговый режим снова включён.")
    return ConversationHandler.END


async def admin_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    user = update.effective_user
    if query is None:
        return ConversationHandler.END
    await query.answer()
    if user is None or user.id != ADMIN_ID:
        return ConversationHandler.END

    action = query.data or ""
    if action == "admin:exit":
        ADMIN_PANEL_USERS.discard(user.id)
        await query.edit_message_text("Админ-панель закрыта. Диалоговый режим снова включён.")
        return ConversationHandler.END
    if action == "admin:add_group":
        await query.edit_message_text(
            "Отправь ID группы или супергруппы (обычно отрицательное число).",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("↩️ Назад", callback_data="admin:menu")]]
            ),
        )
        return WAIT_ADD_GROUP
    if action == "admin:remove_group":
        groups = await database.list_allowed_groups()
        buttons = [
            [InlineKeyboardButton(str(group_id), callback_data=f"admin:drop_group:{group_id}")]
            for group_id in groups
        ]
        buttons.append([InlineKeyboardButton("↩️ Назад", callback_data="admin:menu")])
        await query.edit_message_text(
            "Выбери группу для удаления или отправь её ID сообщением:",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return WAIT_REMOVE_GROUP
    if action == "admin:create_key":
        await query.edit_message_text(
            "Отправь одноразовый ключ доступа. Можно использовать любую непустую строку.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("↩️ Назад", callback_data="admin:menu")]]
            ),
        )
        return WAIT_CREATE_KEY
    if action == "admin:revoke_key":
        keys = await database.list_access_keys()
        context.user_data["revoke_keys"] = keys
        buttons = [
            [
                InlineKeyboardButton(
                    f"Отозвать: {key[:35]}",
                    callback_data=f"admin:revoke:{index}",
                )
            ]
            for index, key in enumerate(keys)
        ]
        buttons.append([InlineKeyboardButton("↩️ Назад", callback_data="admin:menu")])
        await query.edit_message_text(
            "Выбери активный ключ для отзыва:"
            if keys
            else "Активных ключей нет.",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return WAIT_REVOKE_KEY
    if action == "admin:people":
        people = await database.list_authorized_users()
        description = "\n".join(
            f"{user_id} — @{username}" if username else f"{user_id} — username пока неизвестен"
            for user_id, username in people
        )
        await query.edit_message_text(
            "Люди с доступом:\n" + (description or "Список пока пуст."),
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("↩️ Назад", callback_data="admin:menu")]]
            ),
        )
        return ADMIN_MENU
    if action == "admin:menu":
        await _show_admin_menu(update)
        return ADMIN_MENU
    return ADMIN_MENU


async def _return_to_menu(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> int:
    del _context
    query = update.callback_query
    if query:
        await query.answer()
    await _show_admin_menu(update)
    return ADMIN_MENU


async def add_group_input(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> int:
    del _context
    message = update.effective_message
    if message is None or not message.text:
        return WAIT_ADD_GROUP
    value = message.text.strip()
    if not _GROUP_ID_RE.fullmatch(value):
        await message.reply_text("Неверный формат ID. Отправь целое число, например -1001234567890.")
        return WAIT_ADD_GROUP
    group_id = int(value)
    if group_id >= 0:
        await message.reply_text("ID Telegram-группы должен быть отрицательным числом.")
        return WAIT_ADD_GROUP
    try:
        inserted = await database.add_allowed_group(group_id)
        await message.reply_text(
            "Группа добавлена." if inserted else "Эта группа уже есть в белом списке."
        )
        await _show_admin_menu(update)
        return ADMIN_MENU
    except Exception:
        logging.exception("Ошибка добавления группы %s", group_id)
        await message.reply_text("Не удалось добавить группу из-за ошибки базы данных.")
        return WAIT_ADD_GROUP


async def remove_group_input(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> int:
    del _context
    message = update.effective_message
    if message is None or not message.text:
        return WAIT_REMOVE_GROUP
    value = message.text.strip()
    if not _GROUP_ID_RE.fullmatch(value):
        await message.reply_text("Неверный формат ID. Отправь целое число.")
        return WAIT_REMOVE_GROUP
    try:
        removed = await database.remove_allowed_group(int(value))
        await message.reply_text("Группа удалена." if removed else "Такой группы нет в белом списке.")
        await _show_admin_menu(update)
        return ADMIN_MENU
    except Exception:
        logging.exception("Ошибка удаления группы %s", value)
        await message.reply_text("Не удалось удалить группу из-за ошибки базы данных.")
        return WAIT_REMOVE_GROUP


async def create_key_input(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> int:
    del _context
    message = update.effective_message
    if message is None or message.text is None:
        return WAIT_CREATE_KEY
    if not message.text or message.text.isspace():
        await message.reply_text("Ключ не может быть пустым.")
        return WAIT_CREATE_KEY
    try:
        created = await database.create_access_key(message.text)
        await message.reply_text(
            "Ключ создан. Передай его пользователю лично."
            if created
            else "Такой ключ уже существует. Отправь другое значение."
        )
        if created:
            await _show_admin_menu(update)
            return ADMIN_MENU
        return WAIT_CREATE_KEY
    except Exception:
        logging.exception("Ошибка создания ключа доступа")
        await message.reply_text("Не удалось сохранить ключ из-за ошибки базы данных.")
        return WAIT_CREATE_KEY


async def revoke_key_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query is None:
        return WAIT_REVOKE_KEY
    await query.answer()
    try:
        index = int((query.data or "").rsplit(":", 1)[1])
        keys = context.user_data.get("revoke_keys", [])
        key = keys[index]
        revoked = await database.revoke_access_key(key)
        await query.edit_message_text(
            "Ключ отозван." if revoked else "Этот ключ уже не активен.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("↩️ В меню", callback_data="admin:menu")]]
            ),
        )
        return WAIT_REVOKE_KEY
    except (ValueError, IndexError, TypeError):
        await query.edit_message_text(
            "Выбранный ключ больше недоступен.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("↩️ В меню", callback_data="admin:menu")]]
            ),
        )
        return WAIT_REVOKE_KEY
    except Exception:
        logging.exception("Ошибка отзыва ключа")
        await query.edit_message_text("Не удалось отозвать ключ из-за ошибки базы данных.")
        return WAIT_REVOKE_KEY


def build_admin_conversation_handler() -> ConversationHandler:
    back = CallbackQueryHandler(_return_to_menu, pattern=r"^admin:menu$")
    return ConversationHandler(
        entry_points=[CommandHandler("adm", admin_start, filters=filters.ChatType.PRIVATE)],
        states={
            ADMIN_MENU: [CallbackQueryHandler(admin_menu_callback, pattern=r"^admin:")],
            WAIT_ADD_GROUP: [
                back,
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_group_input),
            ],
            WAIT_REMOVE_GROUP: [
                back,
                CallbackQueryHandler(
                    remove_group_callback,
                    pattern=r"^admin:drop_group:",
                ),
                MessageHandler(filters.TEXT & ~filters.COMMAND, remove_group_input),
            ],
            WAIT_CREATE_KEY: [
                back,
                MessageHandler(filters.TEXT & ~filters.COMMAND, create_key_input),
            ],
            WAIT_REVOKE_KEY: [
                back,
                CallbackQueryHandler(revoke_key_callback, pattern=r"^admin:revoke:"),
            ],
        },
        fallbacks=[CommandHandler("cancel", admin_cancel)],
        allow_reentry=True,
        per_message=False,
    )


async def remove_group_callback(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> int:
    del _context
    query = update.callback_query
    if query is None:
        return WAIT_REMOVE_GROUP
    await query.answer()
    try:
        group_id = int((query.data or "").rsplit(":", 1)[1])
        removed = await database.remove_allowed_group(group_id)
        await query.edit_message_text(
            "Группа удалена." if removed else "Группа уже отсутствует.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("↩️ В меню", callback_data="admin:menu")]]
            ),
        )
        return WAIT_REMOVE_GROUP
    except (ValueError, IndexError):
        await query.edit_message_text("Не удалось прочитать ID выбранной группы.")
        return WAIT_REMOVE_GROUP
    except Exception:
        logging.exception("Ошибка удаления группы из админ-панели")
        await query.edit_message_text("Не удалось удалить группу из-за ошибки базы данных.")
        return WAIT_REMOVE_GROUP


async def _private_dialog(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    message_text: str,
) -> None:
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None:
        return
    user_id = user.id
    authorized = user_id == ADMIN_ID
    if not authorized:
        try:
            authorized = await database.is_authorized(user_id)
            if not authorized and await database.consume_access_key(message_text, user_id):
                await message.reply_text("Доступ открыт! Теперь можешь общаться со мной.")
                return
        except Exception:
            logging.exception("Ошибка проверки доступа пользователя %s", user_id)
            await message.reply_text("Не удалось проверить доступ. Попробуй позже.")
            return
    if not authorized:
        await message.reply_text(ACCESS_MESSAGE)
        return

    if message_text.strip().casefold() == "дэл":
        replied = message.reply_to_message
        if replied and replied.from_user and replied.from_user.id == context.bot.id:
            try:
                await replied.delete()
                await message.delete()
            except TelegramError:
                logging.exception("Не удалось выполнить «дэл» в личном чате")
            return

    command = message_text.strip().casefold()
    if (
        command in ("/reaction", "/react")
        or re.fullmatch(r"/(?:reaction|react)@[\w]+", command)
        or command in ("реакция", "реакцию")
    ):
        target = message.reply_to_message or message
        try:
            await target.set_reaction(
                reaction=[ReactionTypeEmoji(random.choice(AVAILABLE_REACTIONS))]
            )
        except TelegramError:
            logging.exception("Не удалось установить реакцию командой в личном чате")
        return

    try:
        summary = is_summary_request(message_text)
        await database.save_message(
            message.chat_id,
            user_id,
            message.message_id,
            user.first_name or user.username or str(user_id),
            message_text,
        )
        history = await database.get_recent_history(
            message.chat_id,
            MAX_HISTORY_SIZE if summary else DEFAULT_CONTEXT_SIZE,
        )
        replied_data = None
        replied = message.reply_to_message
        if replied and replied.text:
            replied_data = (
                replied.message_id,
                replied.from_user.first_name if replied.from_user else "Кто-то",
                replied.from_user.id if replied.from_user else None,
                replied.text,
            )
        prompt = build_prompt(
            history=history,
            user_id=user_id,
            user_name=user.first_name or user.username or str(user_id),
            display_name=user.first_name or user.username or str(user_id),
            current_message=message_text,
            summary=summary,
            replied_message=replied_data,
        )
    except Exception:
        logging.exception("Ошибка сохранения личного диалога в SQLite")
        await message.reply_text("Не удалось подготовить диалог. Попробуй позже.")
        return

    try:
        await context.bot.send_chat_action(message.chat_id, ChatAction.TYPING)
    except TelegramError:
        logging.warning("Не удалось показать typing action в личном чате", exc_info=True)
    answer, metadata = parse_meta(await generate_reply(prompt, summary))
    if not answer:
        answer = "Чего уставился? Я просто немного смутилась..."
    reaction = metadata.get("reaction") if metadata else None
    if isinstance(reaction, str) and reaction in AVAILABLE_REACTIONS and random.random() < 0.15:
        try:
            await message.set_reaction(reaction=[ReactionTypeEmoji(reaction)])
        except TelegramError:
            logging.exception("Не удалось установить META-реакцию в личном чате")
    try:
        sent = await message.reply_text(answer)
    except TelegramError:
        logging.exception("Не удалось ответить в личном чате")
        return
    try:
        await database.save_message(
            message.chat_id,
            context.bot.id,
            sent.message_id,
            "Юбара",
            answer,
        )
    except Exception:
        logging.exception("Не удалось сохранить ответ в личной истории")


async def on_private_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if (
        message is None
        or user is None
        or user.is_bot
        or message.chat.type != ChatType.PRIVATE
        or message.text is None
    ):
        return
    if user.id in ADMIN_PANEL_USERS:
        return
    await _private_dialog(update, context, message.text)
