"""Casino, banner inventory, marketplace, and subscription bonus handlers."""

import asyncio
import html
import logging
import re
import secrets
import time
from typing import Any

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaAnimation,
    InputMediaPhoto,
    Update,
)
from telegram.constants import ChatType
from telegram.error import TelegramError
from telegram.ext import ContextTypes

import database
from banner_store import (
    ensure_banner_file,
    ensure_market_banner_file,
    get_profile_banner,
    render_banner_preview,
    send_banner,
)
from config import (
    ADMIN_ID,
    BACKUP_CHANNEL_ID,
    CASINO_BONUS_CENTS,
    CASINO_BONUS_CHANNEL,
    CASINO_BONUS_CHANNEL_URL,
    CASINO_RECYCLE_PAYOUT_CENTS,
    CASINO_SPIN_COST_CENTS,
    CASINO_UNSUBSCRIBE_PENALTY_CENTS,
    DICE_ANIMATION_DELAY_SECONDS,
)
from yubari_banner import PRESET_NAMES, make_digest, pick_preset

_PRICE_RE = re.compile(r"^\s*(\d{1,8})(?:[,.](\d{1,2}))?\s*$")
_MARKET_COMMAND_RE = re.compile(r"^/market(?:@\w+)?(?:\s+(.+))?$", re.IGNORECASE)
_MARKET_TEXT_RE = re.compile(
    r"^(?:площадка|маркет|маркетплейс)\s+(.+)$",
    re.IGNORECASE,
)
_COIN_PROMO_RE = re.compile(
    r"^/coinpromo(?:@\w+)?\s+(YUBARA-COIN-[A-F0-9]{12})$",
    re.IGNORECASE,
)
_ACTIVATE_CODE_RE = re.compile(r"^/activate(?:@\w+)?(?:\s+(.+))?$", re.IGNORECASE)
_ACTIVATION_INPUT_TTL_SECONDS = 5 * 60
_ERROR_REPORT_INPUT_TTL_SECONDS = 10 * 60
_TRANSFER_RECIPIENT_RE = re.compile(r"^@?([a-z0-9_]{5,32})$", re.IGNORECASE)
_TRANSFER_USER_ID_RE = re.compile(r"^\d{1,20}$")
_RECYCLE_COMMAND_RE = re.compile(
    r"^(?:/recycle(?:@\w+)?|переработ(?:ка|ай))\s+"
    r"(?P<rarity>[\w-]+)\s+(?P<quantity>\d+)$",
    re.IGNORECASE,
)
_RARITY = {
    1: "Обычный",
    2: "Необычный",
    3: "Редкий",
    4: "Эпический",
    5: "Элитный",
    6: "Легендарный",
}
_RARITY_ALIASES = {
    "обычный": 1,
    "обычная": 1,
    "обычные": 1,
    "common": 1,
    "необычный": 2,
    "необычная": 2,
    "необычные": 2,
    "uncommon": 2,
    "редкий": 3,
    "редкая": 3,
    "редкие": 3,
    "rare": 3,
    "эпический": 4,
    "эпическая": 4,
    "эпические": 4,
    "epic": 4,
    "элитный": 5,
    "элитная": 5,
    "элитные": 5,
    "elite": 5,
    "легендарный": 6,
    "легендарная": 6,
    "легендарные": 6,
    "legendary": 6,
}


def _banner_retry_markup(item_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔄 Повторить подготовку", callback_data=f"inv:retry:{item_id}")],
            [
                InlineKeyboardButton("🎒 Инвентарь", callback_data="menu:inv"),
                _menu_button(),
            ],
        ]
    )


def _coins(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    value = abs(cents)
    return f"{sign}{value // 100}.{value % 100:02d}"


def _inventory_url(context: ContextTypes.DEFAULT_TYPE) -> str:
    username = getattr(context.bot, "username", None)
    if not username:
        return "https://t.me/"
    return f"https://t.me/{username}?start=inventory"


def _inventory_button(context: ContextTypes.DEFAULT_TYPE) -> InlineKeyboardButton:
    return InlineKeyboardButton("🎒 Инвентарь", url=_inventory_url(context))


_INVENTORY_PAGE_SIZE = 8


def _inventory_filter_rows(
    level_filter: str,
    sort_by: str,
    view_mode: str,
) -> list[list[InlineKeyboardButton]]:
    filters = [
        ("all", "Все"),
        ("1", "Обыч."),
        ("2", "Необыч."),
        ("3", "Редкий"),
        ("4", "Эпич."),
        ("5", "Элит."),
        ("6", "Легенд."),
    ]
    buttons = [
        InlineKeyboardButton(
            f"{'● ' if value == level_filter else ''}{label}",
            callback_data=f"inv:f:{value}:{sort_by}:{view_mode}",
        )
        for value, label in filters
    ]
    rows = [buttons[:3], buttons[3:6], buttons[6:]]
    rows.append(
        [
            InlineKeyboardButton(
                "↕️ Сортировка: по уровню" if sort_by == "date" else "↕️ Сортировка: новые",
                callback_data=(
                    f"inv:f:{level_filter}:"
                    f"{'level' if sort_by == 'date' else 'date'}:{view_mode}"
                ),
            ),
            InlineKeyboardButton(
                "📃 Списком" if view_mode == "carousel" else "🖼 Карусель",
                callback_data=f"inv:v:{'list' if view_mode == 'carousel' else 'carousel'}:{level_filter}:{sort_by}:0",
            ),
        ]
    )
    return rows


def _menu_button() -> InlineKeyboardButton:
    return InlineKeyboardButton("↩️ В меню", callback_data="casino:home")


def _casino_button(context: ContextTypes.DEFAULT_TYPE) -> InlineKeyboardButton:
    username = getattr(context.bot, "username", None)
    url = f"https://t.me/{username}?start=casino" if username else "https://t.me/"
    return InlineKeyboardButton("🎡 Открыть колесо удачи в ЛС", url=url)


def _casino_keyboard(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    skip_animation: bool,
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    f"🎲 Крутить ({_coins(CASINO_SPIN_COST_CENTS)}💰)",
                    callback_data="casino:spin",
                ),
                InlineKeyboardButton(
                    f"🎲 Крутить ×10 ({_coins(CASINO_SPIN_COST_CENTS * 10)}💰)",
                    callback_data="casino:spin10",
                ),
            ],
            [
                InlineKeyboardButton("💰 Баланс", callback_data="casino:balance"),
                InlineKeyboardButton("📊 Шансы", callback_data="casino:odds"),
            ],
            [
                _inventory_button(context),
                InlineKeyboardButton("🎁 Бонус 20💰", callback_data="bonus:go"),
            ],
            [InlineKeyboardButton("🏪 Площадка", callback_data="mkt:p:0:all:date")],
            [
                InlineKeyboardButton(
                    f"⏩ Пропуск анимации: {'вкл' if skip_animation else 'выкл'}",
                    callback_data="casino:anim",
                )
            ],
            [_menu_button()],
        ]
    )


def _main_menu_keyboard(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    private: bool,
) -> InlineKeyboardMarkup:
    inventory = (
        InlineKeyboardButton("🎒 Инвентарь", callback_data="menu:inv")
        if private
        else _inventory_button(context)
    )
    market = (
        InlineKeyboardButton("🏪 Площадка", callback_data="mkt:p:0:all:date")
        if private
        else _casino_button(context)
    )
    rows = [
        [InlineKeyboardButton("🎡 Колесо удачи", callback_data="casino:menu")],
        [inventory, market],
    ]
    if private:
        rows.append(
            [
                InlineKeyboardButton("👤 Профиль", callback_data="profile:show"),
                InlineKeyboardButton("📝 Заполнить профиль", callback_data="profile:fill"),
            ]
        )
    rows.append([InlineKeyboardButton("🎁 Бонус 20💰", callback_data="bonus:go")])
    if private:
        rows.append(
            [
                InlineKeyboardButton("🔑 Активировать код", callback_data="menu:activate"),
                InlineKeyboardButton("❓ Помощь", callback_data="menu:help"),
            ]
        )
        rows.append(
            [InlineKeyboardButton("🎁 Передать баннер", callback_data="menu:transfer")]
        )
    return InlineKeyboardMarkup(rows)


async def _skip_animation(user_id: int) -> bool:
    return await database.get_meta(f"casino_skip_animation:{user_id}") == "1"


async def open_casino(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    edit: bool = False,
) -> None:
    user = update.effective_user
    message = update.effective_message
    if user is None or message is None:
        return
    skip = await _skip_animation(user.id)
    text = (
        "🎡 Колесо удачи Юбары\n"
        f"Одна крутка стоит {_coins(CASINO_SPIN_COST_CENTS)} монеты; ×10 — "
        f"{_coins(CASINO_SPIN_COST_CENTS * 10)}.\n"
        "Результат случайный, монеты за крутку не возвращаются.\n"
        "💡 Баннер можно сменить в инвентаре в ЛС бота: напиши «инвентарь» "
        "или «сменить банер»."
    )
    keyboard = _casino_keyboard(context, skip_animation=skip)
    if edit and update.callback_query and update.callback_query.message:
        if update.callback_query.message.caption is not None:
            await update.callback_query.message.edit_caption(
                caption=text,
                reply_markup=keyboard,
            )
        else:
            await update.callback_query.message.edit_text(text, reply_markup=keyboard)
    else:
        await message.reply_text(text, reply_markup=keyboard)


async def open_main_menu(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    message = update.effective_message
    if message is None:
        return
    private = message.chat.type == ChatType.PRIVATE
    await message.reply_text(
        "Главное меню Юбары. Выбери раздел кнопкой ниже:",
        reply_markup=_main_menu_keyboard(context, private=private),
    )


async def _return_to_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = update.effective_user
    if query is None or query.message is None or user is None:
        return
    private = query.message.chat.type == ChatType.PRIVATE
    keyboard = _main_menu_keyboard(context, private=private)
    text = (
        "Главное меню Юбары. Выбери раздел кнопкой ниже:"
        if private
        else "Главное меню Юбары:"
    )
    if query.message.caption is not None:
        await query.message.edit_caption(
            caption=text,
            reply_markup=keyboard,
        )
    else:
        await query.message.edit_text(
            text,
            reply_markup=keyboard,
        )


def _result_level(values: tuple[int, int, int]) -> int:
    if values == (6, 6, 6):
        return 6
    if len(set(values)) == 1:
        return 5
    if len(set(values)) == 2:
        return 4 if secrets.randbelow(10) < 3 else 3
    return 2 if secrets.randbelow(10) < 3 else 1


async def _delete_dice_messages(
    bot: Any,
    chat_id: int,
    messages: list[Any],
    user_id: int,
) -> None:
    for dice_message in messages:
        for attempt in range(2):
            try:
                await bot.delete_message(
                    chat_id=chat_id,
                    message_id=dice_message.message_id,
                )
                break
            except TelegramError:
                if attempt == 0:
                    await asyncio.sleep(0.25)
                else:
                    logging.exception(
                        "Не удалось удалить кубик user_id=%s message_id=%s.",
                        user_id,
                        dice_message.message_id,
                    )


def _status_keyboard(round_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("⏩ Пропустить", callback_data=f"casino:skip:{round_id}")]]
    )


