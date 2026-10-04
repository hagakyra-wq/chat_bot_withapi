"""Обработчики личных сообщений, ключей доступа и админ-панели."""

import logging
import random
import re
import time

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
from config import ADMIN_ID, AVAILABLE_REACTIONS, DEFAULT_CONTEXT_SIZE, PROFILE_TRIGGERS
from duel import handle_balance_request, handle_duel_challenge
from images import extract_image_request, image_manager
from llm import (
    build_prompt,
    extract_reported_age,
    generate_reply,
    is_summary_request,
    parse_meta,
)
from message_deletion import handle_delete_bot_messages
from reactions import maybe_react

ADMIN_MENU, WAIT_ADD_GROUP, WAIT_REMOVE_GROUP, WAIT_CREATE_KEY, WAIT_REVOKE_KEY = range(5)
WAIT_KEY_DURATION, WAIT_REVOKE_USER, WAIT_REVOKE_ADMIN = range(5, 8)
ADMIN_PANEL_USERS: set[int] = set()
ACCESS_DURATIONS = {
    "forever": (None, "навсегда"),
    "month": (30 * 24 * 60 * 60, "30 дней"),
    "week": (7 * 24 * 60 * 60, "7 дней"),
    "day": (24 * 60 * 60, "24 часа"),
}
ACCESS_MESSAGE = (
    "Чтобы получить доступ к функционалу бота, необходимо ввести пароль, "
    "который можно получить у @BIGBACA"
)
_GROUP_ID_RE = re.compile(r"^-?\d{1,20}$")


def _format_private_profile(
    profile: dict[str, object],
    group_ids: list[int],
) -> str:
    notes = [
        fact
        for fact in str(profile.get("notes") or "").splitlines()
        if fact.strip()
    ]
    facts = "\n".join(f"• {fact}" for fact in notes) or "• пока фактов нет"
    lines = [
        "Мой глобальный архив о тебе:\n"
        f"Имя: {profile.get('display_name') or 'не указано'}",
        f"Telegram ID: {profile['user_id']}",
        f"Username: @{profile['username']}" if profile.get("username") else "Username: не указан",
        f"Кличка: {profile.get('callsign') or 'не задана'}",
        f"Возраст: {profile.get('age') or 'не указан'}",
        f"Пол: {profile.get('gender') if profile.get('gender') != 'неизвестен' else 'не указан'}",
        "ID групп: " + (", ".join(map(str, group_ids)) if group_ids else "пока нет"),
        "Факты:",
        facts,
        "Будешь делиться фактами — я сохраню их в своих записях.",
    ]
    return "\n".join(lines)


async def _apply_private_profile_meta(
    metadata: dict[str, object] | None,
    *,
    user_id: int,
    chat_id: int,
) -> None:
    if not metadata:
        return
    target = metadata.get("target_user_id")
    if target not in (None, "", user_id, str(user_id)):
        return
    gender_value = metadata.get("gender")
    gender = (
        gender_value
        if isinstance(gender_value, str) and gender_value in ("парень", "девушка")
        else None
    )
    nickname_value = metadata.get("nickname", metadata.get("callsign"))
    callsign = (
        nickname_value.strip()[:80]
        if isinstance(nickname_value, str) and nickname_value.strip()
        else None
    )
    age_value = metadata.get("age")
    age = (
        age_value
        if isinstance(age_value, int) and not isinstance(age_value, bool) and 1 <= age_value <= 120
        else None
    )
    note_value = metadata.get("note")
    note = note_value.strip()[:500] if isinstance(note_value, str) and note_value.strip() else None
    await database.update_user_profile(
        user_id,
        chat_id,
        gender=gender,
        callsign=callsign,
        age=age,
        note=note,
    )


