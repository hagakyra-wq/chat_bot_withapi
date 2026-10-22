"""Обработчики личных сообщений, ключей доступа и админ-панели."""

import asyncio
from io import BytesIO
import logging
import random
import re
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, ReactionTypeEmoji, Update
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
import autoresponder
import personal_account
from config import ADMIN_ID, AVAILABLE_REACTIONS, DEFAULT_CONTEXT_SIZE, PROFILE_TRIGGERS
from duel import (
    handle_balance_request,
    handle_coin_leaderboard_request,
    handle_duel_challenge,
)
from images import extract_image_request, image_manager
from llm import (
    build_prompt,
    extract_reported_age,
    generate_reply,
    is_summary_request,
    parse_meta,
)
from message_deletion import (
    handle_delete_bot_messages,
    handle_delete_replied_message,
)
from reactions import maybe_react
from voice_transcription import handle_voice_transcription, is_transcription_request

ADMIN_MENU, WAIT_ADD_GROUP, WAIT_REMOVE_GROUP, WAIT_CREATE_KEY, WAIT_REVOKE_KEY = range(5)
WAIT_KEY_DURATION, WAIT_REVOKE_USER, WAIT_REVOKE_ADMIN = range(5, 8)
WAIT_USERBOT_PHONE, WAIT_USERBOT_CODE, WAIT_USERBOT_PASSWORD = range(8, 11)
WAIT_USERBOT_QR = 11
AUTO_MENU, WAIT_AUTO_PHONE, WAIT_AUTO_CODE, WAIT_AUTO_PASSWORD = range(20, 24)
WAIT_AUTO_QR, WAIT_AUTO_TIMEZONE, WAIT_AUTO_TEXT = range(24, 27)
WAIT_ADMIN_NIGHT_TEXT = 27
WAIT_AUTO_PRIVATE_TEXT = 28
WAIT_ADMIN_PRIVATE_TEXT = 29
ADMIN_PANEL_USERS: set[int] = set()
_USERBOT_LOGIN_KEYS = (
    "userbot_phone",
    "userbot_phone_code_hash",
    "userbot_qr_needs_password",
)
_USERBOT_QR_TASK_KEY = "userbot_qr_task"
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


def _admin_keyboard(context_enabled: bool) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    f"🧠 Контекст: {'ВКЛ' if context_enabled else 'ВЫКЛ'}",
                    callback_data="admin:toggle_context",
                )
            ],
            [InlineKeyboardButton("➕ Добавить группу", callback_data="admin:add_group")],
            [InlineKeyboardButton("➖ Удалить группу", callback_data="admin:remove_group")],
            [InlineKeyboardButton("🔑 Ключ обычного доступа", callback_data="admin:create_user_key")],
            [InlineKeyboardButton("👑 Пригласить админа", callback_data="admin:create_admin_key")],
            [InlineKeyboardButton("🎟 Ключ автоответчика", callback_data="admin:create_autoresponder_key")],
            [InlineKeyboardButton("🚫 Отозвать ключ", callback_data="admin:revoke_key")],
            [InlineKeyboardButton("👥 Список людей с доступом", callback_data="admin:people")],
            [InlineKeyboardButton("🚫 Отозвать доступ пользователя", callback_data="admin:revoke_user")],
            [InlineKeyboardButton("🚫 Отозвать админа", callback_data="admin:revoke_admin")],
            [InlineKeyboardButton("👤 Личный Telegram-аккаунт", callback_data="admin:userbot")],
            [InlineKeyboardButton("🌙 Настройки автоответчика", callback_data="admin:autoresponder_settings")],
            [InlineKeyboardButton("🚪 Выйти из админ-панели", callback_data="admin:exit")],
        ]
    )


async def _show_admin_menu(update: Update) -> None:
    query = update.callback_query
    try:
        context_enabled = await database.is_chat_context_enabled()
    except Exception:
        logging.exception("Не удалось загрузить настройку контекста для админ-панели.")
        if query:
            await query.edit_message_text(
                "Не удалось загрузить настройки. Попробуй открыть меню ещё раз."
            )
        elif update.effective_message:
            await update.effective_message.reply_text(
                "Не удалось загрузить настройки. Попробуй открыть меню ещё раз."
            )
        return
    if query:
        await query.edit_message_text(
            "Панель администратора Юбары:",
            reply_markup=_admin_keyboard(context_enabled),
        )
    elif update.effective_message:
        await update.effective_message.reply_text(
            "Панель администратора Юбары:",
            reply_markup=_admin_keyboard(context_enabled),
        )


def _format_utc_offset(offset: int) -> str:
    return f"UTC{offset:+d}"


async def _show_autoresponder_settings(update: Update) -> None:
    timezone_offset, night_enabled, private_enabled = (
        await database.get_autoresponder_settings()
    )
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    f"🌙 Ночная рассылка: {'ВКЛ' if night_enabled else 'ВЫКЛ'}",
                    callback_data="admin:autoresponder_toggle_night",
                )
            ],
            [
                InlineKeyboardButton(
                    f"💬 Автоответ в ЛС: {'ВКЛ' if private_enabled else 'ВЫКЛ'}",
                    callback_data="admin:autoresponder_toggle_private",
                )
            ],
            [
                InlineKeyboardButton(
                    f"🕒 Часовой пояс: {_format_utc_offset(timezone_offset)}",
                    callback_data="admin:autoresponder_select_timezone",
                )
            ],
            [
                InlineKeyboardButton(
                    "👥 Группы ночной рассылки",
                    callback_data="admin:autoresponder_groups",
                )
            ],
            [
                InlineKeyboardButton(
                    "✏️ Текст спокойной ночи",
                    callback_data="admin:night_text_edit",
                )
            ],
            [
                InlineKeyboardButton(
                    "✏️ Текст автоответа в ЛС",
                    callback_data="admin:private_text_edit",
                )
            ],
            [InlineKeyboardButton("↩️ Назад", callback_data="admin:menu")],
        ]
    )
    text = (
        "Настройки автоответчика:\n"
        f"Часовой пояс: {_format_utc_offset(timezone_offset)}\n"
        f"Ночная рассылка: {'включена' if night_enabled else 'выключена'}\n"
        f"Автоответ в ЛС: {'включён' if private_enabled else 'выключен'}"
    )
    query = update.callback_query
    if query is not None:
        await query.edit_message_text(text, reply_markup=keyboard)
    elif update.effective_message is not None:
        await update.effective_message.reply_text(text, reply_markup=keyboard)


async def _show_autoresponder_groups(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    page: int = 0,
) -> None:
    common_groups = await autoresponder.list_common_groups(context.bot)
    common_ids = {group_id for group_id, _ in common_groups}
    selected_ids = set(await database.get_autoresponder_night_groups())
    valid_selected_ids = selected_ids & common_ids
    if valid_selected_ids != selected_ids:
        await database.set_autoresponder_night_groups(sorted(valid_selected_ids))

    page_size = 8
    page_count = max(1, (len(common_groups) + page_size - 1) // page_size)
    page = min(max(page, 0), page_count - 1)
    start = page * page_size
    page_groups = common_groups[start : start + page_size]
    rows = [
        [
            InlineKeyboardButton(
                f"{'✅' if group_id in valid_selected_ids else '⬜'} {title[:45]}",
                callback_data=f"admin:autoresponder_group:{group_id}:{page}",
            )
        ]
        for group_id, title in page_groups
    ]
    navigation: list[InlineKeyboardButton] = []
    if page > 0:
        navigation.append(
            InlineKeyboardButton(
                "⬅️",
                callback_data=f"admin:autoresponder_groups_page:{page - 1}",
            )
        )
    if page + 1 < page_count:
        navigation.append(
            InlineKeyboardButton(
                "➡️",
                callback_data=f"admin:autoresponder_groups_page:{page + 1}",
            )
        )
    if navigation:
        rows.append(navigation)
    rows.append(
        [
            InlineKeyboardButton(
                "↩️ К настройкам",
                callback_data="admin:autoresponder_settings",
            )
        ]
    )
    if common_groups:
        text = (
            "Выбери группы для ночной рассылки.\n"
            "Показаны только группы из списка бота, в которых состоят "
            "и личный аккаунт, и бот Юбара.\n"
            f"Страница {page + 1}/{page_count}; выбрано: {len(valid_selected_ids)}."
        )
    else:
        text = (
            "Нет доступных общих групп. В список попадут только разрешённые "
            "группы, где состоят и личный аккаунт, и бот Юбара."
        )
    query = update.callback_query
    if query is not None:
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(rows))