async def _show_admin_help(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None:
        return
    try:
        is_bot_admin = user.id == ADMIN_ID or await database.is_admin_user(user.id)
        is_group_admin = False
        if message.chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
            member = await context.bot.get_chat_member(message.chat_id, user.id)
            status = getattr(member.status, "value", member.status)
            is_group_admin = status in ("creator", "administrator")
    except TelegramError:
        logging.exception(
            "Не удалось проверить права для справки user_id=%s chat_id=%s.",
            user.id,
            message.chat_id,
        )
        await message.reply_text("Не удалось проверить права в этой группе. Попробуй позже.")
        return
    except Exception:
        logging.exception(
            "Ошибка проверки прав для справки user_id=%s chat_id=%s.",
            user.id,
            message.chat_id,
        )
        await message.reply_text("Не удалось открыть справку. Попробуй позже.")
        return

    sections = [
        "👤 Помощь пользователю\n"
        "• /start — открыть inline-меню; /help — эта справка.\n"
        "• /fortuna — колесо удачи; /casino — прежняя команда-алиас; "
        "/inventory — инвентарь баннеров.\n"
        "• «баланс» — посмотреть монеты; «топ монет» — рейтинг.\n"
        "• Нажми «🔑 Активировать код» в меню и отправь код сообщением; "
        "/activate КОД и /coinpromo КОД тоже доступны.\n"
        "• /cancel — отменить текущий ввод."
    ]
    if is_group_admin:
        sections.append(
            "🛡 Помощь администратору группы\n"
            "• /activategroup КОД — подключить эту группу одноразовым кодом.\n"
            "• Ответь «дэл» на сообщение, чтобы удалить его.\n"
            "• «дэл N» — удалить последние N сообщений Юбары "
            "(ответь на сообщение Юбары).\n"
            "• «дэл чат N» — удалить последние N сообщений чата."
        )
    if is_bot_admin:
        sections.append(
            "⚙️ Помощь администратору бота\n"
            "• /adm — панель групп, доступов, ключей, штрафных кодов и настроек.\n"
            "• /addacc — подключение Telegram-аккаунта и настройка автоответчика.\n"
            "• /gethistori — выбрать группу и получить файл истории "
            "(нужны членство и права администратора в группе).\n"
            "• /fees — накопленная комиссия площадки; /break — штатная остановка.\n"
            "• /cancel — отменить ввод или закрыть админ-панель."
        )
        if user.id == ADMIN_ID:
            sections.append(
                "👑 Только владелец бота\n"
                "• /deletealldata — необратимый сброс данных после подтверждения."
            )
    await message.reply_text(
        "\n\n".join(sections),
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "🐞 Сообщить об ошибке",
                        callback_data="menu:report",
                    )
                ],
                [_menu_button()],
            ]
        ),
    )


async def _spin(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    spins: int,
) -> None:
    query = update.callback_query
    user = update.effective_user
    message = query.message if query else update.effective_message
    if user is None or message is None:
        return
    if spins == 10 and message.chat.type != ChatType.PRIVATE:
        await message.reply_text(
            "Крутка ×10 доступна в ЛС бота.",
            reply_markup=InlineKeyboardMarkup([[_casino_button(context)]]),
        )
        return

    round_id = secrets.token_hex(12)
    cost = CASINO_SPIN_COST_CENTS * spins
    try:
        await database.ensure_user(
            user.id,
            message.chat_id,
            user.username,
            user.first_name or user.username,
        )
        await database.get_duel_balance(user.id, user.username)
        await database.begin_casino_round(round_id, user.id, user.username, cost)
    except database.InsufficientCasinoBalanceError:
        await message.reply_text(
            f"Не хватает монет. Крутка стоит {_coins(cost)} 💰."
        )
        return
    except database.ActiveCasinoRoundError:
        await message.reply_text("У тебя уже идёт крутка. Дождись её завершения.")
        return
    except Exception:
        logging.exception("Не удалось начать крутку казино user_id=%s.", user.id)
        await message.reply_text("Не удалось начать крутку. Попробуй позже.")
        return

    skip_animation = await _skip_animation(user.id)
    skip_events: dict[str, asyncio.Event] = context.application.bot_data.setdefault(
        "casino_skip_events",
        {},
    )
    skip_event = asyncio.Event()
    skip_events[round_id] = skip_event
    try:
        status = await message.reply_text(
            f"🎲 Крутка: 0/{spins * 3}",
            reply_markup=None if skip_animation else _status_keyboard(round_id),
        )
    except Exception:
        skip_events.pop(round_id, None)
        logging.exception("Не удалось показать статус крутки user_id=%s.", user.id)
        await database.fail_casino_round(round_id)
        return
    results: list[dict[str, Any]] = []
    all_values: list[tuple[int, int, int]] = []
    try:
        for spin_index in range(spins):
            dice_chat_id = user.id if skip_animation else message.chat_id
            dice_tasks = [
                asyncio.create_task(
                    context.bot.send_dice(
                        chat_id=dice_chat_id,
                        emoji="🎲",
                        disable_notification=skip_animation,
                    )
                )
                for _ in range(3)
            ]
            dice_messages: list[Any] = []
            try:
                send_results = await asyncio.gather(
                    *dice_tasks,
                    return_exceptions=True,
                )
                dice_messages = [
                    result
                    for result in send_results
                    if not isinstance(result, BaseException)
                ]
                failure = next(
                    (
                        result
                        for result in send_results
                        if isinstance(result, BaseException)
                    ),
                    None,
                )
                if failure is not None:
                    raise failure
                values = tuple(
                    int(dice_message.dice.value) for dice_message in dice_messages
                )
                all_values.append((values[0], values[1], values[2]))
                await asyncio.wait_for(
                    skip_event.wait(),
                    timeout=1.05 if skip_animation else DICE_ANIMATION_DELAY_SECONDS,
                )
            except TimeoutError:
                pass
            finally:
                for task in dice_tasks:
                    if not task.done():
                        task.cancel()
                send_results = await asyncio.gather(
                    *dice_tasks,
                    return_exceptions=True,
                )
                dice_messages = [
                    result
                    for result in send_results
                    if not isinstance(result, BaseException)
                ]
                await _delete_dice_messages(
                    context.bot,
                    dice_chat_id,
                    dice_messages,
                    user.id,
                )
            if not skip_animation and spin_index + 1 < spins:
                await status.edit_text(
                    f"🎲 Крутка: {spin_index + 1}/{spins}",
                    reply_markup=None if skip_animation else _status_keyboard(round_id),
                )
            if skip_event.is_set():
                skip_animation = True

        for values in all_values:
            level = _result_level(values)
            seed = secrets.token_hex(16)
            preset = (
                pick_preset(make_digest(user.id, item_seed=seed))
                if level == 6
                else None
            )
            results.append(
                {
                    "values": values,
                    "level": level,
                    "seed": seed,
                    "preset": preset,
                }
            )
        items = await database.complete_casino_round(round_id, results)
    except Exception:
        logging.exception("Ошибка крутки казино user_id=%s.", user.id)
        try:
            await database.fail_casino_round(round_id)
        except Exception:
            logging.exception("Не удалось вернуть монеты за сорванную крутку.")
        await status.edit_text("Крутка не завершилась; монеты возвращены.")
        skip_events.pop(round_id, None)
        return
    skip_events.pop(round_id, None)

    summary_lines = []
    for index, result in enumerate(results, start=1):
        values = " – ".join(str(value) for value in result["values"])
        summary_lines.append(
            f"{index}. 🎲 {values} — {_RARITY[int(result['level'])]}"
        )
    summary = "\n".join(summary_lines)
    if spins == 10:
        counts = {
            level: sum(int(item["level"]) == level for item in items)
            for level in _RARITY
        }
        totals = ", ".join(
            f"{_RARITY[level]}: {count}"
            for level, count in counts.items()
            if count
        )
        summary += f"\n\nИтого: {totals}."
    result_keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🎒 Инвентарь", callback_data="menu:inv"),
                InlineKeyboardButton("🏪 Магазин", callback_data="mkt:p:0:all:date"),
            ],
            [
                InlineKeyboardButton("🔁 Ещё раз", callback_data="casino:spin"),
                InlineKeyboardButton("🔁 Ещё ×10", callback_data="casino:spin10"),
            ],
            [_menu_button()],
        ]
    )
    await status.edit_text(
        f"🎉 Крутка завершена!\n{summary}\n\n⏳ Готовлю отдельные превью призов…",
        reply_markup=result_keyboard,
    )
    prepared: dict[int, dict[str, Any]] = {}
    for item in items:
        try:
            rendered = await ensure_banner_file(
                context.bot,
                item,
                priority=1,
            )
            prepared[int(item["item_id"])] = await ensure_market_banner_file(
                context.bot,
                rendered,
                priority=2,
            )
        except Exception:
            logging.exception(
                "Не удалось подготовить баннер item_id=%s.",
                item["item_id"],
            )
    await status.edit_text(
        f"🎉 Готово!\n{summary}\n\nКаждый приз отправлен отдельным сообщением ниже.",
        reply_markup=result_keyboard,
    )
    for result, item in zip(results, items):
        prepared_item = prepared.get(int(item["item_id"]))
        if prepared_item is None:
            await message.reply_text(
                f"Приз #{item['item_id']} записан в инвентарь, "
                "но не удалось подготовить изображение. Его можно повторить ниже.",
                reply_markup=_banner_retry_markup(int(item["item_id"])),
            )
            continue
        try:
            await send_banner(
                context.bot,
                message.chat_id,
                str(prepared_item["file_id"]),
                animated=int(prepared_item["level"]) == 6,
                caption=(
                    f"🎁 Приз #{item['item_id']} · "
                    f"{_RARITY[int(prepared_item['level'])]}\n"
                    f"🎲 {result['values'][0]} – {result['values'][1]} – {result['values'][2]}"
                ),
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "✅ Активировать",
                                callback_data=f"inv:s:{item['item_id']}",
                            ),
                            InlineKeyboardButton(
                                "🎒 Инвентарь",
                                callback_data="menu:inv",
                            ),
                        ],
                        [_menu_button()],
                    ]
                ),
            )
        except Exception:
            logging.exception("Не удалось показать приз item_id=%s.", item["item_id"])
            await message.reply_text(
                f"Приз #{item['item_id']} сохранён. Посмотри его в инвентаре."
            )