def _admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("➕ Добавить группу", callback_data="admin:add_group")],
            [InlineKeyboardButton("➖ Удалить группу", callback_data="admin:remove_group")],
            [InlineKeyboardButton("🔑 Ключ обычного доступа", callback_data="admin:create_user_key")],
            [InlineKeyboardButton("👑 Пригласить админа", callback_data="admin:create_admin_key")],
            [InlineKeyboardButton("🚫 Отозвать ключ", callback_data="admin:revoke_key")],
            [InlineKeyboardButton("👥 Список людей с доступом", callback_data="admin:people")],
            [InlineKeyboardButton("🚫 Отозвать доступ пользователя", callback_data="admin:revoke_user")],
            [InlineKeyboardButton("🚫 Отозвать админа", callback_data="admin:revoke_admin")],
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
    if user is None or update.effective_chat is None:
        return ConversationHandler.END
    if update.effective_chat.type != ChatType.PRIVATE:
        return ConversationHandler.END
    try:
        if not await _is_admin(user.id):
            return ConversationHandler.END
    except Exception:
        logging.exception("Не удалось проверить право администратора для /adm")
        return ConversationHandler.END
    ADMIN_PANEL_USERS.add(user.id)
    await _show_admin_menu(update)
    return ADMIN_MENU


async def break_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    user = update.effective_user
    message = update.effective_message
    chat = update.effective_chat
    if (
        user is None
        or message is None
        or chat is None
        or chat.type != ChatType.PRIVATE
    ):
        return
    try:
        if not await _is_admin(user.id):
            return
    except Exception:
        logging.exception("Не удалось проверить право администратора для /break")
        return

    try:
        await message.reply_text(
            "Останавливаю процесс бота. Render может автоматически запустить его снова."
        )
    except TelegramError:
        logging.exception("Не удалось подтвердить администратору выполнение /break")

    logging.info("Администратор user_id=%s запросил остановку процесса через /break.", user.id)
    context.application.stop_running()