async def _show_autoresponder_timezones(update: Update) -> None:
    timezone_offset, _, _ = await database.get_autoresponder_settings()
    offsets = range(-12, 15)
    rows = [
        [
            InlineKeyboardButton(
                f"{'✓ ' if offset == timezone_offset else ''}{_format_utc_offset(offset)}",
                callback_data=f"admin:autoresponder_timezone:{offset}",
            )
            for offset in offsets
        ][start : start + 3]
        for start in range(0, len(offsets), 3)
    ]
    rows.append(
        [InlineKeyboardButton("↩️ Назад", callback_data="admin:autoresponder_settings")]
    )
    query = update.callback_query
    if query is not None:
        await query.edit_message_text(
            "Выбери смещение часового пояса относительно UTC:",
            reply_markup=InlineKeyboardMarkup(rows),
        )


async def _show_userbot_menu(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    connected, authorized = personal_account.get_status()
    enabled = personal_account.is_enabled()
    if not personal_account.is_configured():
        status = (
            "Не настроен: задайте TELEGRAM_API_ID и TELEGRAM_API_HASH "
            "в переменных окружения."
        )
    elif authorized:
        status = "Подключён и авторизован." if connected else "Авторизованная сессия отключена."
        status += " Автоматические действия включены." if enabled else " Автоматические действия отключены."
    elif personal_account.session_exists():
        status = "Файл сессии найден, но аккаунт сейчас не подключён."
    else:
        status = "Аккаунт не подключён."

    keyboard = [
        [InlineKeyboardButton("🔐 Подключить / включить", callback_data="admin:userbot_connect")],
        [InlineKeyboardButton("📷 Войти по QR-коду", callback_data="admin:userbot_qr_connect")],
    ]
    if enabled or (connected and authorized):
        keyboard.append(
            [InlineKeyboardButton("⏸ Отключить", callback_data="admin:userbot_disable")]
        )
    keyboard.extend(
        [
            [InlineKeyboardButton("🧹 Сбросить сессию", callback_data="admin:userbot_reset")],
            [InlineKeyboardButton("↩️ Назад", callback_data="admin:menu")],
        ]
    )
    message = f"Личный Telegram-аккаунт:\n{status}"
    query = update.callback_query
    if query is not None:
        await query.edit_message_text(
            message,
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
    elif update.effective_chat is not None:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=message,
            reply_markup=InlineKeyboardMarkup(keyboard),
        )


async def _show_personal_autoresponder_menu(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    user = update.effective_user
    if user is None:
        return
    connected, authorized = personal_account.get_status(user.id)
    enabled = personal_account.is_enabled(user.id)
    timezone_offset, night_enabled, private_enabled = (
        await database.get_autoresponder_settings(user.id)
    )
    night_text = await database.get_autoresponder_night_text(user.id)
    private_text = await database.get_autoresponder_private_text(user.id)
    status = (
        "Аккаунт подключён."
        if connected and authorized
        else "Аккаунт сохранён; подключение появится после запуска бота."
        if personal_account.session_exists(user.id)
        else "Свой Telegram-аккаунт не подключён."
    )
    keyboard = [
        [InlineKeyboardButton("🔐 Подключить по номеру", callback_data="auto:connect_phone")],
        [InlineKeyboardButton("📷 Подключить по QR", callback_data="auto:connect_qr")],
        [
            InlineKeyboardButton(
                f"🌙 Ночная рассылка: {'ВКЛ' if night_enabled else 'ВЫКЛ'}",
                callback_data="auto:toggle_night",
            )
        ],
        [
            InlineKeyboardButton(
                f"💬 Автоответ в ЛС: {'ВКЛ' if private_enabled else 'ВЫКЛ'}",
                callback_data="auto:toggle_private",
            )
        ],
        [
            InlineKeyboardButton(
                f"🕒 Часовой пояс: {_format_utc_offset(timezone_offset)}",
                callback_data="auto:timezone",
            )
        ],
        [InlineKeyboardButton("👥 Группы ночной рассылки", callback_data="auto:groups")],
        [InlineKeyboardButton("✏️ Текст спокойной ночи", callback_data="auto:night_text")],
        [InlineKeyboardButton("✏️ Текст автоответа в ЛС", callback_data="auto:private_text")],
    ]
    if enabled or authorized:
        keyboard.append(
            [InlineKeyboardButton("⏸ Отключить аккаунт", callback_data="auto:disable")]
        )
    if personal_account.session_exists(user.id):
        keyboard.append(
            [InlineKeyboardButton("🧹 Сбросить сессию", callback_data="auto:reset")]
        )
    keyboard.append([InlineKeyboardButton("🚪 Закрыть", callback_data="auto:close")])
    message = (
        "Личный автоответчик (настройки и аккаунт принадлежат только тебе).\n"
        f"{status} Режимы работают, когда аккаунт включён: "
        f"{'да' if enabled else 'нет'}.\n"
        f"Текст ночного сообщения: {night_text}\n"
        f"Текст автоответа в ЛС: {private_text}"
    )
    query = update.callback_query
    if query is not None:
        await query.edit_message_text(message, reply_markup=InlineKeyboardMarkup(keyboard))
    elif update.effective_message is not None:
        await update.effective_message.reply_text(
            message,
            reply_markup=InlineKeyboardMarkup(keyboard),
        )


async def _show_personal_autoresponder_groups(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    page: int = 0,
) -> None:
    common_groups = await autoresponder.list_common_groups(context.bot, user_id)
    common_ids = {group_id for group_id, _ in common_groups}
    selected_ids = set(await database.get_autoresponder_night_groups(user_id))
    selected_ids.intersection_update(common_ids)
    await database.set_autoresponder_night_groups(sorted(selected_ids), user_id)
    page_size = 8
    page_count = max(1, (len(common_groups) + page_size - 1) // page_size)
    page = min(max(page, 0), page_count - 1)
    rows = [
        [
            InlineKeyboardButton(
                f"{'✅' if group_id in selected_ids else '⬜'} {title[:45]}",
                callback_data=f"auto:group:{group_id}:{page}",
            )
        ]
        for group_id, title in common_groups[page * page_size : (page + 1) * page_size]
    ]
    navigation = []
    if page > 0:
        navigation.append(
            InlineKeyboardButton("⬅️", callback_data=f"auto:groups_page:{page - 1}")
        )
    if page + 1 < page_count:
        navigation.append(
            InlineKeyboardButton("➡️", callback_data=f"auto:groups_page:{page + 1}")
        )
    if navigation:
        rows.append(navigation)
    rows.append([InlineKeyboardButton("↩️ В меню", callback_data="auto:menu")])
    text = (
        "Выбери группы, где одновременно состоят твой подключённый аккаунт "
        "и бот Юбара.\n"
        f"Страница {page + 1}/{page_count}; выбрано: {len(selected_ids)}."
        if common_groups
        else "Не найдено общих групп, где состоят и твой аккаунт, и бот Юбара."
    )
    query = update.callback_query
    if query is not None:
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(rows))


async def personal_autoresponder_start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    user = update.effective_user
    if (
        user is None
        or update.effective_chat is None
        or update.effective_chat.type != ChatType.PRIVATE
    ):
        return ConversationHandler.END
    try:
        if not await _has_autoresponder_access(user.id):
            if update.effective_message is not None:
                await update.effective_message.reply_text(
                    "Для доступа к личному автоответчику активируй ключ автоответчика, "
                    "отправив его боту."
                )
            return ConversationHandler.END
        context.user_data["autoresponder_menu"] = True
        await _show_personal_autoresponder_menu(update, context)
        return AUTO_MENU
    except Exception:
        logging.exception("Не удалось открыть автоответчик user_id=%s.", user.id)
        if update.effective_message is not None:
            await update.effective_message.reply_text(
                "Не удалось загрузить меню автоответчика. Попробуй позже."
            )
        return ConversationHandler.END


async def admin_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
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
    context.user_data["autoresponder_menu"] = False
    await _cancel_userbot_login(user.id, context)
    ADMIN_PANEL_USERS.add(user.id)
    await _show_admin_menu(update)
    return ADMIN_MENU


async def _prompt_night_text(
    update: Update,
    user_id: int,
) -> None:
    current_text = await database.get_autoresponder_night_text(user_id)
    text = (
        "Пришли новый текст пожелания спокойной ночи (до 1000 символов).\n\n"
        "Инструкция по форматированию:\n"
        "• Обычный текст отправляется как есть.\n"
        "• Любой фрагмент в круглых скобках, включая сами скобки, "
        "будет скрыт под спойлером.\n"
        "• Можно добавить несколько таких фрагментов.\n\n"
        "Пример: Всем сладких снов! (увидимся завтра)\n"
        "Сейчас сохранено:\n"
        f"{current_text}\n\n"
        "Для отмены отправь /cancel."
    )
    query = update.callback_query
    if query is not None:
        await query.edit_message_text(text)
    elif update.effective_message is not None:
        await update.effective_message.reply_text(text)


async def _prompt_private_text(
    update: Update,
    user_id: int,
) -> None:
    current_text = await database.get_autoresponder_private_text(user_id)
    text = (
        "Пришли новый текст автоответа в личных сообщениях (до 1000 символов).\n\n"
        "Фрагменты в круглых скобках будут скрыты под спойлером.\n"
        "Для отмены отправь /cancel.\n\n"
        "Сейчас сохранено:\n"
        f"{current_text}"
    )
    query = update.callback_query
    if query is not None:
        await query.edit_message_text(text)
    elif update.effective_message is not None:
        await update.effective_message.reply_text(text)


async def _save_night_text_input(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    user = update.effective_user
    message = update.effective_message
    auto_mode = context.user_data.get("autoresponder_menu") is True
    if user is None or message is None or message.text is None:
        return WAIT_AUTO_TEXT if auto_mode else WAIT_ADMIN_NIGHT_TEXT
    if auto_mode:
        if not await _has_autoresponder_access(user.id):
            await message.reply_text("Срок доступа к автоответчику закончился.")
            return ConversationHandler.END
        owner_id = user.id
    else:
        if not await _is_admin(user.id):
            return ConversationHandler.END
        owner_id = ADMIN_ID

    night_text = message.text.strip()
    if not night_text:
        await message.reply_text("Текст не должен быть пустым. Отправь текст ещё раз.")
        return WAIT_AUTO_TEXT if auto_mode else WAIT_ADMIN_NIGHT_TEXT
    if len(night_text) > 1000:
        await message.reply_text("Текст длиннее 1000 символов. Сократи его и отправь ещё раз.")
        return WAIT_AUTO_TEXT if auto_mode else WAIT_ADMIN_NIGHT_TEXT
    try:
        await database.set_autoresponder_night_text(night_text, owner_id)
        await message.reply_text("Текст пожелания спокойной ночи сохранён.")
        if auto_mode:
            await _show_personal_autoresponder_menu(update, context)
            return AUTO_MENU
        await _show_autoresponder_settings(update)
        return ADMIN_MENU
    except Exception:
        logging.exception("Не удалось сохранить текст ночного пожелания user_id=%s.", owner_id)
        await message.reply_text("Не удалось сохранить текст. Попробуй ещё раз.")
        return WAIT_AUTO_TEXT if auto_mode else WAIT_ADMIN_NIGHT_TEXT


async def _save_private_text_input(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    user = update.effective_user
    message = update.effective_message
    auto_mode = context.user_data.get("autoresponder_menu") is True
    retry_state = WAIT_AUTO_PRIVATE_TEXT if auto_mode else WAIT_ADMIN_PRIVATE_TEXT
    if user is None or message is None or message.text is None:
        return retry_state
    if auto_mode:
        if not await _has_autoresponder_access(user.id):
            await message.reply_text("Срок доступа к автоответчику закончился.")
            return ConversationHandler.END
        owner_id = user.id
    else:
        if not await _is_admin(user.id):
            return ConversationHandler.END
        owner_id = ADMIN_ID

    private_text = message.text.strip()
    if not private_text:
        await message.reply_text("Текст не должен быть пустым. Отправь его ещё раз.")
        return retry_state
    if len(private_text) > 1000:
        await message.reply_text("Текст длиннее 1000 символов. Сократи его и отправь ещё раз.")
        return retry_state
    try:
        await database.set_autoresponder_private_text(private_text, owner_id)
        await message.reply_text("Текст автоответа в ЛС сохранён.")
        if auto_mode:
            await _show_personal_autoresponder_menu(update, context)
            return AUTO_MENU
        await _show_autoresponder_settings(update)
        return ADMIN_MENU
    except Exception:
        logging.exception("Не удалось сохранить текст автоответа user_id=%s.", owner_id)
        await message.reply_text("Не удалось сохранить текст. Попробуй ещё раз.")
        return retry_state


async def personal_autoresponder_cancel(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    user = update.effective_user
    if user is not None:
        await _cancel_userbot_login(user.id, context)
    context.user_data["autoresponder_menu"] = False
    if update.effective_message is not None:
        await update.effective_message.reply_text("Настройки автоответчика закрыты.")
    return ConversationHandler.END


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


async def _has_autoresponder_access(user_id: int) -> bool:
    return user_id == ADMIN_ID or await database.is_admin_user(user_id) or (
        await database.is_feature_authorized(user_id, "autoresponder")
    )


async def admin_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    if user:
        ADMIN_PANEL_USERS.discard(user.id)
        await _cancel_userbot_login(user.id, context)
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
        await _cancel_userbot_login(user.id, context)
        await query.edit_message_text("Админ-панель закрыта. Диалоговый режим снова включён.")
        return ConversationHandler.END
    if action == "admin:toggle_context":
        try:
            await database.set_chat_context_enabled(
                not await database.is_chat_context_enabled()
            )
        except Exception:
            logging.exception("Не удалось переключить использование контекста.")
            await query.edit_message_text(
                "Не удалось изменить настройку контекста из-за ошибки базы данных.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("↩️ В меню", callback_data="admin:menu")]]
                ),
            )
            return ADMIN_MENU
        await _show_admin_menu(update)
        return ADMIN_MENU
    if action == "admin:autoresponder_settings":
        try:
            await _show_autoresponder_settings(update)
        except Exception:
            logging.exception("Не удалось загрузить настройки автоответчика.")
            await query.edit_message_text(
                "Не удалось загрузить настройки автоответчика.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("↩️ В меню", callback_data="admin:menu")]]
                ),
            )
        return ADMIN_MENU
    if action == "admin:autoresponder_groups":
        page = 0
    elif action.startswith("admin:autoresponder_groups_page:"):
        try:
            page = int(action.rsplit(":", 1)[1])
        except ValueError:
            page = 0
    elif action.startswith("admin:autoresponder_group:"):
        try:
            _, _, raw_group_id, raw_page = action.split(":")
            group_id = int(raw_group_id)
            page = int(raw_page)
            common_groups = await autoresponder.list_common_groups(context.bot)
            if group_id not in {candidate_id for candidate_id, _ in common_groups}:
                await _show_autoresponder_groups(update, context, page)
                return ADMIN_MENU
            selected_ids = set(await database.get_autoresponder_night_groups())
            if group_id in selected_ids:
                selected_ids.remove(group_id)
            else:
                selected_ids.add(group_id)
            await database.set_autoresponder_night_groups(sorted(selected_ids))
        except (ValueError, TypeError):
            logging.exception("Получен некорректный выбор группы авторассылки.")
            await query.edit_message_text(
                "Некорректный выбор группы.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("↩️ К настройкам", callback_data="admin:autoresponder_settings")]]
                ),
            )
            return ADMIN_MENU
        except Exception:
            logging.exception("Не удалось изменить выбор группы авторассылки.")
            await query.edit_message_text(
                "Не удалось изменить список групп авторассылки.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("↩️ К настройкам", callback_data="admin:autoresponder_settings")]]
                ),
            )
            return ADMIN_MENU
    else:
        page = None
    if page is not None:
        try:
            await _show_autoresponder_groups(update, context, page)
        except Exception:
            logging.exception("Не удалось загрузить общие группы авторассылки.")
            await query.edit_message_text(
                "Не удалось загрузить группы. Убедись, что личный аккаунт "
                "подключён и бот имеет доступ к разрешённым группам.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("↩️ К настройкам", callback_data="admin:autoresponder_settings")]]
                ),
            )
        return ADMIN_MENU
    if action == "admin:autoresponder_select_timezone":
        try:
            await _show_autoresponder_timezones(update)
        except Exception:
            logging.exception("Не удалось загрузить список часовых поясов.")
            await query.edit_message_text(
                "Не удалось загрузить часовой пояс.",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "↩️ К настройкам",
                                callback_data="admin:autoresponder_settings",
                            )
                        ]
                    ]
                ),
            )
        return ADMIN_MENU
    if action == "admin:night_text_edit":
        try:
            await _prompt_night_text(update, ADMIN_ID)
        except Exception:
            logging.exception("Не удалось загрузить текст ночного пожелания.")
            await query.edit_message_text(
                "Не удалось загрузить текст. Попробуй открыть настройки ещё раз.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("↩️ К настройкам", callback_data="admin:autoresponder_settings")]]
                ),
            )
            return ADMIN_MENU
        return WAIT_ADMIN_NIGHT_TEXT
    if action == "admin:private_text_edit":
        try:
            await _prompt_private_text(update, ADMIN_ID)
        except Exception:
            logging.exception("Не удалось загрузить текст автоответа в ЛС.")
            await query.edit_message_text(
                "Не удалось загрузить текст. Попробуй открыть настройки ещё раз.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("↩️ К настройкам", callback_data="admin:autoresponder_settings")]]
                ),
            )
            return ADMIN_MENU
        return WAIT_ADMIN_PRIVATE_TEXT
    if action.startswith("admin:autoresponder_timezone:"):
        raw_offset = action.removeprefix("admin:autoresponder_timezone:")
        try:
            offset = int(raw_offset)
            await database.set_autoresponder_timezone_offset(offset)
            await _show_autoresponder_timezones(update)
        except (ValueError, TypeError):
            logging.exception("Получено некорректное смещение часового пояса: %r.", raw_offset)
            await query.edit_message_text(
                "Некорректное смещение часового пояса.",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "↩️ К настройкам",
                                callback_data="admin:autoresponder_settings",
                            )
                        ]
                    ]
                ),
            )
        except Exception:
            logging.exception("Не удалось сохранить часовой пояс автоответчика.")
            await query.edit_message_text(
                "Не удалось сохранить часовой пояс.",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "↩️ К настройкам",
                                callback_data="admin:autoresponder_settings",
                            )
                        ]
                    ]
                ),
            )
        return ADMIN_MENU
    if action in (
        "admin:autoresponder_toggle_night",
        "admin:autoresponder_toggle_private",
    ):
        try:
            _, night_enabled, private_enabled = (
                await database.get_autoresponder_settings()
            )
            if action == "admin:autoresponder_toggle_night":
                await database.set_autoresponder_night_enabled(not night_enabled)
            else:
                await database.set_autoresponder_private_enabled(not private_enabled)
            await _show_autoresponder_settings(update)
        except Exception:
            logging.exception("Не удалось изменить настройку автоответчика.")
            await query.edit_message_text(
                "Не удалось изменить настройку автоответчика.",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "↩️ К настройкам",
                                callback_data="admin:autoresponder_settings",
                            )
                        ]
                    ]
                ),
            )
        return ADMIN_MENU
    if action == "admin:userbot":
        await _show_userbot_menu(update, context)
        return ADMIN_MENU
    if action == "admin:userbot_connect":
        if not personal_account.is_configured():
            await query.edit_message_text(
                "Для подключения задайте TELEGRAM_API_ID и TELEGRAM_API_HASH "
                "в переменных окружения.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("↩️ Назад", callback_data="admin:userbot")]]
                ),
            )
            return ADMIN_MENU
        try:
            if await personal_account.connect_saved_session():
                personal_account.set_enabled(True)
                await autoresponder.refresh_user_client()
                await _show_userbot_menu(update, context)
                return ADMIN_MENU
        except Exception:
            logging.exception("Не удалось проверить сохранённую user-сессию.")
            await query.edit_message_text(
                "Не удалось подключить сохранённую сессию. Проверьте настройки и повторите попытку.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("↩️ Назад", callback_data="admin:userbot")]]
                ),
            )
            return ADMIN_MENU
        await query.edit_message_text(
            "Отправь номер телефона аккаунта в международном формате "
            "(например, +79991234567). Сообщение с номером будет удалено. "
            "Если код не приходит, можно подключиться по QR.",
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("📷 Войти по QR-коду", callback_data="admin:userbot_qr_connect")],
                    [InlineKeyboardButton("Отмена", callback_data="admin:userbot_cancel")],
                ]
            ),
        )
        return WAIT_USERBOT_PHONE
    if action == "admin:userbot_qr_connect":
        if not personal_account.is_configured():
            await query.edit_message_text(
                "Для QR-входа задайте TELEGRAM_API_ID и TELEGRAM_API_HASH "
                "в переменных окружения.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("↩️ Назад", callback_data="admin:userbot")]]
                ),
            )
            return ADMIN_MENU
        await _cancel_userbot_login(user.id, context)
        try:
            if await personal_account.connect_saved_session():
                personal_account.set_enabled(True)
                await autoresponder.refresh_user_client()
                await _show_userbot_menu(update, context)
                return ADMIN_MENU
        except Exception:
            logging.exception("Не удалось подключить сохранённую user-сессию.")
            await query.edit_message_text(
                "Не удалось подключить сохранённую сессию. Проверьте настройки и повторите попытку.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("↩️ Назад", callback_data="admin:userbot")]]
                ),
            )
            return ADMIN_MENU
        try:
            qr_login = await personal_account.begin_qr_login(user.id)
        except personal_account.AuthFlowBusyError:
            await query.edit_message_text(
                "Другой администратор уже проходит авторизацию. Попробуй позже."
            )
            return ADMIN_MENU
        except Exception:
            logging.exception("Не удалось создать QR-код для входа user-аккаунта.")
            await query.edit_message_text(
                "Не удалось создать QR-код. Проверьте настройки Telegram API и повторите попытку.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("↩️ Назад", callback_data="admin:userbot")]]
                ),
            )
            return ADMIN_MENU
        if qr_login is None:
            personal_account.set_enabled(True)
            await autoresponder.refresh_user_client()
            await _show_userbot_menu(update, context)
            return ADMIN_MENU
        qr_url = qr_login.url
        context.user_data["userbot_qr_needs_password"] = False
        task = context.application.create_task(
            _wait_for_userbot_qr_login(user.id, context),
            update=update,
        )
        context.user_data[_USERBOT_QR_TASK_KEY] = task
        try:
            qr_message = await _send_userbot_qr(context, user.id, qr_url)
            try:
                active_qr_url = personal_account.get_qr_login(user.id).url
            except RuntimeError:
                active_qr_url = None
            if (
                task.done()
                or personal_account.get_status()[1]
                or active_qr_url != qr_url
            ):
                try:
                    await qr_message.delete()
                except TelegramError:
                    logging.info("Не удалось удалить устаревший QR-код.")
            else:
                context.user_data["userbot_qr_message"] = qr_message
        except Exception:
            logging.exception("Не удалось отправить QR-код администратору.")
            await _cancel_userbot_login(user.id, context)
            return ADMIN_MENU
        return WAIT_USERBOT_QR
    if action == "admin:userbot_disable":
        try:
            personal_account.set_enabled(False)
            await autoresponder.stop_user_client()
            await personal_account.disconnect()
        except Exception:
            logging.exception("Не удалось отключить user-аккаунт.")
            await query.edit_message_text(
                "Не удалось отключить аккаунт. Проверьте журнал ошибок.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("↩️ Назад", callback_data="admin:userbot")]]
                ),
            )
            return ADMIN_MENU
        await _show_userbot_menu(update, context)
        return ADMIN_MENU
    if action == "admin:userbot_reset":
        await query.edit_message_text(
            "Сбросить сессию? Файл авторизации будет удалён, и аккаунт потребуется подключить заново.",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "Да, сбросить",
                            callback_data="admin:userbot_reset_confirm",
                        )
                    ],
                    [InlineKeyboardButton("↩️ Отмена", callback_data="admin:userbot")],
                ]
            ),
        )
        return ADMIN_MENU
    if action == "admin:userbot_reset_confirm":
        try:
            personal_account.set_enabled(False)
            await autoresponder.stop_user_client()
            removed = await personal_account.reset_session()
        except Exception:
            logging.exception("Не удалось сбросить user-сессию.")
            await query.edit_message_text(
                "Не удалось сбросить сессию. Автоматическое подключение отключено; "
                "проверьте журнал ошибок.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("↩️ Назад", callback_data="admin:userbot")]]
                ),
            )
            return ADMIN_MENU
        logging.info("Сброшена user-сессия; удалено файлов: %s.", len(removed))
        await _show_userbot_menu(update, context)
        return ADMIN_MENU
    if action == "admin:userbot_cancel":
        await _cancel_userbot_login(user.id, context)
        await _show_userbot_menu(update, context)
        return ADMIN_MENU
    if action == "admin:userbot_qr_done":
        await _show_userbot_menu(update, context)
        return ADMIN_MENU
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
    if action in (
        "admin:create_key",
        "admin:create_user_key",
        "admin:create_admin_key",
        "admin:create_autoresponder_key",
    ):
        if action == "admin:create_admin_key":
            access_level = "admin"
        elif action == "admin:create_autoresponder_key":
            access_level = "autoresponder"
        else:
            access_level = "user"
        context.user_data["new_key_access_level"] = access_level
        level_label = {
            "admin": "админский",
            "autoresponder": "автоответчика",
            "user": "пользовательский",
        }[access_level]
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
                    f"{ {'admin': 'Админ', 'autoresponder': 'Автоответчик', 'user': 'Доступ'}.get(str(key['access_level']), 'Доступ') }: {key['key'][:30]}",
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