async def _show_inventory(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    page: int = 0,
    level_filter: str = "all",
    sort_by: str = "date",
    view_mode: str | None = None,
) -> None:
    user = update.effective_user
    message = update.effective_message
    if user is None or message is None:
        return
    if message.chat.type != ChatType.PRIVATE:
        await message.reply_text(
            "Инвентарь открывается в ЛС бота.",
            reply_markup=InlineKeyboardMarkup([[_inventory_button(context)]]),
        )
        return
    if level_filter != "all" and not (
        level_filter.isdigit() and 1 <= int(level_filter) <= 6
    ):
        level_filter = "all"
    if sort_by not in ("date", "level"):
        sort_by = "date"
    if view_mode not in ("carousel", "list"):
        view_mode = str(context.user_data.get("inventory_view", "carousel"))
    if view_mode not in ("carousel", "list"):
        view_mode = "carousel"
    context.user_data["inventory_view"] = view_mode
    level = int(level_filter) if level_filter != "all" else None
    total = await database.count_user_banner_items(user.id, level=level)
    query = update.callback_query
    if total == 0:
        context.user_data["inventory_page"] = 0
        context.user_data["inventory_filter"] = level_filter
        context.user_data["inventory_sort"] = sort_by
        context.user_data["inventory_list_page"] = 0
        empty_text = (
            f"В инвентаре нет баннеров уровня {level}."
            if level is not None
            else "Пока пусто. Попытай удачу в Колесе удачи."
        )
        empty_rows = _inventory_filter_rows(level_filter, sort_by, view_mode)
        empty_rows.extend(
            [
                [
                    InlineKeyboardButton("🎡 Колесо удачи", callback_data="casino:menu"),
                    InlineKeyboardButton("🏪 Магазин", callback_data="mkt:p:0:all:date"),
                ],
                [_menu_button()],
            ]
        )
        empty_markup = InlineKeyboardMarkup(empty_rows)
        if query is not None and query.message is not None:
            if view_mode == "list" and (
                query.message.photo or query.message.animation
            ):
                try:
                    await query.message.delete()
                except TelegramError:
                    logging.warning(
                        "Не удалось заменить пустую карусель инвентаря списком user_id=%s.",
                        user.id,
                        exc_info=True,
                    )
                sent = await context.bot.send_message(
                    chat_id=user.id,
                    text=empty_text,
                    reply_markup=empty_markup,
                )
                context.user_data["inventory_message_id"] = sent.message_id
            elif query.message.caption is not None:
                await query.message.edit_caption(caption=empty_text, reply_markup=empty_markup)
                context.user_data["inventory_message_id"] = query.message.message_id
            else:
                await query.message.edit_text(empty_text, reply_markup=empty_markup)
                context.user_data["inventory_message_id"] = query.message.message_id
        else:
            sent = await message.reply_text(empty_text, reply_markup=empty_markup)
            context.user_data["inventory_message_id"] = sent.message_id
        return

    context.user_data["inventory_filter"] = level_filter
    context.user_data["inventory_sort"] = sort_by
    if view_mode == "list":
        page_count = (total + _INVENTORY_PAGE_SIZE - 1) // _INVENTORY_PAGE_SIZE
        list_page = min(max(page, 0), page_count - 1)
        context.user_data["inventory_list_page"] = list_page
        context.user_data["inventory_page"] = list_page * _INVENTORY_PAGE_SIZE
        items = await database.get_user_banner_items(
            user.id,
            limit=_INVENTORY_PAGE_SIZE,
            offset=list_page * _INVENTORY_PAGE_SIZE,
            level=level,
            sort_by=sort_by,
        )
        rows = _inventory_filter_rows(level_filter, sort_by, view_mode)
        rows.extend(
            [
                [
                    InlineKeyboardButton(
                        f"{'⏳ ' if item['is_transfer_pending'] else '✅ ' if item['is_active'] else ''}"
                        f"#{item['item_id']} · "
                        f"{_RARITY[int(item['level'])]}",
                        callback_data=f"inv:i:{item['item_id']}",
                    )
                ]
                for item in items
            ]
        )
        navigation: list[InlineKeyboardButton] = []
        if list_page > 0:
            navigation.append(
                InlineKeyboardButton(
                    "⬅️",
                    callback_data=f"inv:lp:{list_page - 1}:{level_filter}:{sort_by}",
                )
            )
        navigation.append(
            InlineKeyboardButton(
                f"{list_page + 1}/{page_count}",
                callback_data="inv:noop",
            )
        )
        if list_page + 1 < page_count:
            navigation.append(
                InlineKeyboardButton(
                    "➡️",
                    callback_data=f"inv:lp:{list_page + 1}:{level_filter}:{sort_by}",
                )
            )
        if navigation:
            rows.append(navigation)
        rows.extend(
            [
                [
                    InlineKeyboardButton("🎡 Колесо удачи", callback_data="casino:menu"),
                    InlineKeyboardButton("🏪 Магазин", callback_data="mkt:p:0:all:date"),
                ],
                [_menu_button()],
            ]
        )
        text = (
            f"🎒 Инвентарь · {total} баннер(ов)\n"
            f"Фильтр: {_RARITY[int(level_filter)] if level_filter != 'all' else 'все редкости'}\n"
            "Нажми на баннер, чтобы открыть его действия."
        )
        markup = InlineKeyboardMarkup(rows)
        if query is not None and query.message is not None:
            if query.message.photo or query.message.animation:
                try:
                    await query.message.delete()
                except TelegramError:
                    logging.warning(
                        "Не удалось заменить карусель инвентаря списком user_id=%s.",
                        user.id,
                        exc_info=True,
                    )
                    await query.message.edit_reply_markup(reply_markup=None)
                    sent = await context.bot.send_message(
                        chat_id=user.id,
                        text=text,
                        reply_markup=markup,
                    )
                    context.user_data["inventory_message_id"] = sent.message_id
                    return
                sent = await context.bot.send_message(
                    chat_id=user.id,
                    text=text,
                    reply_markup=markup,
                )
                context.user_data["inventory_message_id"] = sent.message_id
            else:
                await query.message.edit_text(text, reply_markup=markup)
                context.user_data["inventory_message_id"] = query.message.message_id
        else:
            sent = await message.reply_text(text, reply_markup=markup)
            context.user_data["inventory_message_id"] = sent.message_id
        return

    index = min(max(page, 0), total - 1)
    context.user_data["inventory_page"] = index
    context.user_data["inventory_list_page"] = index // _INVENTORY_PAGE_SIZE
    items = await database.get_user_banner_items(
        user.id,
        limit=1,
        offset=index,
        level=level,
        sort_by=sort_by,
    )
    if not items:
        await message.reply_text("Не удалось открыть баннер. Обнови инвентарь.")
        return
    try:
        item = await ensure_banner_file(context.bot, items[0], priority=0)
    except Exception:
        logging.exception(
            "Не удалось открыть баннер в инвентаре item_id=%s user_id=%s.",
            items[0]["item_id"],
            user.id,
        )
        await message.reply_text(
            f"Баннер #{items[0]['item_id']} сохранён, но изображение пока не подготовлено. "
            "Попробуй повторить через кнопку ниже.",
            reply_markup=_banner_retry_markup(int(items[0]["item_id"])),
        )
        return
    item_id = int(item["item_id"])
    rarity = int(item["level"])
    rows: list[list[InlineKeyboardButton]] = []
    navigation: list[InlineKeyboardButton] = []
    if total > 1:
        navigation.append(
            InlineKeyboardButton(
                "⬅️",
                callback_data=f"inv:p:{(index - 1) % total}:{level_filter}:{sort_by}:carousel",
            )
        )
    navigation.append(
        InlineKeyboardButton(f"{index + 1}/{total}", callback_data="inv:noop")
    )
    if total > 1:
        navigation.append(
            InlineKeyboardButton(
                "➡️",
                callback_data=f"inv:p:{(index + 1) % total}:{level_filter}:{sort_by}:carousel",
            )
        )
    rows.append(navigation)
    if not item["is_active"]:
        rows.append(
            [InlineKeyboardButton("✅ Сделать активным", callback_data=f"inv:s:{item_id}")]
        )
    rows.append(
        [
            InlineKeyboardButton("♻️ Переработать", callback_data=f"inv:r:{item_id}"),
            InlineKeyboardButton("🏷 Продать", callback_data=f"mkt:l:{item_id}"),
        ]
    )
    rows.append(
        [InlineKeyboardButton("🎁 Передать", callback_data=f"inv:t:{item_id}")]
    )
    rows.extend(_inventory_filter_rows(level_filter, sort_by, view_mode))
    rows.append(
        [
            InlineKeyboardButton("🎡 Колесо удачи", callback_data="casino:menu"),
            InlineKeyboardButton("🏪 Магазин", callback_data="mkt:p:0:all:date"),
        ]
    )
    rows.append([_menu_button()])
    caption = (
        f"🎒 Инвентарь · баннер #{item_id} · {_RARITY[rarity]}\n"
        f"{'✅ Активный' if item['is_active'] else 'Не активен'}"
    )
    markup = InlineKeyboardMarkup(rows)
    if (
        query is not None
        and query.message is not None
        and (query.message.photo or query.message.animation)
    ):
        media = (
            InputMediaAnimation(media=str(item["file_id"]), caption=caption)
            if rarity == 6
            else InputMediaPhoto(media=str(item["file_id"]), caption=caption)
        )
        await query.message.edit_media(media=media, reply_markup=markup)
        context.user_data["inventory_message_id"] = query.message.message_id
    else:
        if query is not None and query.message is not None:
            try:
                await query.message.delete()
            except TelegramError:
                logging.warning("Не удалось заменить список каруселью.", exc_info=True)
        sent = await send_banner(
            context.bot,
            message.chat_id,
            str(item["file_id"]),
            animated=rarity == 6,
            caption=caption,
            reply_markup=markup,
        )
        context.user_data["inventory_message_id"] = sent.message_id


async def _show_item(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    item_id: int,
) -> None:
    query = update.callback_query
    user = update.effective_user
    if query is None or user is None or query.message is None:
        return
    if query.message.chat.type != ChatType.PRIVATE:
        await query.message.reply_text("Открой инвентарь в ЛС бота.")
        return
    item = await database.get_banner_item(item_id)
    if item is None or int(item["user_id"]) != user.id:
        await query.message.reply_text("Откройте инвентарь заново.")
        return
    item = await ensure_banner_file(context.bot, item, priority=0, force=True)
    if await _handle_pending_transfer_item(update, context, item):
        return
    level = int(item["level"])
    is_listed = bool(
        await database.get_market_lot_for_item(item_id)
    )
    buttons: list[list[InlineKeyboardButton]] = []
    if not item["is_active"]:
        buttons.append(
            [InlineKeyboardButton("✅ Сделать активным", callback_data=f"inv:s:{item_id}")]
        )
    buttons.append(
        [InlineKeyboardButton("♻️ Переработать", callback_data=f"inv:r:{item_id}")]
    )
    if not is_listed:
        buttons.append(
            [InlineKeyboardButton("🏷 Выставить на продажу", callback_data=f"mkt:l:{item_id}")]
        )
    if not is_listed and item["source"] != "owner":
        buttons.append(
            [InlineKeyboardButton("🎁 Передать", callback_data=f"inv:t:{item_id}")]
        )
    buttons.append(
        [
            InlineKeyboardButton("↩️ Инвентарь", callback_data="menu:inv"),
            _menu_button(),
        ]
    )
    await send_banner(
        context.bot,
        query.message.chat_id,
        str(item["file_id"]),
        animated=level == 6,
        caption=f"Баннер #{item_id} · {_RARITY[level]}",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def _show_market(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    page: int,
    level_filter: str,
    sort_by: str,
    *,
    search_query: str | None = None,
) -> None:
    user = update.effective_user
    message = update.effective_message
    if user is None or message is None:
        return
    if message.chat.type != ChatType.PRIVATE:
        await message.reply_text("Торговая площадка открывается в ЛС бота.")
        return
    if search_query is None:
        search_query = str(context.user_data.get("market_search_query") or "")
    else:
        context.user_data["market_search_query"] = search_query.strip()
        search_query = str(context.user_data["market_search_query"])
    level = int(level_filter) if level_filter.isdigit() and 1 <= int(level_filter) <= 6 else None
    total = (
        await database.count_market_search_results(
            search_query,
            preset_names=PRESET_NAMES,
            level=level,
        )
        if search_query
        else await database.count_market_lots(level=level)
    )
    page = min(max(page, 0), total - 1) if total else 0
    lots = (
        await database.search_market_lots(
            search_query,
            preset_names=PRESET_NAMES,
            level=level,
            sort_by=sort_by,
            limit=1,
            offset=page,
        )
        if search_query
        else await database.get_market_lots(
            level=level,
            sort_by=sort_by,
            limit=1,
            offset=page,
        )
    )
    query = update.callback_query
    if total == 0:
        context.user_data["market_page"] = 0
        context.user_data["market_filter"] = level_filter
        context.user_data["market_sort"] = sort_by
        text = (
            f"🔎 По запросу «{search_query[:80]}» ничего не нашлось."
            if search_query
            else "🏪 Здесь пока нет баннеров на продаже."
        )
        markup = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("🔎 Поиск", callback_data="mkt:search"),
                    InlineKeyboardButton("Сбросить поиск", callback_data="mkt:clear"),
                ],
                [
                    InlineKeyboardButton("🎒 Инвентарь", callback_data="menu:inv"),
                    InlineKeyboardButton("🎡 Колесо удачи", callback_data="casino:menu"),
                ],
                [_menu_button()],
            ]
        )
        if query is not None and query.message is not None:
            if query.message.caption is not None:
                await query.message.edit_caption(caption=text, reply_markup=markup)
            else:
                await query.message.edit_text(text, reply_markup=markup)
        else:
            await message.reply_text(text, reply_markup=markup)
        return
    if not lots:
        await message.reply_text("Лоты изменились. Открой площадку заново.")
        return
    context.user_data["market_page"] = page
    context.user_data["market_filter"] = level_filter
    context.user_data["market_sort"] = sort_by
    lot = lots[0]
    item = await database.get_banner_item(int(lot["item_id"]))
    if item is None:
        raise RuntimeError(f"Баннер магазина item_id={lot['item_id']} не найден.")
    item = await ensure_market_banner_file(context.bot, item, priority=0)
    level = int(item["level"])
    rows: list[list[InlineKeyboardButton]] = []
    navigation: list[InlineKeyboardButton] = []
    if page > 0:
        navigation.append(
            InlineKeyboardButton(
                "⬅️",
                callback_data=f"mkt:p:{page - 1}:{level_filter}:{sort_by}",
            )
        )
    navigation.append(
        InlineKeyboardButton(f"{page + 1}/{total}", callback_data="mkt:noop")
    )
    if page + 1 < total:
        navigation.append(
            InlineKeyboardButton(
                "➡️",
                callback_data=f"mkt:p:{page + 1}:{level_filter}:{sort_by}",
            )
        )
    rows.append(navigation)
    rows.append(
        [
            InlineKeyboardButton("🔎 Поиск", callback_data="mkt:search"),
            InlineKeyboardButton(
                "✖️ Сбросить поиск" if search_query else "Все лоты",
                callback_data="mkt:clear",
            ),
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                f"🛒 Купить за {_coins(int(lot['price_cents']))}💰",
                callback_data=f"mkt:b:{lot['lot_id']}",
            )
        ]
    )
    filters_row = [
        InlineKeyboardButton(
            "Все уровни" if level_filter != "all" else "Все ✓",
            callback_data=f"mkt:p:0:all:{sort_by}",
        )
    ]
    filters_row.extend(
        InlineKeyboardButton(
            f"Ур. {entry}{' ✓' if level_filter == str(entry) else ''}",
            callback_data=f"mkt:p:0:{entry}:{sort_by}",
        )
        for entry in range(1, 7)
    )
    rows.append(filters_row[:4])
    rows.append(filters_row[4:])
    if search_query:
        next_sort = {
            "relevance": "price",
            "price": "date",
            "date": "relevance",
        }.get(sort_by, "relevance")
    else:
        next_sort = "price" if sort_by == "date" else "date"
    sort_label = {
        "date": "дата",
        "price": "цена",
        "relevance": "релевантность",
    }.get(sort_by, "дата")
    rows.append(
        [
            InlineKeyboardButton(
                f"Сортировка: {sort_label}",
                callback_data=f"mkt:p:{page}:{level_filter}:{next_sort}",
            ),
            InlineKeyboardButton("Мои лоты", callback_data="mkt:mine"),
        ]
    )
    rows.append(
        [
            InlineKeyboardButton("🎒 Инвентарь", callback_data="menu:inv"),
            InlineKeyboardButton("🎡 Колесо удачи", callback_data="casino:menu"),
        ]
    )
    rows.append([_menu_button()])
    preset_name = PRESET_NAMES.get(str(item.get("preset") or ""), "")
    title = html.escape(str(lot.get("title") or "").strip())
    pattern_label = html.escape(preset_name or "Стандартный")
    text = (
        f"🏪 Лот #{lot['lot_id']}"
        f"{f' · {title}' if title else ''}\n"
        f"{_RARITY[level]}\n"
        f"Паттерн: {pattern_label}\n"
        f"Цена: {_coins(int(lot['price_cents']))} монет\n"
        "Цена включает комиссию площадки 20%."
    )
    markup = InlineKeyboardMarkup(rows)
    file_id = str(item.get("market_file_id") or "")
    if not file_id:
        raise RuntimeError(f"У лота #{lot['lot_id']} нет превью магазина.")
    if query is not None and query.message is not None and (
        query.message.photo or query.message.animation
    ):
        media = (
            InputMediaAnimation(media=file_id, caption=text, parse_mode="HTML")
            if level == 6
            else InputMediaPhoto(media=file_id, caption=text, parse_mode="HTML")
        )
        await query.message.edit_media(media=media, reply_markup=markup)
    else:
        if query is not None and query.message is not None:
            try:
                await query.message.edit_reply_markup(reply_markup=None)
            except TelegramError:
                logging.debug("Не удалось очистить старое меню магазина.", exc_info=True)
        await send_banner(
            context.bot,
            message.chat_id,
            file_id,
            animated=level == 6,
            caption=text,
            reply_markup=markup,
            parse_mode="HTML",
        )