async def _is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID or await database.is_admin_user(user_id)


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
    if user is None or not await _is_admin(user.id):
        if user is not None:
            ADMIN_PANEL_USERS.discard(user.id)
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
    if action in ("admin:create_key", "admin:create_user_key", "admin:create_admin_key"):
        access_level = "admin" if action == "admin:create_admin_key" else "user"
        context.user_data["new_key_access_level"] = access_level
        level_label = "админский" if access_level == "admin" else "пользовательский"
        duration_buttons = [
            [InlineKeyboardButton(label, callback_data=f"admin:duration:{access_level}:{key}")]
            for key, (_, label) in ACCESS_DURATIONS.items()
        ]
        duration_buttons.append(
            [InlineKeyboardButton("↩️ Назад", callback_data="admin:menu")]
        )
        await query.edit_message_text(
            f"Выбери срок {level_label}а после активации одноразового ключа:",
            reply_markup=InlineKeyboardMarkup(duration_buttons),
        )
        return WAIT_KEY_DURATION
    if action == "admin:revoke_key":
        keys = await database.list_access_keys()
        context.user_data["revoke_keys"] = keys
        buttons = [
            [
                InlineKeyboardButton(
                    f"{'Админ' if key['access_level'] == 'admin' else 'Доступ'}: {key['key'][:30]}",
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
        users = await database.list_authorized_users(access_level="user")
        admins = await database.list_authorized_users(access_level="admin")
        entries = [
            "Пользователи:",
            *(_format_access_record(item) for item in users),
            "",
            "Администраторы:",
            *(
                _format_access_record(item)
                for item in admins
                if int(item["user_id"]) != ADMIN_ID
            ),
        ]
        description = "\n".join(entries)
        await query.edit_message_text(
            "Люди с доступом:\n" + description,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("↩️ Назад", callback_data="admin:menu")]]
            ),
        )
        return ADMIN_MENU
    if action == "admin:revoke_user":
        people = await database.list_authorized_users(access_level="user")
        buttons = [
            [
                InlineKeyboardButton(
                    f"{item['user_id']} @{item['username'] or 'unknown'}",
                    callback_data=f"admin:revoke_user:{item['user_id']}",
                )
            ]
            for item in people
        ]
        buttons.append([InlineKeyboardButton("↩️ Назад", callback_data="admin:menu")])
        await query.edit_message_text(
            "Выбери пользователя, которому нужно отозвать доступ:"
            if people
            else "Пользователей с активным доступом нет.",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return WAIT_REVOKE_USER
    if action == "admin:revoke_admin":
        people = [
            item
            for item in await database.list_authorized_users(access_level="admin")
            if int(item["user_id"]) != ADMIN_ID
        ]
        buttons = [
            [
                InlineKeyboardButton(
                    f"{item['user_id']} @{item['username'] or 'unknown'}",
                    callback_data=f"admin:revoke_admin:{item['user_id']}",
                )
            ]
            for item in people
        ]
        buttons.append([InlineKeyboardButton("↩️ Назад", callback_data="admin:menu")])
        await query.edit_message_text(
            "Выбери администратора, которому нужно отозвать права:"
            if people
            else "Дополнительных администраторов нет.",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return WAIT_REVOKE_ADMIN
    if action == "admin:menu":
        await _show_admin_menu(update)
        return ADMIN_MENU
    return ADMIN_MENU


def _format_access_record(record: dict[str, object]) -> str:
    username = record.get("username")
    expires_at = record.get("expires_at")
    if expires_at is None:
        expiry = "навсегда"
    else:
        expiry = time.strftime("%d.%m.%Y %H:%M UTC", time.gmtime(float(expires_at)))
    suffix = f"@{username}" if username else "username неизвестен"
    return f"{record['user_id']} — {suffix}; действует до: {expiry}"


async def _return_to_menu(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> int:
    del _context
    query = update.callback_query
    if query:
        await query.answer()
    user = update.effective_user
    if user is None or not await _is_admin(user.id):
        if user is not None:
            ADMIN_PANEL_USERS.discard(user.id)
        return ConversationHandler.END
    await _show_admin_menu(update)
    return ADMIN_MENU


async def add_group_input(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> int:
    del _context
    user = update.effective_user
    if user is None or not await _is_admin(user.id):
        return ConversationHandler.END
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
    user = update.effective_user
    if user is None or not await _is_admin(user.id):
        return ConversationHandler.END
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
    context = _context
    user = update.effective_user
    if user is None or not await _is_admin(user.id):
        if user is not None:
            ADMIN_PANEL_USERS.discard(user.id)
        return ConversationHandler.END
    message = update.effective_message
    if message is None or message.text is None:
        return WAIT_CREATE_KEY
    if not message.text or message.text.isspace():
        await message.reply_text("Ключ не может быть пустым.")
        return WAIT_CREATE_KEY
    try:
        access_level = context.user_data.get("new_key_access_level", "user")
        duration_seconds = context.user_data.get("new_key_duration")
        created = await database.create_access_key(
            message.text,
            access_level=access_level,
            duration_seconds=duration_seconds,
            created_by=user.id,
        )
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
    user = update.effective_user
    if query is None:
        return WAIT_REVOKE_KEY
    await query.answer()
    if user is None or not await _is_admin(user.id):
        if user is not None:
            ADMIN_PANEL_USERS.discard(user.id)
        return ConversationHandler.END
    try:
        index = int((query.data or "").rsplit(":", 1)[1])
        keys = context.user_data.get("revoke_keys", [])
        key = keys[index]["key"]
        revoked = await database.revoke_access_key(str(key))
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


async def key_duration_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    query = update.callback_query
    user = update.effective_user
    if query is None:
        return WAIT_KEY_DURATION
    await query.answer()
    if user is None or not await _is_admin(user.id):
        if user is not None:
            ADMIN_PANEL_USERS.discard(user.id)
        return ConversationHandler.END
    parts = (query.data or "").split(":")
    if len(parts) != 4 or parts[2] not in ("user", "admin"):
        await query.edit_message_text("Некорректный срок доступа.")
        return WAIT_KEY_DURATION
    duration_key = parts[3]
    if duration_key not in ACCESS_DURATIONS:
        await query.edit_message_text("Такой срок доступа не поддерживается.")
        return WAIT_KEY_DURATION
    duration_seconds, duration_label = ACCESS_DURATIONS[duration_key]
    access_level = parts[2]
    context.user_data["new_key_access_level"] = access_level
    context.user_data["new_key_duration"] = duration_seconds
    access_label = "админское приглашение" if access_level == "admin" else "ключ доступа"
    await query.edit_message_text(
        f"Срок {duration_label} выбран. Отправь значение одноразового {access_label}."
    )
    return WAIT_CREATE_KEY


async def revoke_authorized_callback(
    update: Update,
    _context: ContextTypes.DEFAULT_TYPE,
) -> int:
    del _context
    query = update.callback_query
    user = update.effective_user
    if query is None:
        return ConversationHandler.END
    await query.answer()
    if user is None or not await _is_admin(user.id):
        if user is not None:
            ADMIN_PANEL_USERS.discard(user.id)
        return ConversationHandler.END
    action = query.data or ""
    access_level = "admin" if action.startswith("admin:revoke_admin:") else "user"
    try:
        target_id = int(action.rsplit(":", 1)[1])
        if target_id == ADMIN_ID:
            await query.edit_message_text("Главный администратор не может быть отозван.")
            return WAIT_REVOKE_ADMIN if access_level == "admin" else WAIT_REVOKE_USER
        revoked = await database.revoke_authorized_user(
            target_id,
            access_level=access_level,
        )
        if access_level == "admin" and target_id == user.id:
            ADMIN_PANEL_USERS.discard(user.id)
        result = "Доступ отозван." if revoked else "Активная запись доступа не найдена."
        await query.edit_message_text(
            result,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("↩️ В меню", callback_data="admin:menu")]]
            ),
        )
        return WAIT_REVOKE_ADMIN if access_level == "admin" else WAIT_REVOKE_USER
    except (ValueError, IndexError):
        await query.edit_message_text("Не удалось прочитать ID пользователя.")
        return WAIT_REVOKE_ADMIN if access_level == "admin" else WAIT_REVOKE_USER
    except Exception:
        logging.exception("Ошибка отзыва доступа")
        await query.edit_message_text("Не удалось отозвать доступ из-за ошибки базы данных.")
        return WAIT_REVOKE_ADMIN if access_level == "admin" else WAIT_REVOKE_USER


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
            WAIT_KEY_DURATION: [
                back,
                CallbackQueryHandler(key_duration_callback, pattern=r"^admin:duration:"),
            ],
            WAIT_REVOKE_KEY: [
                back,
                CallbackQueryHandler(revoke_key_callback, pattern=r"^admin:revoke:"),
            ],
            WAIT_REVOKE_USER: [
                back,
                CallbackQueryHandler(
                    revoke_authorized_callback,
                    pattern=r"^admin:revoke_user:",
                ),
            ],
            WAIT_REVOKE_ADMIN: [
                back,
                CallbackQueryHandler(
                    revoke_authorized_callback,
                    pattern=r"^admin:revoke_admin:",
                ),
            ],
        },
        fallbacks=[CommandHandler("cancel", admin_cancel)],
        allow_reentry=True,
        per_message=False,
    )