async def personal_autoresponder_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    query = update.callback_query
    user = update.effective_user
    if query is None or user is None:
        return ConversationHandler.END
    await query.answer()
    if not await _has_autoresponder_access(user.id):
        await _cancel_userbot_login(user.id, context)
        await query.edit_message_text("Срок доступа к автоответчику закончился.")
        return ConversationHandler.END

    action = query.data or ""
    if action == "auto:close":
        await _cancel_userbot_login(user.id, context)
        await query.edit_message_text("Меню автоответчика закрыто.")
        return ConversationHandler.END
    if action == "auto:menu":
        try:
            await _show_personal_autoresponder_menu(update, context)
        except Exception:
            logging.exception("Не удалось показать меню автоответчика user_id=%s.", user.id)
            await query.edit_message_text("Не удалось загрузить настройки автоответчика.")
            return ConversationHandler.END
        return AUTO_MENU
    if action == "auto:connect_phone":
        try:
            if await personal_account.connect_saved_session(user.id):
                personal_account.set_enabled(True, user.id)
                await autoresponder.refresh_user_client(user.id)
                await _show_personal_autoresponder_menu(update, context)
                return AUTO_MENU
        except Exception:
            logging.exception("Не удалось проверить сохранённую сессию user_id=%s.", user.id)
            await query.edit_message_text("Не удалось подключить сохранённую сессию.")
            return AUTO_MENU
        await query.edit_message_text(
            "Отправь свой номер в международном формате (+79991234567). "
            "Сообщение будет удалено до обработки.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("Отмена", callback_data="auto:menu")]]
            ),
        )
        return WAIT_AUTO_PHONE
    if action == "auto:connect_qr":
        await _cancel_userbot_login(user.id, context)
        if not personal_account.is_configured():
            await query.edit_message_text(
                "Подключение сейчас недоступно: администратор не настроил Telegram API."
            )
            return AUTO_MENU
        try:
            qr_login = await personal_account.begin_qr_login(user.id)
        except personal_account.AuthFlowBusyError:
            await query.edit_message_text("Вход уже выполняется. Попробуй позже.")
            return AUTO_MENU
        except Exception:
            logging.exception("Не удалось создать QR для user_id=%s.", user.id)
            await query.edit_message_text("Не удалось создать QR-код. Попробуй позже.")
            return AUTO_MENU
        if qr_login is None:
            personal_account.set_enabled(True, user.id)
            await autoresponder.refresh_user_client(user.id)
            await _show_personal_autoresponder_menu(update, context)
            return AUTO_MENU
        context.user_data["userbot_qr_needs_password"] = False
        task = context.application.create_task(
            _wait_for_userbot_qr_login(user.id, context, callback_prefix="auto"),
            update=update,
        )
        context.user_data[_USERBOT_QR_TASK_KEY] = task
        try:
            qr_message = await _send_userbot_qr(
                context,
                user.id,
                qr_login.url,
                callback_prefix="auto",
            )
            if task.done() or personal_account.get_status(user.id)[1]:
                try:
                    await qr_message.delete()
                except TelegramError:
                    logging.info("Не удалось удалить устаревший QR user_id=%s.", user.id)
            else:
                context.user_data["userbot_qr_message"] = qr_message
        except Exception:
            logging.exception("Не удалось отправить QR user_id=%s.", user.id)
            await _cancel_userbot_login(user.id, context)
            await query.edit_message_text("Не удалось показать QR-код. Попробуй ещё раз.")
            return AUTO_MENU
        return WAIT_AUTO_QR
    if action == "auto:disable":
        try:
            personal_account.set_enabled(False, user.id)
            await autoresponder.stop_user_client(user.id)
            await personal_account.disconnect(user.id)
            await _show_personal_autoresponder_menu(update, context)
        except Exception:
            logging.exception("Не удалось отключить личный автоответчик user_id=%s.", user.id)
            await query.edit_message_text("Не удалось отключить аккаунт.")
        return AUTO_MENU
    if action == "auto:reset":
        try:
            personal_account.set_enabled(False, user.id)
            await autoresponder.stop_user_client(user.id)
            await personal_account.reset_session(user.id)
            await _show_personal_autoresponder_menu(update, context)
        except Exception:
            logging.exception("Не удалось сбросить user-сессию user_id=%s.", user.id)
            await query.edit_message_text("Не удалось сбросить сессию.")
        return AUTO_MENU
    if action in ("auto:toggle_night", "auto:toggle_private"):
        try:
            _, night_enabled, private_enabled = (
                await database.get_autoresponder_settings(user.id)
            )
            if action == "auto:toggle_night":
                await database.set_autoresponder_night_enabled(
                    not night_enabled,
                    user.id,
                )
            else:
                await database.set_autoresponder_private_enabled(
                    not private_enabled,
                    user.id,
                )
            await _show_personal_autoresponder_menu(update, context)
        except Exception:
            logging.exception("Не удалось изменить настройки user_id=%s.", user.id)
            await query.edit_message_text("Не удалось изменить настройку.")
        return AUTO_MENU
    if action == "auto:timezone":
        offset, _, _ = await database.get_autoresponder_settings(user.id)
        offsets = range(-12, 15)
        rows = [
            [
                InlineKeyboardButton(
                    f"{'✓ ' if item == offset else ''}{_format_utc_offset(item)}",
                    callback_data=f"auto:timezone_set:{item}",
                )
                for item in list(offsets)[start : start + 3]
            ]
            for start in range(0, 27, 3)
        ]
        rows.append([InlineKeyboardButton("↩️ В меню", callback_data="auto:menu")])
        await query.edit_message_text(
            "Выбери часовой пояс автоответчика:",
            reply_markup=InlineKeyboardMarkup(rows),
        )
        return WAIT_AUTO_TIMEZONE
    if action.startswith("auto:timezone_set:"):
        try:
            offset = int(action.rsplit(":", 1)[1])
            await database.set_autoresponder_timezone_offset(offset, user.id)
            await _show_personal_autoresponder_menu(update, context)
        except (ValueError, TypeError):
            await query.edit_message_text("Неверное значение часового пояса.")
        except Exception:
            logging.exception("Не удалось сохранить часовой пояс user_id=%s.", user.id)
            await query.edit_message_text("Не удалось сохранить часовой пояс.")
        return AUTO_MENU
    if action == "auto:groups":
        page = 0
    elif action.startswith("auto:groups_page:"):
        try:
            page = int(action.rsplit(":", 1)[1])
        except ValueError:
            page = 0
    elif action.startswith("auto:group:"):
        try:
            _, _, raw_group_id, raw_page = action.split(":")
            group_id, page = int(raw_group_id), int(raw_page)
            groups = await autoresponder.list_common_groups(context.bot, user.id)
            if group_id not in {candidate for candidate, _ in groups}:
                await _show_personal_autoresponder_groups(
                    update,
                    context,
                    user.id,
                    page,
                )
                return AUTO_MENU
            selected = set(await database.get_autoresponder_night_groups(user.id))
            if group_id in selected:
                selected.remove(group_id)
            else:
                selected.add(group_id)
            await database.set_autoresponder_night_groups(sorted(selected), user.id)
        except (ValueError, TypeError):
            logging.exception("Некорректный выбор группы автоответчика user_id=%s.", user.id)
            await query.edit_message_text("Не удалось прочитать выбранную группу.")
            return AUTO_MENU
        except Exception:
            logging.exception("Не удалось изменить группы автоответчика user_id=%s.", user.id)
            await query.edit_message_text("Не удалось изменить список групп.")
            return AUTO_MENU
    else:
        page = None
    if page is not None:
        try:
            await _show_personal_autoresponder_groups(
                update,
                context,
                user.id,
                page,
            )
        except Exception:
            logging.exception("Не удалось загрузить группы user_id=%s.", user.id)
            await query.edit_message_text(
                "Не удалось загрузить группы. Убедись, что твой аккаунт подключён."
            )
        return AUTO_MENU
    if action == "auto:night_text":
        try:
            await _prompt_night_text(update, user.id)
        except Exception:
            logging.exception("Не удалось загрузить текст автоответчика user_id=%s.", user.id)
            await query.edit_message_text("Не удалось загрузить текст.")
            return AUTO_MENU
        return WAIT_AUTO_TEXT
    if action == "auto:private_text":
        try:
            await _prompt_private_text(update, user.id)
        except Exception:
            logging.exception("Не удалось загрузить текст автоответа user_id=%s.", user.id)
            await query.edit_message_text("Не удалось загрузить текст.")
            return AUTO_MENU
        return WAIT_AUTO_PRIVATE_TEXT
    return AUTO_MENU