async def _show_my_lots(
    update: Update,
) -> None:
    query = update.callback_query
    user = update.effective_user
    if query is None or user is None or query.message is None:
        return
    lots = await database.get_user_market_lots(user.id)
    if not lots:
        await query.message.reply_text(
            "У тебя нет активных лотов.",
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("🏪 Площадка", callback_data="mkt:p:0:all:date")],
                    [InlineKeyboardButton("🎡 Колесо удачи", callback_data="casino:menu")],
                    [_menu_button()],
                ]
            ),
        )
        return
    rows = [
        [
            InlineKeyboardButton(
                f"Снять #{lot['lot_id']} · "
                f"{str(lot.get('title') or _RARITY[int(lot['level'])])[:24]}",
                callback_data=f"mkt:c:{lot['lot_id']}",
            )
        ]
        for lot in lots[:30]
    ]
    await query.message.reply_text(
        "Твои активные лоты:",
        reply_markup=InlineKeyboardMarkup(
            rows
            + [
                [InlineKeyboardButton("🏪 Площадка", callback_data="mkt:p:0:all:date")],
                [InlineKeyboardButton("🎒 Инвентарь", callback_data="menu:inv")],
                [_menu_button()],
            ]
        ),
    )


async def _bonus_prompt(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    del context
    message = update.effective_message
    if message is None:
        return
    await message.reply_text(
        f"Бонус {_coins(CASINO_BONUS_CENTS)} монет за подписку на {CASINO_BONUS_CHANNEL}. "
        f"Если отпишешься, спишется штраф {_coins(CASINO_UNSUBSCRIBE_PENALTY_CENTS)} монет. "
        "При отрицательном балансе вместо баннера будет показан служебный "
        "баннер; продавать баннеры нельзя до выхода из долга. Сами баннеры не пропадут.",
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("📢 Открыть канал", url=CASINO_BONUS_CHANNEL_URL)],
                [InlineKeyboardButton("✅ Проверить подписку", callback_data="bonus:chk")],
                [InlineKeyboardButton("❌ Отмена", callback_data="casino:menu")],
            ]
        ),
    )


async def _handle_unsubscribe(bot: Any, user_id: int) -> None:
    balance = await database.penalize_subscription_bonus(
        user_id,
        CASINO_UNSUBSCRIBE_PENALTY_CENTS,
    )
    if balance is None:
        return
    debt_note = (
        "\nПока баланс отрицательный, профиль показывает служебный баннер."
        if balance < 0
        else ""
    )
    try:
        await bot.send_message(
            chat_id=user_id,
            text=(
                f"Отписка от {CASINO_BONUS_CHANNEL} замечена. Списано "
                f"{_coins(CASINO_UNSUBSCRIBE_PENALTY_CENTS)} монет; текущий баланс "
                f"{_coins(balance)}.{debt_note}"
            ),
        )
    except TelegramError:
        logging.exception("Не удалось уведомить user_id=%s о штрафе за отписку.", user_id)