async def remove_group_callback(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> int:
    del _context
    user = update.effective_user
    query = update.callback_query
    if query is None:
        return WAIT_REMOVE_GROUP
    await query.answer()
    if user is None or not await _is_admin(user.id):
        if user is not None:
            ADMIN_PANEL_USERS.discard(user.id)
        return ConversationHandler.END
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
            if not authorized:
                granted = await database.consume_access_key(
                    message_text,
                    user_id,
                    user.username,
                )
                if granted is not None:
                    level = granted["access_level"]
                    expires_at = granted["expires_at"]
                    expiry = (
                        "без ограничения срока"
                        if expires_at is None
                        else "до " + time.strftime("%d.%m.%Y %H:%M UTC", time.gmtime(expires_at))
                    )
                    await message.reply_text(
                        f"Доступ администратора открыт {expiry}!"
                        if level == "admin"
                        else f"Доступ к боту открыт {expiry}! Теперь можешь общаться со мной."
                    )
                    return
        except Exception:
            logging.exception("Ошибка проверки доступа пользователя %s", user_id)
            await message.reply_text("Не удалось проверить доступ. Попробуй позже.")
            return
    if not authorized:
        await message.reply_text(ACCESS_MESSAGE)
        return
    if user.username:
        try:
            await database.update_authorized_username(user_id, user.username)
        except Exception:
            logging.exception("Не удалось обновить username пользователя %s", user_id)

    try:
        await database.ensure_user(
            user_id,
            message.chat_id,
            user.username,
            user.first_name or user.username or str(user_id),
        )
    except Exception:
        logging.exception("Не удалось обновить глобальный профиль пользователя %s.", user_id)

    profile_request = any(
        phrase in message_text.casefold() for phrase in PROFILE_TRIGGERS
    ) or re.fullmatch(r"/profile(?:@\w+)?", message_text.strip(), re.IGNORECASE) is not None
    if profile_request:
        try:
            profile = await database.get_global_profile(user_id)
            if profile is None:
                raise RuntimeError(f"Глобальный профиль пользователя {user_id} отсутствует.")
            group_ids = await database.get_user_group_ids(user_id)
            sent = await message.reply_text(_format_private_profile(profile, group_ids))
            await database.save_message(
                message.chat_id,
                context.bot.id,
                sent.message_id,
                "Юбара",
                sent.text or "Профиль пользователя",
            )
        except Exception:
            logging.exception("Не удалось показать глобальный профиль пользователя %s.", user_id)
            await message.reply_text("Не удалось открыть профиль. Попробуй позже.")
        return

    if await image_manager.consume_setting_input(update, context):
        return

    await image_manager.refresh_status(message.chat_id, context.bot, owner_id=user.id)

    if await handle_duel_challenge(update, context):
        return
    if await handle_balance_request(update, context):
        return

    if await handle_delete_bot_messages(update, context):
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
        is_image_request, image_prompt = extract_image_request(message_text)
        if is_image_request:
            await image_manager.offer(update, context, image_prompt)
            return
        history = (
            await database.get_history_for_summary(
                message.chat_id,
                hours=12,
            )
            if summary
            else await database.get_recent_history(message.chat_id, DEFAULT_CONTEXT_SIZE)
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
            current_message_id=message.message_id,
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
    reported_age = extract_reported_age(message_text)
    if reported_age is not None:
        metadata = {**(metadata or {}), "target_user_id": user_id, "age": reported_age}
    try:
        await _apply_private_profile_meta(
            metadata,
            user_id=user_id,
            chat_id=message.chat_id,
        )
    except Exception:
        logging.exception("Не удалось сохранить факты личного профиля пользователя %s.", user_id)
    if not answer:
        answer = "Чего уставился? Я просто немного смутилась..."
    try:
        sent = await message.reply_text(answer)
    except TelegramError:
        logging.exception("Не удалось ответить в личном чате")
        return
    await image_manager.refresh_status(message.chat_id, context.bot, owner_id=user.id)
    await maybe_react(message)
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
        try:
            if await _is_admin(user.id):
                return
        except Exception:
            logging.exception("Не удалось проверить право администратора в личном чате")
            return
        ADMIN_PANEL_USERS.discard(user.id)
    await _private_dialog(update, context, message.text)