def _clear_userbot_login(context: ContextTypes.DEFAULT_TYPE) -> None:
    for key in _USERBOT_LOGIN_KEYS:
        context.user_data.pop(key, None)


async def _cancel_userbot_login(
    user_id: int,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    task = context.user_data.pop(_USERBOT_QR_TASK_KEY, None)
    if isinstance(task, asyncio.Task) and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    await personal_account.cancel_login(user_id)
    _clear_userbot_login(context)
    await _delete_userbot_qr_message(context)


async def _delete_userbot_qr_message(context: ContextTypes.DEFAULT_TYPE) -> None:
    qr_message = context.user_data.pop("userbot_qr_message", None)
    if not isinstance(qr_message, Message):
        return
    try:
        await qr_message.delete()
    except TelegramError:
        logging.info("Не удалось удалить сообщение с QR после завершения входа.")


def _make_userbot_qr_image(url: str) -> BytesIO:
    import qrcode

    image = qrcode.make(url)
    photo = BytesIO()
    image.save(photo, format="PNG")
    photo.seek(0)
    photo.name = "telegram_login_qr.png"
    return photo


async def _send_userbot_qr(
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    url: str,
    callback_prefix: str = "admin",
) -> Message:
    photo = await asyncio.to_thread(_make_userbot_qr_image, url)
    try:
        return await context.bot.send_photo(
            chat_id=user_id,
            photo=photo,
            caption=(
                "QR для входа в личный Telegram-аккаунт. В приложении Telegram "
                "на другом устройстве откройте Настройки → Устройства → "
                "Подключить устройство и отсканируйте этот код. "
                "Окно входа открыто; при истечении срока код обновится автоматически. "
                "Не пересылайте QR и не показывайте его другим людям."
            ),
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "Отмена",
                            callback_data=(
                                "auto:menu"
                                if callback_prefix == "auto"
                                else "admin:userbot_cancel"
                            ),
                        )
                    ]
                ]
            ),
        )
    finally:
        photo.close()