async def on_chat_member_update(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    member_update = update.chat_member
    if member_update is None:
        return
    status = getattr(
        member_update.new_chat_member.status,
        "value",
        member_update.new_chat_member.status,
    )
    member = member_update.new_chat_member.user
    if (
        member_update.chat.type in (ChatType.GROUP, ChatType.SUPERGROUP)
        and not member.is_bot
    ):
        is_member = status in ("member", "administrator", "creator") or (
            status == "restricted"
            and bool(getattr(member_update.new_chat_member, "is_member", False))
        )
        try:
            if is_member:
                await database.add_allowed_group(member_update.chat.id)
            await database.set_group_membership(
                member_update.chat.id,
                member.id,
                is_member=is_member,
            )
        except Exception:
            logging.exception(
                "Не удалось обновить членство user_id=%s в группе %s.",
                member.id,
                member_update.chat.id,
            )

    if (
        str(member_update.chat.username or "").casefold() == "megurushka"
        and status in ("left", "kicked")
    ):
        await _handle_unsubscribe(context.bot, member.id)


async def audit_subscription_bonuses(context: ContextTypes.DEFAULT_TYPE) -> None:
    for user_id in await database.get_users_with_active_subscription_bonus():
        try:
            member = await context.bot.get_chat_member(CASINO_BONUS_CHANNEL, user_id)
            status = getattr(member.status, "value", member.status)
            if status not in ("member", "administrator", "creator"):
                await _handle_unsubscribe(context.bot, user_id)
        except TelegramError:
            logging.exception("Не удалось проверить подписку user_id=%s.", user_id)
        await asyncio.sleep(0.2)


async def _check_bonus(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    user = update.effective_user
    if query is None or user is None or query.message is None:
        return
    try:
        member = await context.bot.get_chat_member(CASINO_BONUS_CHANNEL, user.id)
    except TelegramError:
        logging.exception("Не удалось проверить подписку user_id=%s.", user.id)
        await query.message.reply_text("Не удалось проверить подписку. Попробуй позже.")
        return
    status = getattr(member.status, "value", member.status)
    if status not in ("member", "administrator", "creator"):
        await query.message.reply_text("Подписку не вижу, подпишись и нажми ещё раз.")
        return
    try:
        await database.ensure_user(
            user.id,
            query.message.chat_id,
            user.username,
            user.first_name or user.username,
        )
        await database.get_duel_balance(user.id, user.username)
        result, _ = await database.claim_subscription_bonus(
            user.id,
            CASINO_BONUS_CENTS,
        )
        if result == "claimed":
            balance = await database.get_duel_balance(user.id, user.username)
            await query.message.reply_text(
                f"Подписка проверена, начислено {_coins(CASINO_BONUS_CENTS)} монет. "
                f"Баланс: {_coins(balance)}."
            )
        else:
            await query.message.reply_text("Бонус уже получен.")
    except Exception:
        logging.exception("Не удалось начислить бонус user_id=%s.", user.id)
        await query.message.reply_text("Не удалось начислить бонус. Попробуй позже.")


async def _parse_callback_int(query: Any, value: str) -> int | None:
    try:
        return int(value)
    except ValueError:
        if query.message:
            await query.message.reply_text("Откройте меню заново.")
        return None


async def on_game_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    user = update.effective_user
    if query is None:
        return
    action = query.data or ""
    is_transfer_action = action.startswith(("inv:ta:", "inv:tr:", "inv:tx:"))
    if not is_transfer_action:
        await query.answer()
    if user is None or query.message is None:
        return
    if action in ("casino:home", "menu:home", "casino:cancel"):
        for key in (
            "market_search_waiting",
            "market_search_query",
            "market_listing_item",
            "market_listing_title_item",
            "market_listing_price_cents",
            "inventory_transfer_item_id",
            "activation_code_waiting_until",
        ):
            context.user_data.pop(key, None)
        await _return_to_main_menu(update, context)
        return
    if action == "menu:help":
        await _show_admin_help(update, context)
        return
    if action == "menu:report":
        if query.message.chat.type != ChatType.PRIVATE or query.message.chat_id != user.id:
            await query.message.reply_text("Чтобы сообщить об ошибке, открой справку в ЛС с ботом.")
            return
        context.user_data["error_report_waiting_until"] = (
            time.monotonic() + _ERROR_REPORT_INPUT_TTL_SECONDS
        )
        await query.message.reply_text(
            "Опиши ошибку одним сообщением (до 3000 символов). "
            "Я отправлю обращение владельцу бота. Не присылай пароли и коды доступа.",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "❌ Отмена",
                            callback_data="menu:report_cancel",
                        )
                    ],
                    [_menu_button()],
                ]
            ),
        )
        return
    if action == "menu:report_cancel":
        context.user_data.pop("error_report_waiting_until", None)
        await query.message.reply_text(
            "Отправка сообщения об ошибке отменена.",
            reply_markup=InlineKeyboardMarkup([[_menu_button()]]),
        )
        return
    if action == "menu:activate":
        context.user_data["activation_code_waiting_until"] = (
            time.monotonic() + _ACTIVATION_INPUT_TTL_SECONDS
        )
        await query.message.reply_text(
            "Отправь код следующим сообщением — вводить команду со слешем не нужно. "
            "Подойдут ключ доступа, промокод на монеты или штрафной ключ. "
            "Запрос действует 5 минут.",
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("❌ Отмена", callback_data="menu:activation_cancel")],
                    [_menu_button()],
                ]
            ),
        )
        return
    if action == "menu:activation_cancel":
        context.user_data.pop("activation_code_waiting_until", None)
        await query.message.reply_text(
            "Ввод кода отменён.",
            reply_markup=InlineKeyboardMarkup([[_menu_button()]]),
        )
        return
    if action == "menu:transfer":
        await query.message.reply_text(
            "Передача доступна для баннеров из инвентаря: открой его "
            "и нажми «🎁 Передать» под нужным предметом.",
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("🎒 Инвентарь", callback_data="menu:inv")],
                    [_menu_button()],
                ]
            ),
        )
        return
    if action == "inv:tcancel":
        context.user_data.pop("inventory_transfer_item_id", None)
        await query.message.reply_text(
            "Передача отменена.",
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("🎒 Инвентарь", callback_data="menu:inv")],
                    [_menu_button()],
                ]
            ),
        )
        return
    if action.startswith(("inv:ta:", "inv:tr:", "inv:tx:")):
        item_id = await _parse_callback_int(query, action.rsplit(":", 1)[1])
        if item_id is None:
            await query.answer("Некорректный запрос.", show_alert=True)
            return
        if action.startswith("inv:tx:") and (
            query.message.chat.type != ChatType.PRIVATE
            or query.message.chat_id != user.id
        ):
            await query.answer("Отменить передачу можно в своём ЛС.", show_alert=True)
            return
        if action.startswith("inv:ta:"):
            pending = await database.get_pending_banner_transfer(item_id)
            if (
                pending is None
                or int(pending["recipient_id"]) != user.id
            ):
                await query.answer(
                    "Запрос уже закрыт или предназначен другому пользователю.",
                    show_alert=True,
                )
                return
            result, sender_id = await database.accept_banner_transfer(item_id, user.id)
            if result == "transferred" and sender_id is not None:
                await query.answer("Передача принята.")
                accepted_text = (
                    f"✅ Ты принял баннер #{item_id}. Он стал твоим активным баннером. "
                    "Нажми «Переделать баннер» в профиле, чтобы создать оформление "
                    "с твоей аватаркой."
                )
                if query.message.chat.type == ChatType.PRIVATE:
                    await query.message.edit_text(
                        accepted_text,
                        reply_markup=InlineKeyboardMarkup(
                            [
                                [
                                    InlineKeyboardButton(
                                        "👤 Открыть профиль",
                                        callback_data="profile:show",
                                    )
                                ],
                                [
                                    InlineKeyboardButton(
                                        "🎒 Открыть инвентарь",
                                        callback_data="menu:inv",
                                    )
                                ],
                                [_menu_button()],
                            ]
                        ),
                    )
                else:
                    await query.message.edit_text(accepted_text, reply_markup=None)
                logging.info(
                    "Передача принята item_id=%s sender_id=%s recipient_id=%s.",
                    item_id,
                    sender_id,
                    user.id,
                )
                await _notify_transfer_resolution(
                    context,
                    sender_id=sender_id,
                    recipient_id=user.id,
                    item_id=item_id,
                    accepted=True,
                )
            else:
                await query.answer(
                    "Не удалось принять передачу.",
                    show_alert=True,
                )
                explanations = {
                    "missing": "Запрос уже закрыт или баннер больше недоступен.",
                    "not_recipient": "Этот запрос предназначен другому пользователю.",
                    "listed": "Баннер выставлен на площадку; передача отменена.",
                }
                await query.message.edit_text(
                    explanations.get(result, "Не удалось принять передачу.")
                )
            return
        if action.startswith("inv:tr:"):
            pending = await database.get_pending_banner_transfer(item_id)
            if (
                pending is None
                or int(pending["recipient_id"]) != user.id
            ):
                await query.answer(
                    "Запрос уже закрыт или предназначен другому пользователю.",
                    show_alert=True,
                )
                return
            result, sender_id = await database.return_banner_transfer(item_id, user.id)
            if result == "returned" and sender_id is not None:
                await query.answer("Запрос возвращён.")
                returned_text = (
                    f"↩️ Запрос на баннер #{item_id} возвращён. "
                    "Баннер остался у отправителя."
                )
                await query.message.edit_text(
                    returned_text,
                    reply_markup=(
                        InlineKeyboardMarkup([[_menu_button()]])
                        if query.message.chat.type == ChatType.PRIVATE
                        else None
                    ),
                )
                logging.info(
                    "Запрос передачи возвращён item_id=%s sender_id=%s recipient_id=%s.",
                    item_id,
                    sender_id,
                    user.id,
                )
                await _notify_transfer_resolution(
                    context,
                    sender_id=sender_id,
                    recipient_id=user.id,
                    item_id=item_id,
                    accepted=False,
                )
            else:
                await query.answer(
                    "Запрос уже закрыт или предназначен другому пользователю.",
                    show_alert=True,
                )
                await query.message.edit_text(
                    "Запрос уже закрыт или предназначен другому пользователю."
                )
            return
        result, recipient_id = await database.cancel_banner_transfer(item_id, user.id)
        if result == "cancelled" and recipient_id is not None:
            await query.answer("Передача отменена.")
            text = f"Передача баннера #{item_id} отменена; предмет остался у тебя."
            markup = InlineKeyboardMarkup([[_menu_button()]])
            if query.message.photo or query.message.animation:
                await query.message.edit_caption(caption=text, reply_markup=markup)
            else:
                await query.message.edit_text(text, reply_markup=markup)
            try:
                await context.bot.send_message(
                    chat_id=recipient_id,
                    text=f"Отправитель отменил запрос на передачу баннера #{item_id}.",
                )
            except TelegramError:
                logging.warning(
                    "Не удалось сообщить получателю user_id=%s об отмене transfer_item_id=%s.",
                    recipient_id,
                    item_id,
                    exc_info=True,
                )
        else:
            await query.answer("Этот запрос уже закрыт.", show_alert=True)
            await query.message.reply_text("Этот запрос уже закрыт.")
        return
    if action in ("inv:noop", "mkt:noop"):
        return
    if action.startswith(("inv:", "menu:inv", "mkt:")) and (
        query.message.chat.type != ChatType.PRIVATE
        or query.message.chat_id != user.id
    ):
        await query.message.reply_text("Откройте инвентарь заново в ЛС бота.")
        return
    if action in ("casino:spin", "casino:again"):
        await _spin(update, context, spins=1)
    elif action == "casino:spin10":
        await _spin(update, context, spins=10)
    elif action == "casino:menu":
        await open_casino(update, context, edit=True)
    elif action == "casino:balance":
        try:
            balance = await database.get_duel_balance(user.id, user.username)
            await query.message.reply_text(f"Твой баланс: {_coins(balance)} 💰.")
        except Exception:
            logging.exception("Не удалось получить баланс user_id=%s.", user.id)
            await query.message.reply_text("Не удалось получить баланс. Попробуй позже.")
    elif action == "casino:odds":
        await query.message.reply_text(
            "Шансы за одну крутку:\n"
            "Обычный — 38,9%; необычный — 16,7%; редкий — 29,2%; "
            "эпический — 12,5%; элитный — 2,3%; легендарный — 0,46%."
        )
    elif action == "casino:anim":
        current = await _skip_animation(user.id)
        await database.set_meta(
            f"casino_skip_animation:{user.id}",
            "0" if current else "1",
        )
        await open_casino(update, context, edit=True)
    elif action.startswith("casino:skip:"):
        round_id = action.split(":", 2)[2]
        skip_events: dict[str, asyncio.Event] = context.application.bot_data.setdefault(
            "casino_skip_events",
            {},
        )
        event = skip_events.get(round_id)
        if event is not None:
            event.set()
        try:
            await query.message.edit_reply_markup(reply_markup=None)
        except TelegramError:
            logging.exception("Не удалось убрать кнопку пропуска крутки.")
    elif action == "menu:inv" or action.startswith(
        ("inv:p:", "inv:lp:", "inv:f:", "inv:v:")
    ):
        view_mode = str(context.user_data.get("inventory_view", "carousel"))
        level_filter = str(context.user_data.get("inventory_filter", "all"))
        sort_by = str(context.user_data.get("inventory_sort", "date"))
        page = (
            int(context.user_data.get("inventory_list_page", 0))
            if view_mode == "list"
            else int(context.user_data.get("inventory_page", 0))
        )
        if action.startswith("inv:p:"):
            parts = action.split(":")
            page = int(parts[2])
            level_filter = parts[3]
            sort_by = parts[4]
            view_mode = parts[5] if len(parts) > 5 else "carousel"
        elif action.startswith("inv:lp:"):
            parts = action.split(":")
            page = int(parts[2])
            level_filter = parts[3]
            sort_by = parts[4]
            view_mode = "list"
        elif action.startswith("inv:f:"):
            parts = action.split(":")
            page = 0
            level_filter = parts[2]
            sort_by = parts[3]
            view_mode = parts[4]
        elif action.startswith("inv:v:"):
            parts = action.split(":")
            view_mode = parts[2]
            level_filter = parts[3]
            sort_by = parts[4]
            page = int(parts[5])
        await _show_inventory(
            update,
            context,
            page=page,
            level_filter=level_filter,
            sort_by=sort_by,
            view_mode=view_mode,
        )
    elif action.startswith("inv:i:"):
        item_id = await _parse_callback_int(query, action.split(":")[2])
        if item_id is not None:
            await _show_item(update, context, item_id)
    elif action.startswith("inv:retry:"):
        item_id = await _parse_callback_int(query, action.split(":")[2])
        if item_id is None:
            return
        item = await database.get_banner_item(item_id)
        if item is None or int(item["user_id"]) != user.id:
            await query.message.reply_text("Этот баннер уже недоступен.")
            return
        progress = await query.message.reply_text(
            f"⏳ Начала восстановление баннера #{item_id}. "
            "Это может занять несколько секунд."
        )
        try:
            item = await ensure_banner_file(
                context.bot,
                item,
                priority=0,
                force=True,
            )
            rarity = int(item["level"])
            await send_banner(
                context.bot,
                query.message.chat_id,
                str(item["file_id"]),
                animated=rarity == 6,
                caption=f"Баннер #{item_id} · {_RARITY[rarity]}",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [InlineKeyboardButton("✅ Активировать", callback_data=f"inv:s:{item_id}")],
                        [
                            InlineKeyboardButton("🎒 Инвентарь", callback_data="menu:inv"),
                            _menu_button(),
                        ],
                    ]
                ),
            )
        except Exception:
            logging.exception(
                "Не удалось повторно подготовить баннер item_id=%s user_id=%s.",
                item_id,
                user.id,
            )
            await progress.edit_text(
                f"Баннер #{item_id} сохранён, но изображение пока не удалось подготовить. "
                "Нажми «Повторить подготовку» ещё раз или открой инвентарь позже.",
                reply_markup=_banner_retry_markup(item_id),
            )
            return
        await progress.edit_text(
            f"✅ Баннер #{item_id} восстановлен и сохранён в инвентаре.",
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("🎒 Открыть инвентарь", callback_data="menu:inv")],
                    [_menu_button()],
                ]
            ),
        )
    elif action.startswith("inv:t:"):
        item_id = await _parse_callback_int(query, action.split(":")[2])
        if item_id is None:
            return
        item = await database.get_banner_item(item_id)
        if item is None or int(item["user_id"]) != user.id:
            await query.message.reply_text("Этот предмет больше недоступен.")
            return
        if item["source"] == "owner":
            await query.message.reply_text(
                "Служебный баннер передавать нельзя."
            )
            return
        context.user_data["inventory_transfer_item_id"] = item_id
        await query.message.reply_text(
            f"Кому передать баннер #{item_id}? Отправь @username или Telegram ID. "
            "Получатель должен хотя бы раз взаимодействовать с Юбарой. "
            "После принятия баннер станет активным у получателя, заменив его текущий. "
            "Получатель сможет перерисовать его кнопкой в профиле. "
            "Для отмены нажми кнопку или отправь /cancel.",
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("❌ Отмена", callback_data="inv:tcancel")],
                    [_menu_button()],
                ]
            ),
        )
    elif action.startswith("inv:s:"):
        item_id = await _parse_callback_int(query, action.split(":")[2])
        if item_id is None:
            return
        item = await database.get_banner_item(item_id)
        if item is None or int(item["user_id"]) != user.id:
            await query.message.reply_text("Откройте инвентарь заново.")
            return
        item = await ensure_banner_file(context.bot, item, priority=0, force=True)
        if await database.activate_banner_item(user.id, item_id):
            await query.message.reply_text("Баннер установлен ✅")
            await send_banner(
                context.bot,
                query.message.chat_id,
                str(item["file_id"]),
                animated=int(item["level"]) == 6,
                caption=f"Активный баннер · {_RARITY[int(item['level'])]}",
            )
    elif action.startswith("inv:r:"):
        item_id = await _parse_callback_int(query, action.split(":")[2])
        if item_id is None:
            return
        item = await database.get_banner_item(item_id)
        if item is None or int(item["user_id"]) != user.id:
            await query.message.reply_text("Откройте инвентарь заново.")
            return
        payout = CASINO_RECYCLE_PAYOUT_CENTS[int(item["level"])]
        await query.message.reply_text(
            f"Переработать за {_coins(payout)} монет? Это необратимо.",
            reply_markup=InlineKeyboardMarkup(
                [[
                    InlineKeyboardButton("♻️ Подтвердить", callback_data=f"inv:rc:{item_id}"),
                    InlineKeyboardButton("Отмена", callback_data=f"inv:i:{item_id}"),
                ]]
            ),
        )
    elif action.startswith("inv:rc:"):
        item_id = await _parse_callback_int(query, action.split(":")[2])
        if item_id is None:
            return
        item = await database.get_banner_item(item_id)
        if item is None or int(item["user_id"]) != user.id:
            await query.message.reply_text("Откройте инвентарь заново.")
            return
        payout = CASINO_RECYCLE_PAYOUT_CENTS[int(item["level"])]
        result = await database.recycle_banner_item(user.id, item_id, payout)
        if result == "recycled":
            await query.message.reply_text(f"Баннер переработан. Получено {_coins(payout)} монет.")
            if BACKUP_CHANNEL_ID is not None:
                for backup_field in ("backup_msg_id", "market_backup_msg_id"):
                    if item.get(backup_field):
                        try:
                            await context.bot.delete_message(
                                BACKUP_CHANNEL_ID,
                                int(item[backup_field]),
                            )
                        except TelegramError:
                            logging.exception(
                                "Не удалось удалить копию переработанного баннера из бэкапа."
                            )
            await _show_inventory(
                update,
                context,
                page=(
                    int(context.user_data.get("inventory_list_page", 0))
                    if context.user_data.get("inventory_view") == "list"
                    else max(0, int(context.user_data.get("inventory_page", 0)) - 1)
                ),
                level_filter=str(context.user_data.get("inventory_filter", "all")),
                sort_by=str(context.user_data.get("inventory_sort", "date")),
                view_mode=str(context.user_data.get("inventory_view", "carousel")),
            )
        else:
            explanations = {
                "active": "Активный баннер нельзя переработать, пока баланс не в долгу.",
                "forbidden": "Этот баннер нельзя переработать.",
                "missing": "Откройте инвентарь заново.",
            }
            await query.message.reply_text(explanations.get(result, "Переработка не выполнена."))
    elif action == "mkt:search":
        context.user_data["market_search_waiting"] = True
        await query.message.reply_text(
            "Напиши, что искать: название, редкость, цену или seed. "
            "Слова можно вводить в любом порядке; поиск приблизительный.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("❌ Отмена", callback_data="casino:cancel")]]
            ),
        )
    elif action == "mkt:clear":
        context.user_data.pop("market_search_query", None)
        context.user_data.pop("market_search_waiting", None)
        await _show_market(
            update,
            context,
            page=0,
            level_filter="all",
            sort_by="date",
            search_query="",
        )
    elif action.startswith("mkt:p:"):
        _, _, page_value, level_filter, sort_by = action.split(":")
        await _show_market(
            update,
            context,
            int(page_value),
            level_filter,
            sort_by,
        )
    elif action == "mkt:mine":
        await _show_my_lots(update)
    elif action.startswith("mkt:l:"):
        item_id = await _parse_callback_int(query, action.split(":")[2])
        if item_id is None:
            return
        item = await database.get_banner_item(item_id)
        if item is None or int(item["user_id"]) != user.id:
            await query.message.reply_text("Откройте инвентарь заново.")
            return
        context.user_data["market_listing_item"] = item_id
        await query.message.reply_text(
            f"Укажи цену баннера #{item_id} в монетах (например, 12.50).",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("❌ Отмена", callback_data="casino:cancel")]]
            ),
        )
    elif action.startswith("mkt:b:"):
        lot_id = await _parse_callback_int(query, action.split(":")[2])
        if lot_id is None:
            return
        await database.ensure_user(
            user.id,
            query.message.chat_id,
            user.username,
            user.first_name or user.username,
        )
        result, item = await database.buy_market_lot(user.id, user.username, lot_id)
        if result != "bought" or item is None:
            messages = {
                "sold": "Лот уже продан или снят.",
                "own": "Нельзя купить собственный баннер.",
                "funds": "Недостаточно монет.",
            }
            await query.message.reply_text(messages.get(result, "Покупка не выполнена."))
            return
        await query.message.reply_text("Покупка завершена. Баннер обновляется под твой профиль…")
        item = await ensure_banner_file(context.bot, item, priority=0, force=True)
        await send_banner(
            context.bot,
            query.message.chat_id,
            str(item["file_id"]),
            animated=int(item["level"]) == 6,
            caption=f"Куплен баннер · {_RARITY[int(item['level'])]}",
        )
        await _show_market(
            update,
            context,
            page=int(context.user_data.get("market_page", 0)),
            level_filter=str(context.user_data.get("market_filter", "all")),
            sort_by=str(context.user_data.get("market_sort", "date")),
        )
    elif action.startswith("mkt:c:"):
        lot_id = await _parse_callback_int(query, action.split(":")[2])
        if lot_id is None:
            return
        removed = await database.withdraw_market_lot(user.id, lot_id)
        await query.message.reply_text(
            "Лот снят с продажи." if removed else "Лот уже снят или недоступен."
        )
        if removed:
            await _show_my_lots(update)
    elif action == "bonus:go":
        await _bonus_prompt(update, context)
    elif action == "bonus:chk":
        await _check_bonus(update, context)


async def _handle_listing_price(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> bool:
    message = update.effective_message
    user = update.effective_user
    if (
        message is None
        or user is None
        or message.chat.type != ChatType.PRIVATE
        or not context.user_data.get("market_listing_item")
    ):
        return False
    match = _PRICE_RE.fullmatch(message.text or "")
    if match is None:
        await message.reply_text(
            "Укажи положительную цену, например 12.50.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("❌ Отмена", callback_data="casino:cancel")]]
            ),
        )
        return True
    cents = int(match.group(1)) * 100 + int((match.group(2) or "0").ljust(2, "0"))
    if cents <= 0:
        await message.reply_text(
            "Цена должна быть больше нуля.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("❌ Отмена", callback_data="casino:cancel")]]
            ),
        )
        return True
    item_id = int(context.user_data["market_listing_item"])
    context.user_data.pop("market_listing_item", None)
    context.user_data["market_listing_title_item"] = item_id
    context.user_data["market_listing_price_cents"] = cents
    await message.reply_text(
        "Придумай название лота (до 48 символов), например «Розовый элитный». "
        "Отправь «-», если хочешь оставить лот без названия.",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("❌ Отмена", callback_data="casino:cancel")]]
        ),
    )
    return True


async def _handle_listing_title(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> bool:
    message = update.effective_message
    user = update.effective_user
    item_id = context.user_data.get("market_listing_title_item")
    if (
        message is None
        or user is None
        or message.chat.type != ChatType.PRIVATE
        or not isinstance(item_id, int)
    ):
        return False
    raw_title = re.sub(r"\s+", " ", (message.text or "").strip())
    if raw_title.casefold() == "/cancel":
        context.user_data.pop("market_listing_title_item", None)
        context.user_data.pop("market_listing_price_cents", None)
        await message.reply_text("Выставление лота отменено.")
        return True
    title = None if raw_title == "-" else raw_title
    if title is not None and (
        not title or len(title) > 48 or title.startswith("/")
    ):
        await message.reply_text(
            "Название должно быть от 1 до 48 символов. "
            "Или отправь «-», чтобы оставить лот без названия.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("❌ Отмена", callback_data="casino:cancel")]]
            ),
        )
        return True
    price_cents = context.user_data.get("market_listing_price_cents")
    if not isinstance(price_cents, int):
        context.user_data.pop("market_listing_title_item", None)
        await message.reply_text("Не нашла цену лота. Начни выставление заново.")
        return True
    context.user_data.pop("market_listing_title_item", None)
    context.user_data.pop("market_listing_price_cents", None)
    inventory_message_id = context.user_data.pop("inventory_message_id", None)
    inventory_view = str(context.user_data.get("inventory_view", "carousel"))
    inventory_page = (
        int(context.user_data.get("inventory_list_page", 0))
        if inventory_view == "list"
        else max(0, int(context.user_data.get("inventory_page", 0)) - 1)
    )
    inventory_filter = str(context.user_data.get("inventory_filter", "all"))
    inventory_sort = str(context.user_data.get("inventory_sort", "date"))
    result = await database.create_market_lot(
        user.id,
        item_id,
        price_cents,
        title=title,
    )
    responses = {
        "listed": "Баннер выставлен на площадку.",
        "debt": "Пока баланс отрицательный, продавать баннеры нельзя.",
        "missing": "Баннер не найден в твоём инвентаре.",
        "forbidden": "Активный баннер и баннер владельца продавать нельзя.",
    }
    await message.reply_text(responses.get(result, "Не удалось выставить баннер."))
    if result == "listed":
        if isinstance(inventory_message_id, int):
            try:
                await context.bot.delete_message(
                    chat_id=message.chat_id,
                    message_id=inventory_message_id,
                )
            except TelegramError:
                logging.exception(
                    "Не удалось убрать выставленный баннер из инвентаря user_id=%s.",
                    user.id,
                )
        await _show_inventory(
            update,
            context,
            page=inventory_page,
            level_filter=inventory_filter,
            sort_by=inventory_sort,
            view_mode=inventory_view,
        )
    return True


async def _handle_market_search_input(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> bool:
    message = update.effective_message
    if not context.user_data.get("market_search_waiting"):
        return False
    if message is None or message.chat.type != ChatType.PRIVATE or not message.text:
        return False
    query = message.text.strip()
    context.user_data.pop("market_search_waiting", None)
    if query.casefold() == "/cancel":
        await message.reply_text("Поиск отменён.")
        return True
    if not query or len(query) > 100:
        context.user_data["market_search_waiting"] = True
        await message.reply_text(
            "Запрос должен содержать от 1 до 100 символов.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("❌ Отмена", callback_data="casino:cancel")]]
            ),
        )
        return True
    await _show_market(
        update,
        context,
        page=0,
        level_filter="all",
        sort_by="relevance",
        search_query=query,
    )
    return True


async def _handle_recycle_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> bool:
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None or message.text is None:
        return False
    match = _RECYCLE_COMMAND_RE.fullmatch(message.text.strip())
    if match is None:
        return False
    level = _RARITY_ALIASES.get(match.group("rarity").casefold())
    if level is None:
        await message.reply_text(
            "Не распознала редкость. Используй обычный, необычный, редкий, "
            "эпический, элитный или легендарный."
        )
        return True
    quantity = int(match.group("quantity"))
    if not 1 <= quantity <= 30:
        await message.reply_text("За одну команду можно переработать от 1 до 30 баннеров.")
        return True
    try:
        await database.ensure_user(
            user.id,
            message.chat_id,
            user.username,
            user.first_name or user.username,
        )
        await database.get_duel_balance(user.id, user.username)
        payout = CASINO_RECYCLE_PAYOUT_CENTS[level]
        result, items, balance = await database.recycle_banner_items_by_level(
            user.id,
            level,
            quantity,
            payout,
        )
    except Exception:
        logging.exception(
            "Не удалось переработать баннеры user_id=%s level=%s quantity=%s.",
            user.id,
            level,
            quantity,
        )
        await message.reply_text("Не удалось переработать баннеры. Попробуй позже.")
        return True
    if result == "insufficient":
        await message.reply_text(
            f"Не хватает свободных баннеров редкости «{_RARITY[level]}»: "
            f"нужно {quantity}. Активные и выставленные на продажу не трогаю."
        )
        return True
    if result != "recycled" or balance is None:
        await message.reply_text("Не удалось найти игровой баланс. Открой казино и попробуй снова.")
        return True
    if BACKUP_CHANNEL_ID is not None:
        for item in items:
            for field in ("backup_msg_id", "market_backup_msg_id"):
                backup_message_id = item.get(field)
                if backup_message_id is None:
                    continue
                try:
                    await context.bot.delete_message(
                        chat_id=BACKUP_CHANNEL_ID,
                        message_id=int(backup_message_id),
                    )
                except TelegramError:
                    logging.exception(
                        "Не удалось удалить резервное изображение переработанного баннера %s.",
                        item["item_id"],
                    )
    await message.reply_text(
        f"♻️ Переработано: {_RARITY[level]} ×{quantity}. "
        f"Получено {_coins(payout * quantity)} монет. Баланс: {_coins(balance)}."
    )
    return True