async def _wait_for_userbot_qr_login(
    user_id: int,
    context: ContextTypes.DEFAULT_TYPE,
    callback_prefix: str = "admin",
) -> None:
    current_task = asyncio.current_task()
    try:
        while True:
            try:
                await personal_account.wait_for_qr_login(user_id)
                personal_account.set_enabled(True, user_id)
                await autoresponder.refresh_user_client(user_id)
                await _delete_userbot_qr_message(context)
                _clear_userbot_login(context)
                await context.bot.send_message(
                    chat_id=user_id,
                    text="Личный Telegram-аккаунт подключён по QR и включён.",
                    reply_markup=InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton(
                                    "Готово",
                                    callback_data=(
                                        "auto:menu"
                                        if callback_prefix == "auto"
                                        else "admin:userbot_qr_done"
                                    ),
                                )
                            ]
                        ]
                    ),
                )
                return
            except asyncio.TimeoutError:
                await _delete_userbot_qr_message(context)
                qr_login = await personal_account.recreate_qr_login(user_id)
                qr_message = await _send_userbot_qr(
                    context,
                    user_id,
                    qr_login.url,
                    callback_prefix,
                )
                context.user_data["userbot_qr_message"] = qr_message
            except personal_account.TwoFactorRequiredError:
                context.user_data["userbot_qr_needs_password"] = True
                await context.bot.send_message(
                    chat_id=user_id,
                    text=(
                        "Telegram запросил пароль 2FA. Отправь его следующим "
                        "сообщением; бот удалит сообщение до проверки."
                    ),
                    reply_markup=InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton(
                                    "Отмена",
                                    callback_data=(
                                        "auto:menu"
                                        if callback_prefix == "auto"
                                        else "admin:userbot_cancel"
                                    ),
                                )
                            ]
                        ]
                    ),
                )
                return
    except asyncio.CancelledError:
        raise
    except Exception:
        logging.exception("Не удалось завершить авторизацию user-аккаунта по QR.")
        try:
            await autoresponder.stop_user_client(user_id)
            personal_account.set_enabled(False, user_id)
            await personal_account.disconnect(user_id)
        except Exception:
            logging.exception("Не удалось отключить частично подключённый QR-сеанс.")
        await personal_account.cancel_login(user_id)
        await _delete_userbot_qr_message(context)
        _clear_userbot_login(context)
        await context.bot.send_message(
            chat_id=user_id,
            text=(
                "Не удалось завершить вход по QR. Начни подключение заново через "
                + ("/addacc." if callback_prefix == "auto" else "/adm.")
            ),
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            (
                                "Открыть меню автоответчика"
                                if callback_prefix == "auto"
                                else "Открыть админ-панель"
                            ),
                            callback_data=(
                                "auto:menu"
                                if callback_prefix == "auto"
                                else "admin:userbot_qr_done"
                            ),
                        )
                    ]
                ]
            ),
        )
    finally:
        if context.user_data.get(_USERBOT_QR_TASK_KEY) is current_task:
            context.user_data.pop(_USERBOT_QR_TASK_KEY, None)