async def _activate_code(update: Update, code: str | None) -> None:
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None:
        return
    if message.chat.type != ChatType.PRIVATE:
        await message.reply_text("Ключи и промокоды активируются только в личном чате с Юбарой.")
        return
    if not code:
        await message.reply_text(
            "Отправь код сообщением или нажми «🔑 Активировать код» в меню."
        )
        return

    try:
        access = await database.consume_access_key(code, user.id, user.username)
        if access is not None:
            level = str(access["access_level"])
            logging.info(
                "Ключ доступа активирован user_id=%s access_level=%s.",
                user.id,
                level,
            )
            descriptions = {
                "admin": "Доступ администратора активирован.",
                "autoresponder": (
                    "Ключ автоответчика активирован. Открой /addacc, "
                    "чтобы подключить Telegram-аккаунт."
                ),
                "user": "Доступ к боту активирован.",
            }
            await message.reply_text(
                descriptions.get(level, "Ключ доступа активирован."),
                reply_markup=InlineKeyboardMarkup([[_menu_button()]]),
            )
            return
        if code.upper().startswith("YUBARA-COIN-"):
            result, amount, balance = await database.redeem_coin_promo_code(
                code,
                user.id,
                user.username,
            )
            if result == "redeemed" and amount is not None and balance is not None:
                logging.info(
                    "Промокод монет активирован user_id=%s amount_cents=%s balance_cents=%s.",
                    user.id,
                    amount,
                    balance,
                )
                await message.reply_text(
                    f"Промокод активирован: +{_coins(amount)} монет. "
                    f"Баланс: {_coins(balance)}.",
                    reply_markup=InlineKeyboardMarkup([[_menu_button()]]),
                )
                return
            if result == "used":
                await message.reply_text("Этот промокод уже использован.")
                return
        elif code.upper().startswith("YUBARA-PEN-"):
            result, amount, balance = await database.redeem_coin_penalty_code(
                code,
                user.id,
                user.username,
            )
            if result == "redeemed" and amount is not None and balance is not None:
                logging.info(
                    "Штрафной ключ активирован user_id=%s amount_cents=%s balance_cents=%s.",
                    user.id,
                    amount,
                    balance,
                )
                await message.reply_text(
                    f"Штрафной ключ активирован: −{_coins(amount)} монет. "
                    f"Новый баланс: {_coins(balance)}.",
                    reply_markup=InlineKeyboardMarkup([[_menu_button()]]),
                )
                return
            if result == "used":
                await message.reply_text("Этот штрафной ключ уже активирован.")
                return
    except Exception:
        logging.exception(
            "Не удалось активировать ключ/промокод user_id=%s.",
            user.id,
        )
        await message.reply_text("Не удалось активировать код. Попробуй позже.")
        return
    await message.reply_text("Код не найден или указан неверно.")


async def handle_activation_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE | None = None,
) -> bool:
    message = update.effective_message
    if message is None or message.text is None:
        return False
    if (
        context is not None
        and message.chat.type == ChatType.PRIVATE
        and "activation_code_waiting_until" in context.user_data
    ):
        expires_at = context.user_data.get("activation_code_waiting_until")
        if not isinstance(expires_at, (int, float)) or time.monotonic() > expires_at:
            context.user_data.pop("activation_code_waiting_until", None)
            await message.reply_text(
                "Время ввода кода истекло. Нажми «🔑 Активировать код» в меню ещё раз."
            )
            return True
        else:
            code = message.text.strip()
            if code.casefold() in {"отмена", "отменить", "в меню", "/cancel"}:
                context.user_data.pop("activation_code_waiting_until", None)
                await message.reply_text(
                    "Ввод кода отменён.",
                    reply_markup=InlineKeyboardMarkup([[_menu_button()]]),
                )
                return True
            if not code:
                await message.reply_text("Отправь код одним сообщением или нажми «Отмена».")
                return True
            context.user_data.pop("activation_code_waiting_until", None)
            command_match = _ACTIVATE_CODE_RE.fullmatch(code)
            if command_match is None:
                if _COIN_PROMO_RE.fullmatch(code) is not None:
                    await handle_coin_promo_message(update)
                    return True
                await _activate_code(update, code)
                return True
            code = command_match.group(1).strip() if command_match.group(1) else None
            await _activate_code(update, code)
            return True
    match = _ACTIVATE_CODE_RE.fullmatch(message.text.strip())
    if match is None:
        return False
    code = match.group(1).strip() if match.group(1) else None
    await _activate_code(update, code)
    return True


async def handle_error_report_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> bool:
    message = update.effective_message
    user = update.effective_user
    expires_at = context.user_data.get("error_report_waiting_until")
    if (
        message is None
        or user is None
        or message.chat.type != ChatType.PRIVATE
        or expires_at is None
    ):
        return False
    context.user_data.pop("error_report_waiting_until", None)
    if not isinstance(expires_at, (int, float)) or time.monotonic() > expires_at:
        await message.reply_text(
            "Время отправки обращения истекло. Открой справку и нажми "
            "«🐞 Сообщить об ошибке» ещё раз."
        )
        return True
    report = message.text.strip()
    if report.casefold() in {"отмена", "отменить", "/cancel"}:
        await message.reply_text(
            "Отправка сообщения об ошибке отменена.",
            reply_markup=InlineKeyboardMarkup([[_menu_button()]]),
        )
        return True
    if not report:
        await message.reply_text("Сообщение пустое. Открой справку и начни отправку заново.")
        return True
    if len(report) > 3000:
        await message.reply_text(
            "Сообщение слишком длинное. Ограничение — 3000 символов. "
            "Открой справку и попробуй ещё раз."
        )
        return True
    username = f"@{user.username}" if user.username else "не указан"
    report_text = (
        "🐞 <b>Сообщение об ошибке</b>\n"
        f"От: {html.escape(user.full_name)}\n"
        f"Username: {html.escape(username)}\n"
        f"User ID: <code>{user.id}</code>\n\n"
        f"{html.escape(report)}"
    )
    try:
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=report_text,
            parse_mode="HTML",
        )
    except TelegramError:
        logging.exception(
            "Не удалось доставить сообщение об ошибке владельцу user_id=%s.",
            user.id,
        )
        await message.reply_text(
            "Не получилось доставить сообщение владельцу. Попробуй позже."
        )
        return True
    logging.info("Получено сообщение об ошибке от user_id=%s.", user.id)
    await message.reply_text(
        "Спасибо, сообщение об ошибке отправлено владельцу бота.",
        reply_markup=InlineKeyboardMarkup([[_menu_button()]]),
    )
    return True


async def _handle_inventory_transfer_input(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> bool:
    message = update.effective_message
    user = update.effective_user
    item_id_value = context.user_data.get("inventory_transfer_item_id")
    if (
        item_id_value is None
        or message is None
        or user is None
        or message.text is None
        or message.chat.type != ChatType.PRIVATE
    ):
        return False
    text = message.text.strip()
    if text.casefold() in {"отмена", "отменить", "/cancel"}:
        context.user_data.pop("inventory_transfer_item_id", None)
        await message.reply_text(
            "Передача отменена.",
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("🎒 Инвентарь", callback_data="menu:inv")],
                    [_menu_button()],
                ]
            ),
        )
        return True

    recipient_id: int | None = None
    if _TRANSFER_USER_ID_RE.fullmatch(text):
        recipient_id = int(text)
    else:
        match = _TRANSFER_RECIPIENT_RE.fullmatch(text)
        if match is not None:
            try:
                recipient_id = await database.find_user_id_by_username(match.group(1))
            except Exception:
                logging.exception(
                    "Не удалось найти получателя баннера user_id=%s.",
                    user.id,
                )
                await message.reply_text("Не удалось найти получателя. Попробуй позже.")
                return True
    if recipient_id is None:
        await message.reply_text(
            "Не удалось распознать получателя. Отправь @username или Telegram ID "
            "пользователя, который уже взаимодействовал с Юбарой."
        )
        return True
    try:
        result = await database.request_banner_transfer(
            user.id,
            recipient_id,
            int(item_id_value),
        )
    except Exception:
        logging.exception(
            "Ошибка передачи баннера item_id=%s sender_id=%s recipient_id=%s.",
            item_id_value,
            user.id,
            recipient_id,
        )
        await message.reply_text("Не удалось передать баннер. Попробуй позже.")
        return True
    if result == "pending":
        context.user_data.pop("inventory_transfer_item_id", None)
        logging.info(
            "Создан запрос передачи баннера item_id=%s sender_id=%s recipient_id=%s.",
            item_id_value,
            user.id,
            recipient_id,
        )
        private_notified, notified_groups = await _notify_banner_transfer(
            context,
            sender_id=user.id,
            recipient_id=recipient_id,
            item_id=int(item_id_value),
        )
        if not private_notified and notified_groups == 0:
            cancelled = False
            try:
                cancel_result, _ = await database.cancel_banner_transfer(
                    int(item_id_value),
                    user.id,
                )
                cancelled = cancel_result == "cancelled"
            except Exception:
                logging.exception(
                    "Не удалось отменить недоставленный запрос передачи item_id=%s.",
                    item_id_value,
                )
            await message.reply_text(
                "Не удалось доставить запрос получателю ни в ЛС, ни в его группах. "
                + (
                    "Баннер остался у тебя; попробуй позже."
                    if cancelled
                    else "Не удалось отменить запрос автоматически; проверь инвентарь "
                    "и попробуй снова."
                )
            )
            return True
        notification_status = (
            f"Запрос отправлен в ЛС и {notified_groups} группах."
            if private_notified
            else (
                f"Запрос отправлен в {notified_groups} группах, "
                "но не удалось доставить его в ЛС."
            )
        )
        await message.reply_text(
            f"Запрос на передачу баннера #{item_id_value} отправлен пользователю "
            f"{recipient_id}. Предмет останется у тебя, пока получатель не примет его. "
            f"{notification_status}",
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("🎒 Инвентарь", callback_data="menu:inv")],
                    [_menu_button()],
                ]
            ),
        )
    else:
        explanations = {
            "same_user": "Нельзя передать баннер самому себе.",
            "missing": "Этот баннер больше не принадлежит тебе.",
            "owner_item": "Служебный баннер передавать нельзя.",
            "listed": "Сначала сними баннер с площадки.",
            "already_pending": "Для этого баннера уже ожидается решение по передаче.",
            "pending": "Для этого баннера уже ожидается решение по передаче.",
            "recipient_not_found": (
                "Получатель не найден. Он должен сначала открыть бота "
                "или написать в группу с Юбарой."
            ),
        }
        await message.reply_text(explanations.get(result, "Передача не выполнена."))
    return True


def _transfer_display_name(profile: dict[str, Any] | None, user_id: int) -> str:
    if profile is None:
        return f"Пользователь {user_id}"
    return str(
        profile.get("callsign")
        or profile.get("display_name")
        or profile.get("username")
        or f"Пользователь {user_id}"
    )