async def _consume_userbot_secret(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    description: str,
) -> str | None:
    message = update.effective_message
    if message is None or message.text is None:
        return None
    secret = message.text
    try:
        await message.delete()
    except TelegramError:
        logging.exception(
            "Не удалось удалить сообщение с данными авторизации user-аккаунта "
            "(message_id=%s).",
            message.message_id,
        )
        user = update.effective_user
        if user is not None:
            await _cancel_userbot_login(user.id, context)
        if update.effective_chat is not None:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=(
                    f"Не удалось удалить сообщение {description}. "
                    "Данные не были отправлены Telegram для авторизации; "
                    "проверь права бота и начни подключение заново."
                ),
            )
        return None
    return secret


async def _userbot_phone_input(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    user = update.effective_user
    if user is None or not await _has_autoresponder_access(user.id):
        return ConversationHandler.END
    auto_mode = context.user_data.get("autoresponder_menu") is True
    menu_state = AUTO_MENU if auto_mode else ADMIN_MENU
    phone_state = WAIT_AUTO_PHONE if auto_mode else WAIT_USERBOT_PHONE
    code_state = WAIT_AUTO_CODE if auto_mode else WAIT_USERBOT_CODE
    raw_phone = await _consume_userbot_secret(update, context, "номером телефона")
    if raw_phone is None:
        return menu_state
    phone = re.sub(r"[\s()-]", "", raw_phone)
    if not re.fullmatch(r"\+[1-9]\d{6,14}", phone):
        await context.bot.send_message(
            chat_id=user.id,
            text="Нужен номер в международном формате, например +79991234567.",
        )
        return phone_state
    try:
        phone_code_hash = await personal_account.begin_login(user.id, phone)
    except personal_account.AuthFlowBusyError:
        await context.bot.send_message(
            chat_id=user.id,
            text="Другой администратор уже проходит авторизацию. Попробуй позже.",
        )
        return menu_state
    except Exception:
        logging.exception("Не удалось запросить код авторизации user-аккаунта.")
        await personal_account.cancel_login(user.id)
        await context.bot.send_message(
            chat_id=user.id,
            text="Не удалось запросить код Telegram. Проверь настройки и повтори подключение.",
        )
        return menu_state
    if phone_code_hash is None:
        try:
            personal_account.set_enabled(True, user.id)
            await autoresponder.refresh_user_client(user.id)
        except Exception:
            logging.exception("Не удалось сохранить включение user-аккаунта.")
            await autoresponder.stop_user_client(user.id)
            await personal_account.disconnect(user.id)
            await context.bot.send_message(
                chat_id=user.id,
                text="Аккаунт авторизован, но настройку не удалось сохранить; сессия отключена.",
            )
            return menu_state
        await context.bot.send_message(
            chat_id=user.id,
            text="Сохранённая сессия уже авторизована и включена.",
        )
        if auto_mode:
            await _show_personal_autoresponder_menu(update, context)
        else:
            await _show_userbot_menu(update, context)
        return menu_state

    context.user_data["userbot_phone"] = phone
    context.user_data["userbot_phone_code_hash"] = phone_code_hash
    await context.bot.send_message(
        chat_id=user.id,
        text=(
            "Telegram отправил код способом: "
            f"{personal_account.get_code_delivery_hint(user.id)}.\n"
            "Проверь официальный чат Telegram на уже авторизованном устройстве "
            "или указанное место доставки (не чат с ботом). "
            "Введённое сюда сообщение с кодом бот удалит до проверки. "
            "Если код недоступен, выбери вход по QR."
        ),
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "📷 Войти по QR-коду",
                        callback_data="auto:connect_qr" if auto_mode else "admin:userbot_qr_connect",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "Отмена",
                        callback_data="auto:menu" if auto_mode else "admin:userbot_cancel",
                    )
                ],
            ]
        ),
    )
    return code_state


async def _userbot_code_input(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    user = update.effective_user
    if user is None or not await _has_autoresponder_access(user.id):
        return ConversationHandler.END
    auto_mode = context.user_data.get("autoresponder_menu") is True
    menu_state = AUTO_MENU if auto_mode else ADMIN_MENU
    code_state = WAIT_AUTO_CODE if auto_mode else WAIT_USERBOT_CODE
    password_state = WAIT_AUTO_PASSWORD if auto_mode else WAIT_USERBOT_PASSWORD
    code = await _consume_userbot_secret(update, context, "кодом авторизации")
    if code is None:
        return menu_state
    code = re.sub(r"\s+", "", code)
    if not re.fullmatch(r"\d{4,8}", code):
        await context.bot.send_message(
            chat_id=user.id,
            text="Код должен состоять из 4–8 цифр. Отправь код ещё раз.",
        )
        return code_state
    phone = context.user_data.get("userbot_phone")
    phone_code_hash = context.user_data.get("userbot_phone_code_hash")
    if not isinstance(phone, str) or not isinstance(phone_code_hash, str):
        _clear_userbot_login(context)
        await context.bot.send_message(
            chat_id=user.id,
            text="Шаг авторизации завершился. Начни подключение заново через /addacc.",
        )
        return menu_state
    try:
        await personal_account.submit_login_code(
            user.id,
            phone,
            code,
            phone_code_hash,
        )
    except personal_account.TwoFactorRequiredError:
        await context.bot.send_message(
            chat_id=user.id,
            text=(
                "Для аккаунта включена двухфакторная защита. Отправь пароль 2FA; "
                "сообщение будет удалено до проверки."
            ),
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("Отмена", callback_data="admin:userbot_cancel")]]
            ),
        )
        return password_state
    except personal_account.InvalidLoginCodeError:
        await context.bot.send_message(
            chat_id=user.id,
            text="Telegram отклонил код. Проверь его и отправь новый код.",
        )
        return code_state
    except personal_account.ExpiredLoginCodeError:
        await personal_account.cancel_login(user.id)
        _clear_userbot_login(context)
        await context.bot.send_message(
            chat_id=user.id,
            text="Срок действия кода истёк. Начни подключение заново.",
        )
        return menu_state
    except Exception:
        logging.exception("Не удалось завершить вход user-аккаунта по коду.")
        await context.bot.send_message(
            chat_id=user.id,
            text="Не удалось проверить код. Повтори попытку или отмени авторизацию.",
        )
        return code_state

    _clear_userbot_login(context)
    try:
        personal_account.set_enabled(True, user.id)
        await autoresponder.refresh_user_client(user.id)
    except Exception:
        logging.exception("Не удалось сохранить включение user-аккаунта.")
        await autoresponder.stop_user_client(user.id)
        await personal_account.disconnect(user.id)
        await context.bot.send_message(
            chat_id=user.id,
            text="Аккаунт авторизован, но настройку не удалось сохранить; сессия отключена.",
        )
        return menu_state
    await context.bot.send_message(chat_id=user.id, text="Telegram-аккаунт подключён.")
    if auto_mode:
        await _show_personal_autoresponder_menu(update, context)
    else:
        await _show_userbot_menu(update, context)
    return menu_state