async def _handle_pending_transfer_item(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    item: dict[str, Any],
) -> bool:
    query = update.callback_query
    user = update.effective_user
    if query is None or user is None or query.message is None:
        return False
    pending = await database.get_pending_banner_transfer(int(item["item_id"]))
    if pending is None:
        return False
    item_id = int(item["item_id"])
    sender_id = int(pending["sender_id"])
    recipient_id = int(pending["recipient_id"])
    if sender_id == user.id:
        text = f"⏳ Передача баннера #{item_id} ожидает решения пользователя {recipient_id}."
        buttons = [
            [
                InlineKeyboardButton(
                    "❌ Отменить передачу",
                    callback_data=f"inv:tx:{item_id}",
                )
            ],
            [
                InlineKeyboardButton("↩️ Инвентарь", callback_data="menu:inv"),
                _menu_button(),
            ],
        ]
    else:
        text = f"⏳ Баннер #{item_id} ожидает подтверждения получателя."
        buttons = [[_menu_button()]]
    await send_banner(
        context.bot,
        query.message.chat_id,
        str(item["file_id"]),
        animated=int(item["level"]) == 6,
        caption=text,
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return True


async def _notify_banner_transfer(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    sender_id: int,
    recipient_id: int,
    item_id: int,
) -> tuple[bool, int]:
    item: dict[str, Any] | None = None
    try:
        item = await database.get_banner_item(item_id)
    except Exception:
        logging.exception(
            "Не удалось получить баннер для уведомления transfer_item_id=%s.",
            item_id,
        )
    if item is None:
        logging.error(
            "Переданный баннер не найден для уведомления item_id=%s recipient_id=%s.",
            item_id,
            recipient_id,
        )
        rarity = "баннер"
    else:
        rarity = _RARITY.get(int(item["level"]), "баннер")

    try:
        sender_profile = await database.get_global_profile(sender_id)
        recipient_profile = await database.get_global_profile(recipient_id)
    except Exception:
        logging.exception(
            "Не удалось получить профили для уведомления о передаче "
            "item_id=%s sender_id=%s recipient_id=%s.",
            item_id,
            sender_id,
            recipient_id,
        )
        sender_profile = None
        recipient_profile = None
    sender_name = _transfer_display_name(sender_profile, sender_id)
    recipient_name = _transfer_display_name(recipient_profile, recipient_id)
    sender_link = f'<a href="tg://user?id={sender_id}">{html.escape(sender_name)}</a>'
    recipient_link = (
        f'<a href="tg://user?id={recipient_id}">{html.escape(recipient_name)}</a>'
    )
    private_sent = False
    try:
        await context.bot.send_message(
            chat_id=recipient_id,
            text=(
                f"🎁 {sender_link} предлагает тебе баннер #{item_id} "
                f"({html.escape(rarity)}). Прими передачу или верни запрос."
            ),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "✅ Принять",
                            callback_data=f"inv:ta:{item_id}",
                        ),
                        InlineKeyboardButton(
                            "↩️ Вернуть",
                            callback_data=f"inv:tr:{item_id}",
                        ),
                    ]
                ]
            ),
        )
        private_sent = True
    except TelegramError:
        logging.warning(
            "Не удалось отправить получателю ЛС о передаче баннера "
            "item_id=%s sender_id=%s recipient_id=%s.",
            item_id,
            sender_id,
            recipient_id,
            exc_info=True,
        )

    notified_groups = 0
    try:
        memberships = set(await database.list_user_group_memberships(recipient_id))
        allowed_groups = set(await database.list_allowed_groups())
    except Exception:
        logging.exception(
            "Не удалось получить список групп получателя transfer_item_id=%s "
            "recipient_id=%s.",
            item_id,
            recipient_id,
        )
        return private_sent, notified_groups

    group_text = (
        f"🎁 {sender_link} предлагает баннер #{item_id} ({html.escape(rarity)}) "
        f"пользователю {recipient_link}. Запрос ожидает решения получателя."
    )
    for group_id in sorted(memberships & allowed_groups):
        try:
            member = await context.bot.get_chat_member(group_id, recipient_id)
            status = getattr(member.status, "value", member.status)
            is_member = status in ("creator", "administrator", "member") or (
                status == "restricted" and bool(getattr(member, "is_member", False))
            )
            if not is_member:
                await database.set_group_membership(
                    group_id,
                    recipient_id,
                    is_member=False,
                )
                continue
            await context.bot.send_message(
                chat_id=group_id,
                text=group_text,
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "✅ Принять",
                                callback_data=f"inv:ta:{item_id}",
                            ),
                            InlineKeyboardButton(
                                "↩️ Вернуть",
                                callback_data=f"inv:tr:{item_id}",
                            ),
                        ]
                    ]
                ),
            )
            notified_groups += 1
        except TelegramError:
            logging.warning(
                "Не удалось отправить в группу %s уведомление о передаче "
                "item_id=%s recipient_id=%s.",
                group_id,
                item_id,
                recipient_id,
                exc_info=True,
            )
        except Exception:
            logging.exception(
                "Ошибка уведомления группы %s о передаче item_id=%s recipient_id=%s.",
                group_id,
                item_id,
                recipient_id,
            )
    return private_sent, notified_groups


async def _notify_transfer_resolution(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    sender_id: int,
    recipient_id: int,
    item_id: int,
    accepted: bool,
) -> None:
    if accepted:
        try:
            await context.bot.send_message(
                chat_id=sender_id,
                text=(
                    f"✅ Пользователь {recipient_id} принял передачу баннера "
                    f"#{item_id}. Предмет теперь у получателя."
                ),
            )
        except TelegramError:
            logging.warning(
                "Не удалось уведомить отправителя user_id=%s о принятии "
                "transfer_item_id=%s.",
                sender_id,
                item_id,
                exc_info=True,
            )
    else:
        try:
            await context.bot.send_message(
                chat_id=sender_id,
                text=(
                    f"↩️ Пользователь {recipient_id} вернул запрос на баннер "
                    f"#{item_id}. Предмет остался у тебя."
                ),
            )
        except TelegramError:
            logging.warning(
                "Не удалось уведомить отправителя user_id=%s об отклонении "
                "transfer_item_id=%s.",
                sender_id,
                item_id,
                exc_info=True,
            )

    try:
        memberships = set(await database.list_user_group_memberships(recipient_id))
        allowed_groups = set(await database.list_allowed_groups())
    except Exception:
        logging.exception(
            "Не удалось получить группы для результата передачи item_id=%s.",
            item_id,
        )
        return

    try:
        sender_profile = await database.get_global_profile(sender_id)
        recipient_profile = await database.get_global_profile(recipient_id)
    except Exception:
        logging.exception(
            "Не удалось получить профили для результата передачи item_id=%s.",
            item_id,
        )
        sender_profile = None
        recipient_profile = None
    sender_link = (
        f'<a href="tg://user?id={sender_id}">'
        f"{html.escape(_transfer_display_name(sender_profile, sender_id))}</a>"
    )
    recipient_link = (
        f'<a href="tg://user?id={recipient_id}">'
        f"{html.escape(_transfer_display_name(recipient_profile, recipient_id))}</a>"
    )
    group_text = (
        f"✅ {recipient_link} принял баннер #{item_id} от {sender_link}."
        if accepted
        else f"↩️ {recipient_link} вернул запрос на баннер #{item_id} от {sender_link}."
    )
    for group_id in sorted(memberships & allowed_groups):
        try:
            member = await context.bot.get_chat_member(group_id, recipient_id)
            status = getattr(member.status, "value", member.status)
            if status not in ("creator", "administrator", "member") and not (
                status == "restricted" and bool(getattr(member, "is_member", False))
            ):
                await database.set_group_membership(
                    group_id,
                    recipient_id,
                    is_member=False,
                )
                continue
            await context.bot.send_message(
                chat_id=group_id,
                text=group_text,
                parse_mode="HTML",
            )
        except TelegramError:
            logging.warning(
                "Не удалось отправить группе %s подтверждение передачи item_id=%s.",
                group_id,
                item_id,
                exc_info=True,
            )
        except Exception:
            logging.exception(
                "Ошибка отправки подтверждения передачи в группу %s item_id=%s.",
                group_id,
                item_id,
            )


async def handle_game_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> bool:
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None or message.text is None:
        return False
    if await _handle_inventory_transfer_input(update, context):
        return True
    if await _handle_listing_title(update, context):
        return True
    if await _handle_listing_price(update, context):
        return True
    if await _handle_market_search_input(update, context):
        return True
    if await _handle_recycle_command(update, context):
        return True
    text = message.text.strip().casefold()
    if re.fullmatch(r"/(?:help|adminhelp|helpadmin)(?:@\w+)?", text):
        await _show_admin_help(update, context)
        return True
    if await handle_activation_message(update, context):
        return True
    start_match = re.fullmatch(r"/start(?:@\w+)?(?:\s+(\w+))?", text)
    if start_match and start_match.group(1) == "inventory":
        await _show_inventory(update, context)
        return True
    if start_match and start_match.group(1) == "casino":
        await open_casino(update, context)
        return True
    if start_match:
        await open_main_menu(update, context)
        return True
    if re.fullmatch(r"/(?:fortuna|casino|slots)(?:@\w+)?", text) or text in {
        "казино",
        "колесо удачи",
        "казик",
        "крутка",
        "🎰 казино",
        "🎡 колесо удачи",
    }:
        await open_casino(update, context)
        return True
    if re.fullmatch(r"/(?:inventory|inv)(?:@\w+)?", text) or text in {
        "инвентарь",
        "сменить банер",
        "сменить баннер",
        "🎒 инвентарь",
    }:
        await _show_inventory(update, context)
        return True
    if re.fullmatch(r"/banner(?:@\w+)?", text) or text in {"мой баннер", "мой банер"}:
        try:
            media = await get_profile_banner(context.bot, user.id)
            if media is None:
                await message.reply_text(
                    "У тебя пока нет активного баннера. Попытай удачу в Колесе удачи.",
                    reply_markup=InlineKeyboardMarkup(
                        [[InlineKeyboardButton("🎡 Колесо удачи", callback_data="casino:menu")]]
                    ),
                )
            else:
                payload, animated = media
                await send_banner(
                    context.bot,
                    message.chat_id,
                    payload,
                    animated=animated,
                    caption="Твой профильный баннер.",
                )
        except Exception:
            logging.exception("Не удалось показать баннер user_id=%s.", user.id)
            await message.reply_text("Не удалось открыть баннер. Попробуй позже.")
        return True
    market_command = _MARKET_COMMAND_RE.fullmatch(message.text.strip())
    market_text = _MARKET_TEXT_RE.fullmatch(message.text.strip())
    if market_command is not None or market_text is not None or text in {
        "площадка",
        "маркет",
        "маркетплейс",
        "🏪 площадка",
    }:
        query = (
            market_command.group(1)
            if market_command is not None
            else market_text.group(1) if market_text is not None else ""
        )
        await _show_market(
            update,
            context,
            0,
            "all",
            "relevance" if query else "date",
            search_query=query,
        )
        return True
    if re.fullmatch(r"/fees(?:@\w+)?", text):
        if user.id != ADMIN_ID and not await database.is_admin_user(user.id):
            await message.reply_text("Команда доступна только администраторам бота.")
            return True
        try:
            fee = await database.get_market_fee_cents()
            await message.reply_text(f"Накопленная комиссия площадки: {_coins(fee)} монет.")
        except Exception:
            logging.exception("Не удалось прочитать комиссию маркетплейса.")
            await message.reply_text("Не удалось получить сумму комиссии.")
        return True
    if text in {"бонус", "бонус подписка", "🎁 бонус"}:
        await _bonus_prompt(update, context)
        return True
    if text in {"🏠 главное меню", "главное меню"}:
        await open_main_menu(update, context)
        return True
    return False


async def handle_coin_promo_message(update: Update) -> bool:
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None or message.text is None:
        return False
    match = _COIN_PROMO_RE.fullmatch(message.text.strip())
    if match is None:
        return False
    if message.chat.type != ChatType.PRIVATE:
        return True
    try:
        result, credited, balance = await database.redeem_coin_promo_code(
            match.group(1),
            user.id,
            user.username,
        )
    except Exception:
        logging.exception("Не удалось активировать промокод монет user_id=%s.", user.id)
        await message.reply_text("Не удалось активировать промокод. Попробуй позже.")
        return True
    if result == "redeemed" and credited is not None and balance is not None:
        await message.reply_text(
            f"Промокод активирован! Начислено {_coins(credited)} "
            f"монет. Баланс: {_coins(balance)}."
        )
    elif result == "used":
        await message.reply_text("Этот промокод уже использован.")
    else:
        await message.reply_text("Промокод не найден или указан неверно.")
    return True


async def test_banner_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None or message.chat.type != ChatType.PRIVATE:
        return
    if user.id != ADMIN_ID and not await database.is_admin_user(user.id):
        return

    args = list(context.args or [])
    try:
        level = int(args[0]) if args else 6
    except ValueError:
        await message.reply_text("Использование: /testbanner [уровень 1-6] [паттерн]")
        return
    if (
        not 1 <= level <= 6
        or len(args) > 2
        or (len(args) == 2 and level != 6)
    ):
        await message.reply_text("Использование: /testbanner [уровень 1-6] [паттерн]")
        return

    seed = secrets.token_hex(16)
    preset = args[1] if len(args) == 2 else pick_preset(
        make_digest(user.id, item_seed=seed)
    )
    if preset not in PRESET_NAMES:
        await message.reply_text(
            "Неизвестный паттерн. Доступные: "
            + ", ".join(sorted(PRESET_NAMES))
        )
        return

    progress = await message.reply_text("Готовлю тестовый баннер…")
    try:
        buffer = await render_banner_preview(
            priority=0,
            user_id=user.id,
            name=user.full_name,
            tag=user.username,
            level=level,
            item_seed=seed,
            preset=preset,
            salt="yubari",
            show_name=True,
        )
        await send_banner(
            context.bot,
            message.chat_id,
            buffer,
            animated=level == 6,
            caption=(
                f"🧪 Тестовый баннер · уровень {level}"
                f"{f' · паттерн {PRESET_NAMES[preset]}' if level == 6 else ''}"
            ),
        )
        await progress.delete()
    except Exception:
        logging.exception(
            "Не удалось создать тестовый баннер user_id=%s level=%s.",
            user.id,
            level,
        )
        await progress.edit_text("Не удалось создать тестовый баннер. Ошибка записана в журнал.")
        raise