async def _userbot_password_input(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    user = update.effective_user
    if user is None or not await _has_autoresponder_access(user.id):
        return ConversationHandler.END
    auto_mode = context.user_data.get("autoresponder_menu") is True
    menu_state = AUTO_MENU if auto_mode else ADMIN_MENU
    password_state = WAIT_AUTO_PASSWORD if auto_mode else WAIT_USERBOT_PASSWORD
    password = await _consume_userbot_secret(update, context, "паролем 2FA")
    if password is None:
        return menu_state
    if not password:
        await context.bot.send_message(
            chat_id=user.id,
            text="Пароль 2FA пустой. Отправь пароль ещё раз.",
        )
        return password_state
    try:
        await personal_account.submit_two_factor_password(user.id, password)
    except personal_account.InvalidTwoFactorPasswordError:
        await context.bot.send_message(
            chat_id=user.id,
            text="Telegram отклонил пароль 2FA. Проверь его и отправь ещё раз.",
        )
        return password_state
    except Exception:
        logging.exception("Не удалось завершить вход user-аккаунта с паролем 2FA.")
        await context.bot.send_message(
            chat_id=user.id,
            text="Не удалось проверить пароль 2FA. Повтори попытку или отмени авторизацию.",
        )
        return password_state

    _clear_userbot_login(context)
    try:
        personal_account.set_enabled(True, user.id)
        await autoresponder.refresh_user_client(user.id)
    except Exception:
        logging.exception("Не удалось сохранить включение user-аккаунта.")
        await autoresponder.stop_user_client(user.id)
        await personal_account.disconnect(user.id)
        await context.bot.send_message(
            chat_id=user.id,
            text="Аккаунт авторизован, но настройку не удалось сохранить; сессия отключена.",
        )
        return menu_state
    await context.bot.send_message(chat_id=user.id, text="Telegram-аккаунт подключён.")
    if auto_mode:
        await _show_personal_autoresponder_menu(update, context)
    else:
        await _show_userbot_menu(update, context)
    return menu_state


async def _userbot_qr_input(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    user = update.effective_user
    if user is None or not await _has_autoresponder_access(user.id):
        return ConversationHandler.END
    auto_mode = context.user_data.get("autoresponder_menu") is True
    qr_state = WAIT_AUTO_QR if auto_mode else WAIT_USERBOT_QR
    if context.user_data.get("userbot_qr_needs_password") is True:
        return await _userbot_password_input(update, context)
    if personal_account.get_status(user.id)[1]:
        await context.bot.send_message(
            chat_id=user.id,
            text="QR-вход завершён. Нажми «Готово», чтобы открыть меню аккаунта.",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "Готово",
                            callback_data="auto:menu" if auto_mode else "admin:userbot_qr_done",
                        )
                    ]
                ]
            ),
        )
        return qr_state
    await context.bot.send_message(
        chat_id=user.id,
        text=(
            "Вводить текст не нужно: открой QR в этом чате на другом устройстве "
            "и отсканируй его через Telegram → Настройки → Устройства → "
            "Подключить устройство."
        ),
    )
    return qr_state


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
    if len(parts) != 4 or parts[2] not in ("user", "admin", "autoresponder"):
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
    access_label = {
        "admin": "админское приглашение",
        "autoresponder": "ключ автоответчика",
        "user": "ключ доступа",
    }[access_level]
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
            WAIT_USERBOT_PHONE: [
                CallbackQueryHandler(
                    admin_menu_callback,
                    pattern=r"^admin:userbot_(?:cancel|qr_connect)$",
                ),
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    _userbot_phone_input,
                ),
            ],
            WAIT_USERBOT_CODE: [
                CallbackQueryHandler(
                    admin_menu_callback,
                    pattern=r"^admin:userbot_(?:cancel|qr_connect)$",
                ),
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    _userbot_code_input,
                ),
            ],
            WAIT_USERBOT_PASSWORD: [
                CallbackQueryHandler(
                    admin_menu_callback,
                    pattern=r"^admin:userbot_cancel$",
                ),
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    _userbot_password_input,
                ),
            ],
            WAIT_USERBOT_QR: [
                CallbackQueryHandler(
                    admin_menu_callback,
                    pattern=r"^admin:userbot_(?:cancel|qr_done)$",
                ),
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    _userbot_qr_input,
                ),
            ],
            WAIT_ADMIN_NIGHT_TEXT: [
                CallbackQueryHandler(
                    admin_menu_callback,
                    pattern=r"^admin:autoresponder_settings$",
                ),
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    _save_night_text_input,
                ),
            ],
            WAIT_ADMIN_PRIVATE_TEXT: [
                CallbackQueryHandler(
                    admin_menu_callback,
                    pattern=r"^admin:autoresponder_settings$",
                ),
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    _save_private_text_input,
                ),
            ],
        },
        fallbacks=[CommandHandler("cancel", admin_cancel)],
        allow_reentry=True,
        per_message=False,
    )


def build_personal_autoresponder_conversation_handler() -> ConversationHandler:
    auto_callbacks = CallbackQueryHandler(
        personal_autoresponder_callback,
        pattern=r"^auto:",
    )
    return ConversationHandler(
        entry_points=[
            CommandHandler(
                "addacc",
                personal_autoresponder_start,
                filters=filters.ChatType.PRIVATE,
            )
        ],
        states={
            AUTO_MENU: [auto_callbacks],
            WAIT_AUTO_PHONE: [
                auto_callbacks,
                MessageHandler(filters.TEXT & ~filters.COMMAND, _userbot_phone_input),
            ],
            WAIT_AUTO_CODE: [
                auto_callbacks,
                MessageHandler(filters.TEXT & ~filters.COMMAND, _userbot_code_input),
            ],
            WAIT_AUTO_PASSWORD: [
                auto_callbacks,
                MessageHandler(filters.TEXT & ~filters.COMMAND, _userbot_password_input),
            ],
            WAIT_AUTO_QR: [
                auto_callbacks,
                MessageHandler(filters.TEXT & ~filters.COMMAND, _userbot_qr_input),
            ],
            WAIT_AUTO_TIMEZONE: [auto_callbacks],
            WAIT_AUTO_TEXT: [
                auto_callbacks,
                MessageHandler(filters.TEXT & ~filters.COMMAND, _save_night_text_input),
            ],
            WAIT_AUTO_PRIVATE_TEXT: [
                auto_callbacks,
                MessageHandler(filters.TEXT & ~filters.COMMAND, _save_private_text_input),
            ],
        },
        fallbacks=[CommandHandler("cancel", personal_autoresponder_cancel)],
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
                    else (
                        f"Доступ к автоответчику активирован {expiry}! "
                        "Теперь открой /addacc и подключи свой Telegram-аккаунт."
                        if level == "autoresponder"
                        else f"Доступ к боту открыт {expiry}! Теперь можешь общаться со мной."
                    )
                )
                return
            authorized = await database.is_authorized(user_id)
            if not authorized and await database.is_feature_authorized(
                user_id,
                "autoresponder",
            ):
                await message.reply_text(
                    "Для управления автоответчиком используй /addacc. "
                    "Этот ключ не открывает доступ к обычному диалогу с ботом."
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

    if is_transcription_request(message_text, message):
        await handle_voice_transcription(message, context)
        return

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
    if await handle_coin_leaderboard_request(update, context):
        return
    if await handle_balance_request(update, context):
        return

    if await handle_delete_bot_messages(update, context):
        return

    if await handle_delete_replied_message(update):
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
        history = await database.load_chat_history(
            message.chat_id,
            summary=summary,
            limit=DEFAULT_CONTEXT_SIZE,
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
        try:
            prompt = build_prompt(
                history=[],
                user_id=user_id,
                user_name=user.first_name or user.username or str(user_id),
                display_name=user.first_name or user.username or str(user_id),
                current_message=message_text,
                summary=summary,
                current_message_id=message.message_id,
            )
        except Exception:
            logging.exception("Не удалось собрать личный запрос без контекста.")
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
