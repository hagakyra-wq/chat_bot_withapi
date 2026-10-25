"""Telegram-authenticated API operations for the Mini App."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import html
import logging
import re
import secrets
import time
from types import SimpleNamespace
from typing import Any
from urllib.parse import quote

import httpx
import database
from casino_rules import RARITY_NAMES, RARITY_WEIGHTS, generate_result
from config import (
    ADMIN_ID,
    BACKUP_CHANNEL_ID,
    CASINO_RECYCLE_PAYOUT_CENTS,
    CASINO_SPIN_COST_CENTS,
    TELEGRAM_TOKEN,
)
from telegram.error import TelegramError
from yubari_banner import PRESET_NAMES

_MEDIA_LINK_LIFETIME_SECONDS = 600
_MAX_BANNER_MEDIA_SIZE = 20 * 1024 * 1024
_BANNER_ART_TASKS: set[asyncio.Task[None]] = set()
_TRANSFER_USERNAME_RE = re.compile(r"@?([a-z0-9_]{5,32})", re.IGNORECASE)
_TRANSFER_USER_ID_RE = re.compile(r"\d{1,19}")


def _is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID


async def _community_groups(user_id: int, bot: Any | None) -> list[dict[str, Any]]:
    memberships = set(await database.list_user_group_memberships(user_id))
    allowed_groups = set(await database.list_allowed_groups())
    groups = []
    for group_id in sorted(memberships & allowed_groups):
        title = f"Группа {group_id}"
        if bot is not None:
            try:
                chat = await bot.get_chat(group_id)
                title = str(chat.title or title)
            except TelegramError:
                logging.warning(
                    "Не удалось получить название группы group_id=%s для Mini App.",
                    group_id,
                    exc_info=True,
                )
        groups.append({"group_id": group_id, "title": title})
    return groups


async def _community_leaderboard(
    query: dict[str, list[str]],
    user_id: int,
) -> tuple[int, dict[str, Any]]:
    try:
        group_id = int(query.get("group_id", [""])[0])
    except ValueError:
        return 400, {"error": "Выбери группу для рейтинга."}
    period = query.get("period", ["d"])[0]
    mode = query.get("mode", ["activity"])[0]
    if period not in ("d", "w", "m"):
        return 400, {"error": "Выбери период: день, неделя или месяц."}
    if mode not in ("activity", "coins"):
        return 400, {"error": "Неизвестный тип рейтинга."}

    memberships = set(await database.list_user_group_memberships(user_id))
    if group_id not in memberships or not await database.is_group_allowed(group_id):
        return 403, {"error": "Рейтинг доступен только участникам подключённых групп."}

    if mode == "coins":
        records = await database.get_group_coin_leaderboard(group_id, limit=20)
        entries = [
            {
                "display_name": str(record["display_name"]),
                "value": int(record["balance_cents"]),
            }
            for record in records
        ]
    else:
        records = await database.get_group_message_leaderboard(
            group_id,
            period,
            limit=20,
        )
        entries = [
            {
                "display_name": str(record["display_name"]),
                "value": int(record["message_count"]),
            }
            for record in records
        ]
    return 200, {"group_id": group_id, "mode": mode, "period": period, "entries": entries}


async def _update_profile(
    user_id: int,
    payload: dict[str, Any],
) -> tuple[int, dict[str, Any]]:
    fields: dict[str, Any] = {}
    callsign = payload.get("callsign")
    if callsign is not None:
        if not isinstance(callsign, str) or len(callsign.strip()) > 80:
            return 400, {"error": "Кличка должна содержать не более 80 символов."}
        if callsign.strip():
            fields["callsign"] = callsign.strip()

    age = payload.get("age")
    if age is not None:
        if type(age) is not int or not 1 <= age <= 120:
            return 400, {"error": "Возраст должен быть целым числом от 1 до 120."}
        fields["age"] = age

    gender = payload.get("gender")
    if gender is not None:
        if gender not in ("парень", "девушка"):
            return 400, {"error": "Выбери корректный вариант пола."}
        fields["gender"] = gender

    note = payload.get("note")
    if note is not None:
        if not isinstance(note, str) or len(note.strip()) > 500:
            return 400, {"error": "Факт должен содержать не более 500 символов."}
        if note.strip():
            fields["note"] = note.strip()
    if not fields:
        return 400, {"error": "Укажи хотя бы одно новое значение профиля."}

    memberships = set(await database.list_user_group_memberships(user_id))
    allowed_groups = set(await database.list_allowed_groups())
    group_ids = sorted(memberships & allowed_groups)
    if not group_ids:
        return 403, {"error": "Изменение профиля доступно участникам подключённых групп."}
    if not await database.update_user_profile(user_id, group_ids[0], **fields):
        return 404, {"error": "Профиль пока нельзя изменить. Напиши Юбаре в подключённой группе."}
    return 200, {"status": "updated", "message": "Профиль обновлён."}


async def _submit_error_report(
    user_id: int,
    first_name: str,
    username: str | None,
    payload: dict[str, Any],
    bot: Any | None,
) -> tuple[int, dict[str, Any]]:
    report = payload.get("message")
    if not isinstance(report, str) or not 1 <= len(report.strip()) <= 3000:
        return 400, {"error": "Опиши проблему сообщением от 1 до 3000 символов."}
    if bot is None:
        return 503, {"error": "Отправка обращений временно недоступна."}

    username_label = f"@{username}" if username else "не указан"
    text = (
        "🐞 Сообщение об ошибке из Mini App\n"
        f"От: {html.escape(first_name)}\n"
        f"Username: {html.escape(username_label)}\n"
        f"User ID: <code>{user_id}</code>\n\n"
        f"{html.escape(report.strip())}"
    )
    try:
        await bot.send_message(chat_id=ADMIN_ID, text=text, parse_mode="HTML")
    except TelegramError:
        logging.exception(
            "Не удалось доставить Mini App обращение владельцу user_id=%s.",
            user_id,
        )
        return 503, {"error": "Не получилось доставить обращение. Попробуй позже."}
    logging.info("Получено Mini App обращение об ошибке от user_id=%s.", user_id)
    return 200, {"status": "sent", "message": "Спасибо! Обращение отправлено владельцу бота."}


async def _offer_image_request(
    user_id: int,
    payload: dict[str, Any],
    bot: Any | None,
) -> tuple[int, dict[str, Any]]:
    from images import PROMPT_MAX_LENGTH, image_manager

    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not 1 <= len(prompt.strip()) <= PROMPT_MAX_LENGTH:
        return 400, {
            "error": (
                "Описание изображения должно содержать от 1 до "
                f"{PROMPT_MAX_LENGTH} символов."
            ),
        }
    if bot is None:
        return 503, {"error": "Генерация изображений временно недоступна."}
    memberships = set(await database.list_user_group_memberships(user_id))
    allowed_groups = set(await database.list_allowed_groups())
    if not memberships.intersection(allowed_groups) and not await _has_admin_access(user_id):
        return 403, {"error": "Генерация доступна участникам подключённых групп."}

    try:
        await image_manager.offer_webapp(bot, user_id, prompt)
    except TelegramError:
        return 409, {
            "error": "Не удалось написать тебе в Telegram. Сначала открой личный чат с Юбарой.",
        }
    return 202, {
        "status": "queued",
        "message": "Запрос отправлен в чат с Юбарой. Продолжи генерацию там.",
    }


async def _has_admin_access(user_id: int) -> bool:
    return _is_admin(user_id) or await database.is_admin_user(user_id)


def _media_signature(user_id: int, item_id: int, expires: int) -> str:
    secret = hashlib.sha256(TELEGRAM_TOKEN.encode("utf-8")).digest()
    message = f"{user_id}:{item_id}:{expires}".encode("ascii")
    return hmac.new(secret, message, hashlib.sha256).hexdigest()


def _media_url(user_id: int, item_id: int) -> str:
    expires = int(time.time()) + _MEDIA_LINK_LIFETIME_SECONDS
    signature = _media_signature(user_id, item_id, expires)
    return f"/media/banner/{item_id}?user={user_id}&expires={expires}&sig={signature}"


async def get_session(user_id: int, first_name: str) -> tuple[int, dict[str, Any]]:
    is_admin = await _has_admin_access(user_id)
    return 200, {
        "user_id": user_id,
        "first_name": first_name,
        "role": "admin" if is_admin else "user",
    }


async def _profile(
    user_id: int,
    first_name: str,
    username: str | None,
) -> dict[str, Any]:
    profile = await database.get_global_profile(user_id) or {}
    activity = await database.get_user_message_activity(user_id)
    balance = await database.get_duel_balance(user_id, username)
    active_banner = await database.get_active_banner_item(user_id)
    inventory = await database.get_user_banner_items(user_id, limit=100)
    incoming_transfers = await database.get_user_pending_banner_transfers(
        user_id,
        direction="incoming",
    )
    outgoing_transfers = await database.get_user_pending_banner_transfers(
        user_id,
        direction="outgoing",
    )
    pending_transfers: dict[str, list[dict[str, Any]]] = {}
    for direction, transfers in (
        ("incoming", incoming_transfers),
        ("outgoing", outgoing_transfers),
    ):
        pending_transfers[direction] = [
            {
                "item_id": int(item["item_id"]),
                "other_name": str(
                    item.get("other_callsign")
                    or item.get("other_display_name")
                    or item.get("other_username")
                    or (
                        f"ID {item['sender_id']}"
                        if direction == "incoming"
                        else f"ID {item['recipient_id']}"
                    )
                ),
                "level": int(item["level"]),
                "image_url": _media_url(user_id, int(item["item_id"])),
            }
            for item in transfers
        ]
    return {
        "user_id": user_id,
        "username": profile.get("username") or username,
        "display_name": profile.get("display_name") or first_name,
        "callsign": profile.get("callsign"),
        "age": profile.get("age"),
        "gender": profile.get("gender"),
        "notes": profile.get("notes") or "",
        "balance_cents": balance,
        "activity": activity,
        "active_banner": (
            {
                "item_id": int(active_banner["item_id"]),
                "level": int(active_banner["level"]),
                "image_url": _media_url(user_id, int(active_banner["item_id"])),
                "animated": int(active_banner["level"]) == 6,
            }
            if active_banner is not None
            else None
        ),
        "inventory": [
            {
                "item_id": int(item["item_id"]),
                "level": int(item["level"]),
                "is_active": bool(item["is_active"]),
                "is_transfer_pending": bool(item["is_transfer_pending"]),
                "source": str(item["source"]),
                "recycle_value_cents": CASINO_RECYCLE_PAYOUT_CENTS[
                    int(item["level"])
                ],
                "image_url": _media_url(user_id, int(item["item_id"])),
                "animated": int(item["level"]) == 6,
            }
            for item in inventory
        ],
        "pending_transfers": pending_transfers,
    }


async def _prepare_banner_art(
    bot: Any,
    items: list[dict[str, Any]],
    purpose: str,
) -> None:
    from banner_store import ensure_banner_file

    for item in items:
        try:
            banner = await database.get_banner_item(int(item["item_id"]))
            if banner is None:
                raise RuntimeError(f"Banner {item['item_id']} was not found.")
            await ensure_banner_file(bot, banner, priority=1)
        except Exception:
            logging.exception(
                "Не удалось подготовить баннер item_id=%s purpose=%s.",
                item.get("item_id"),
                purpose,
            )


def _queue_banner_art(bot: Any, items: list[dict[str, Any]], purpose: str) -> None:
    task = asyncio.create_task(_prepare_banner_art(bot, items, purpose))
    _BANNER_ART_TASKS.add(task)
    task.add_done_callback(_BANNER_ART_TASKS.discard)


async def _notify_transfer_user(
    bot: Any | None,
    user_id: int,
    message: str,
) -> bool:
    if bot is None:
        return False
    try:
        await bot.send_message(chat_id=user_id, text=message)
    except TelegramError:
        logging.warning(
            "Не удалось уведомить user_id=%s об изменении передачи баннера.",
            user_id,
            exc_info=True,
        )
        return False
    return True


async def _casino_dashboard(user_id: int, username: str | None) -> dict[str, Any]:
    balance = await database.get_duel_balance(user_id, username)
    total_weight = sum(RARITY_WEIGHTS.values())
    return {
        "balance_cents": balance,
        "spin_cost_cents": CASINO_SPIN_COST_CENTS,
        "rarities": [
            {
                "level": level,
                "name": RARITY_NAMES[level],
                "weight": weight,
                "percentage": round(weight / total_weight * 100, 2),
            }
            for level, weight in RARITY_WEIGHTS.items()
        ],
    }


async def _spin_casino(
    user_id: int,
    username: str | None,
    spins: int,
    bot: Any | None,
) -> tuple[int, dict[str, Any]]:
    round_id = secrets.token_hex(12)
    cost = CASINO_SPIN_COST_CENTS * spins
    try:
        await database.get_duel_balance(user_id, username)
        await database.begin_casino_round(round_id, user_id, username, cost)
    except database.InsufficientCasinoBalanceError:
        return 409, {"error": "Недостаточно монет для выбранного числа круток."}
    except database.ActiveCasinoRoundError:
        return 409, {"error": "У тебя уже идёт крутка. Дождись её завершения."}
    except Exception:
        logging.exception("Не удалось начать Mini App крутку user_id=%s.", user_id)
        return 503, {"error": "Не удалось начать крутку. Попробуй позже."}

    try:
        generated = [
            generate_result(
                user_id,
                (
                    secrets.randbelow(6) + 1,
                    secrets.randbelow(6) + 1,
                    secrets.randbelow(6) + 1,
                ),
            )
            for _ in range(spins)
        ]
        items = await database.complete_casino_round(round_id, generated)
    except Exception:
        logging.exception("Не удалось завершить Mini App крутку user_id=%s.", user_id)
        try:
            refunded = await database.fail_casino_round(round_id)
        except Exception:
            logging.exception("Не удалось вернуть монеты за Mini App крутку.")
            refunded = False
        if refunded:
            return 500, {
                "error": "Крутка не завершилась; списанные монеты возвращены."
            }
        logging.error(
            "Не удалось подтвердить возврат или завершение Mini App крутки "
            "round_id=%s user_id=%s.",
            round_id,
            user_id,
        )
        return 500, {
            "error": "Крутка не завершилась. Проверь баланс и инвентарь перед повторной попыткой."
        }

    if bot is not None:
        _queue_banner_art(bot, items, "casino_reward")
    balance = await database.get_duel_balance(user_id, username)
    return 200, {
        "balance_cents": balance,
        "cost_cents": cost,
        "results": [
            {
                "item_id": int(item["item_id"]),
                "level": int(result["level"]),
                "rarity": RARITY_NAMES[int(result["level"])],
                "values": [int(value) for value in result["values"]],
                "image_url": _media_url(user_id, int(item["item_id"])),
                "animated": int(result["level"]) == 6,
            }
            for item, result in zip(items, generated, strict=True)
        ],
    }


async def _delete_recycled_banner_backups(
    bot: Any | None,
    item: dict[str, Any],
) -> None:
    if bot is None or BACKUP_CHANNEL_ID is None:
        return
    message_ids = {
        int(item[field])
        for field in ("backup_msg_id", "market_backup_msg_id")
        if item.get(field)
    }
    for message_id in message_ids:
        try:
            await bot.delete_message(
                chat_id=BACKUP_CHANNEL_ID,
                message_id=message_id,
            )
        except TelegramError:
            logging.exception(
                "Не удалось удалить копию переработанного баннера из бэкапа "
                "message_id=%s.",
                message_id,
            )


async def _notify_existing_transfer_request(
    bot: Any,
    sender_id: int,
    recipient_id: int,
    item_id: int,
) -> tuple[bool, int]:
    from casino import _notify_banner_transfer

    return await _notify_banner_transfer(
        SimpleNamespace(bot=bot),
        sender_id=sender_id,
        recipient_id=recipient_id,
        item_id=item_id,
    )


async def _notify_existing_transfer_resolution(
    bot: Any | None,
    sender_id: int,
    recipient_id: int,
    item_id: int,
    *,
    accepted: bool,
) -> bool:
    if bot is None:
        return False
    from casino import _notify_transfer_resolution

    try:
        await _notify_transfer_resolution(
            SimpleNamespace(bot=bot),
            sender_id=sender_id,
            recipient_id=recipient_id,
            item_id=item_id,
            accepted=accepted,
        )
    except Exception:
        logging.exception(
            "Не удалось отправить уведомление о решении передачи item_id=%s.",
            item_id,
        )
        return False
    return True


async def _request_banner_transfer(
    user_id: int,
    payload: dict[str, Any],
    bot: Any | None,
) -> tuple[int, dict[str, Any]]:
    item_id = payload.get("item_id")
    recipient_input = payload.get("recipient")
    if type(item_id) is not int or item_id <= 0:
        return 400, {"error": "Выбери баннер из своего инвентаря."}
    if not isinstance(recipient_input, str):
        return 400, {"error": "Укажи Telegram ID или username получателя."}

    recipient_value = recipient_input.strip()
    if _TRANSFER_USER_ID_RE.fullmatch(recipient_value):
        recipient_id = int(recipient_value)
        if recipient_id <= 0:
            return 400, {"error": "Telegram ID должен быть положительным числом."}
    else:
        username_match = _TRANSFER_USERNAME_RE.fullmatch(recipient_value)
        if username_match is None:
            return 400, {"error": "Укажи корректный Telegram ID или username."}
        recipient_id = await database.find_user_id_by_username(username_match.group(1))
        if recipient_id is None:
            return 404, {
                "error": "Пользователь не найден. Он должен хотя бы раз открыть бота."
            }

    result = await database.request_banner_transfer(user_id, recipient_id, item_id)
    if result != "pending":
        messages = {
            "same_user": "Нельзя передать баннер самому себе.",
            "missing": "Баннер не найден в твоём инвентаре.",
            "owner_item": "Баннер владельца нельзя передать.",
            "already_pending": "Для этого баннера уже ожидается решение.",
            "listed": "Сначала сними баннер с площадки.",
            "recipient_not_found": "Получатель не найден. Он должен открыть бота.",
        }
        return 409, {"error": messages.get(result, "Не удалось создать запрос передачи.")}

    notified_in_private = False
    notified_groups = 0
    notification_failed = False
    if bot is not None:
        try:
            notified_in_private, notified_groups = await _notify_existing_transfer_request(
                bot,
                user_id,
                recipient_id,
                item_id,
            )
        except Exception:
            logging.exception(
                "Запрос передачи создан, но уведомление не выполнено item_id=%s.",
                item_id,
            )
            notification_failed = True
    if notified_in_private:
        message = "Запрос отправлен. Получатель должен подтвердить передачу."
    elif notified_groups:
        message = "Запрос создан и показан получателю в общей группе."
    elif notification_failed:
        message = (
            "Запрос создан, но уведомление не отправлено. Получатель может "
            "увидеть его во входящих Mini App; при необходимости сообщи ему отдельно."
        )
    else:
        message = (
            "Запрос сохранён, но уведомление получателю не доставлено. "
            "При необходимости сообщи ему отдельно."
        )
    return 201, {"status": "pending", "message": message}


async def _market(
    query: dict[str, list[str]],
    user_id: int,
) -> dict[str, Any]:
    raw_level = query.get("level", ["all"])[0]
    level = int(raw_level) if raw_level.isdigit() and 1 <= int(raw_level) <= 6 else None
    sort_by = query.get("sort", ["date"])[0]
    if sort_by not in ("date", "price"):
        sort_by = "date"
    search = query.get("q", [""])[0].strip()[:64]
    try:
        page = max(0, min(int(query.get("page", ["0"])[0]), 10_000))
    except ValueError:
        page = 0

    if search:
        total = await database.count_market_search_results(
            search,
            preset_names=PRESET_NAMES,
            level=level,
        )
        lots = await database.search_market_lots(
            search,
            preset_names=PRESET_NAMES,
            level=level,
            sort_by=sort_by,
            limit=20,
            offset=page * 20,
        )
    else:
        total = await database.count_market_lots(level=level)
        lots = await database.get_market_lots(
            level=level,
            sort_by=sort_by,
            limit=20,
            offset=page * 20,
        )
    my_lots = await database.get_user_market_lots(user_id)
    inventory = await database.get_user_banner_items(user_id, limit=100)
    lots = [
        {
            **lot,
            "image_url": _media_url(user_id, int(lot["item_id"])),
            "animated": int(lot["level"]) == 6,
        }
        for lot in lots
    ]
    return {
        "lots": [
            {
                "lot_id": int(lot["lot_id"]),
                "item_id": int(lot["item_id"]),
                "seller_id": int(lot["seller_id"]),
                "title": str(lot.get("title") or ""),
                "level": int(lot["level"]),
                "price_cents": int(lot["price_cents"]),
                "created_at": str(lot["created_at"]),
                "image_url": str(lot["image_url"]),
                "animated": bool(lot["animated"]),
            }
            for lot in lots
        ],
        "my_lots": [
            {
                "lot_id": int(lot["lot_id"]),
                "item_id": int(lot["item_id"]),
                "title": str(lot.get("title") or ""),
                "level": int(lot["level"]),
                "price_cents": int(lot["price_cents"]),
                "created_at": str(lot["created_at"]),
                "image_url": _media_url(user_id, int(lot["item_id"])),
            }
            for lot in my_lots
        ],
        "inventory": [
            {
                "item_id": int(item["item_id"]),
                "level": int(item["level"]),
                "is_active": bool(item["is_active"]),
                "is_transfer_pending": bool(item["is_transfer_pending"]),
                "source": str(item["source"]),
                "image_url": _media_url(user_id, int(item["item_id"])),
                "animated": int(item["level"]) == 6,
            }
            for item in inventory
        ],
        "total": total,
        "page": page,
        "page_size": 20,
    }


async def _admin_dashboard() -> dict[str, Any]:
    keys = await database.list_access_keys()
    admins = await database.list_authorized_users(access_level="admin")
    market_fee = await database.get_market_fee_cents()
    return {
        "access_keys": [
            {
                "key": str(key["key"]),
                "access_level": str(key["access_level"]),
                "duration_seconds": key["duration_seconds"],
            }
            for key in keys
        ],
        "admin_count": len(admins) + int(
            not any(int(admin["user_id"]) == ADMIN_ID for admin in admins)
        ),
        "market_fee_cents": market_fee,
    }


async def get_banner_media(
    item_id: int,
    query: dict[str, list[str]],
    bot: Any | None = None,
) -> tuple[int, bytes, str]:
    try:
        user_id = int(query.get("user", [""])[0])
        expires = int(query.get("expires", [""])[0])
        signature = query.get("sig", [""])[0]
    except ValueError:
        return 400, b"Invalid media signature.", "text/plain; charset=utf-8"

    now = int(time.time())
    if expires < now or expires > now + _MEDIA_LINK_LIFETIME_SECONDS + 30:
        return 403, b"Media link expired.", "text/plain; charset=utf-8"
    expected = _media_signature(user_id, item_id, expires)
    if not hmac.compare_digest(signature, expected):
        return 403, b"Invalid media signature.", "text/plain; charset=utf-8"

    item = await database.get_banner_item(item_id)
    if item is None:
        return 404, b"Banner not found.", "text/plain; charset=utf-8"

    lot = await database.get_market_lot_for_item(item_id)
    is_public_listing = lot is not None
    is_pending_recipient = False
    if not is_public_listing and int(item["user_id"]) != user_id:
        pending_transfer = await database.get_pending_banner_transfer(item_id)
        is_pending_recipient = (
            pending_transfer is not None
            and int(pending_transfer["sender_id"]) == int(item["user_id"])
            and int(pending_transfer["recipient_id"]) == user_id
        )
    if (
        not is_public_listing
        and int(item["user_id"]) != user_id
        and not is_pending_recipient
    ):
        return 404, b"Banner not found.", "text/plain; charset=utf-8"

    if is_public_listing and not item.get("market_file_id") and bot is not None:
        try:
            from banner_store import ensure_market_banner_file

            item = await ensure_market_banner_file(bot, item)
        except Exception:
            if item.get("file_id"):
                logging.warning(
                    "Не удалось подготовить отдельное превью лота item_id=%s; "
                    "будет показан баннер владельца.",
                    item_id,
                    exc_info=True,
                )
            else:
                logging.exception(
                    "Не удалось подготовить превью лота item_id=%s.",
                    item_id,
                )
                return 503, b"Banner preview is not ready.", "text/plain; charset=utf-8"
    elif not item.get("file_id") and bot is not None:
        try:
            from banner_store import ensure_banner_file

            item = await ensure_banner_file(bot, item)
        except Exception:
            logging.exception(
                "Не удалось подготовить превью баннера item_id=%s.",
                item_id,
            )
            return 503, b"Banner preview is not ready.", "text/plain; charset=utf-8"

    file_id = (
        item.get("market_file_id") or item.get("file_id")
        if is_public_listing
        else item.get("file_id")
    )
    if not isinstance(file_id, str) or not file_id:
        return 404, b"Banner image not available.", "text/plain; charset=utf-8"

    api_base = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(20, connect=8)) as client:
            file_response = await client.get(
                f"{api_base}/getFile",
                params={"file_id": file_id},
            )
            file_response.raise_for_status()
            file_data = file_response.json()
            file_path = file_data.get("result", {}).get("file_path")
            if not file_data.get("ok") or not isinstance(file_path, str):
                return 502, b"Telegram could not provide this image.", "text/plain; charset=utf-8"

            media_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{quote(file_path, safe='/')}"
            async with client.stream("GET", media_url) as media_response:
                media_response.raise_for_status()
                content_type = media_response.headers.get("Content-Type", "").split(";", 1)[0]
                if not content_type.startswith("image/"):
                    return 502, b"Telegram returned a non-image file.", "text/plain; charset=utf-8"
                content_length = media_response.headers.get("Content-Length")
                if content_length and int(content_length) > _MAX_BANNER_MEDIA_SIZE:
                    return 413, b"Image is too large.", "text/plain; charset=utf-8"
                chunks: list[bytes] = []
                total_size = 0
                async for chunk in media_response.aiter_bytes():
                    total_size += len(chunk)
                    if total_size > _MAX_BANNER_MEDIA_SIZE:
                        return 413, b"Image is too large.", "text/plain; charset=utf-8"
                    chunks.append(chunk)
        return 200, b"".join(chunks), content_type
    except (httpx.HTTPError, ValueError):
        return 502, b"Could not retrieve banner image from Telegram.", "text/plain; charset=utf-8"


async def _create_access_key(
    user_id: int,
    payload: dict[str, Any],
) -> tuple[int, dict[str, Any]]:
    access_level = payload.get("access_level", "user")
    if access_level not in ("user", "admin", "autoresponder"):
        return 400, {"error": "Выбран неизвестный тип доступа."}
    duration = payload.get("duration_seconds", 2_592_000)
    if duration is not None and (
        type(duration) is not int
        or duration not in (86_400, 604_800, 2_592_000, 31_536_000)
    ):
        return 400, {"error": "Выбран неизвестный срок действия."}

    for _ in range(3):
        key = f"YUBARA-WEB-{secrets.token_hex(8).upper()}"
        if await database.create_access_key(
            key,
            access_level=access_level,
            duration_seconds=duration,
            created_by=user_id,
        ):
            return 201, {"key": key, "access_level": access_level}
    return 503, {"error": "Не удалось создать уникальный ключ. Попробуй ещё раз."}


async def _activate_code(
    user_id: int,
    username: str | None,
    payload: dict[str, Any],
) -> tuple[int, dict[str, Any]]:
    raw_code = payload.get("code")
    if not isinstance(raw_code, str) or not 1 <= len(raw_code.strip()) <= 128:
        return 400, {"error": "Введи код длиной от 1 до 128 символов."}
    code = raw_code.strip()

    access = await database.consume_access_key(code, user_id, username)
    if access is not None:
        return 200, {
            "status": "activated",
            "message": "Доступ активирован.",
            "access_level": access["access_level"],
        }

    normalized = code.upper()
    if normalized.startswith("YUBARA-COIN-"):
        result, amount, balance = await database.redeem_coin_promo_code(
            normalized,
            user_id,
            username,
        )
        if result == "redeemed":
            return 200, {
                "status": result,
                "message": "Промокод активирован.",
                "amount_cents": amount,
                "delta_cents": amount,
                "balance_cents": balance,
            }
        if result == "used":
            return 409, {"error": "Этот промокод уже использован."}
    elif normalized.startswith("YUBARA-PEN-"):
        result, amount, balance = await database.redeem_coin_penalty_code(
            normalized,
            user_id,
            username,
        )
        if result == "redeemed":
            return 200, {
                "status": result,
                "message": "Штрафной код применён.",
                "amount_cents": amount,
                "delta_cents": -int(amount or 0),
                "balance_cents": balance,
            }
        if result == "used":
            return 409, {"error": "Этот штрафной код уже использован."}
    elif normalized.startswith("YUBARA-BANNER-"):
        item = await database.redeem_banner_pattern_key(normalized, user_id)
        if item is not None:
            return 200, {
                "status": "redeemed",
                "message": "Баннер добавлен в инвентарь.",
                "item_id": item["item_id"],
                "level": item["level"],
            }
    return 404, {"error": "Код не найден или уже использован."}


async def dispatch(
    method: str,
    path: str,
    query: dict[str, list[str]],
    user_id: int,
    first_name: str,
    username: str | None,
    payload: dict[str, Any],
    bot: Any | None = None,
) -> tuple[int, dict[str, Any]]:
    if path == "/api/v1/profile" and method == "GET":
        return 200, await _profile(user_id, first_name, username)
    if path == "/api/v1/profile/update" and method == "POST":
        return await _update_profile(user_id, payload)
    if path == "/api/v1/community/groups" and method == "GET":
        return 200, {"groups": await _community_groups(user_id, bot)}
    if path == "/api/v1/community/leaderboard" and method == "GET":
        return await _community_leaderboard(query, user_id)
    if path == "/api/v1/feedback" and method == "POST":
        return await _submit_error_report(
            user_id,
            first_name,
            username,
            payload,
            bot,
        )
    if path == "/api/v1/image" and method == "POST":
        return await _offer_image_request(user_id, payload, bot)
    if path == "/api/v1/privacy/forget" and method == "POST":
        if payload.get("confirmed") is not True:
            return 400, {"error": "Подтверди удаление сохранённых данных."}
        deleted = await database.forget_user(user_id, 0)
        return 200, {
            "status": "deleted" if deleted else "empty",
            "message": (
                "Сохранённые профильные данные и статистика сообщений удалены."
                if deleted
                else "Сохранённых профильных данных и статистики нет."
            ),
        }
    if path == "/api/v1/casino" and method == "GET":
        return 200, await _casino_dashboard(user_id, username)
    if path == "/api/v1/casino/spin" and method == "POST":
        spins = payload.get("spins", 1)
        if type(spins) is not int or spins not in (1, 10):
            return 400, {"error": "Можно выполнить одну крутку или пакет ×10."}
        return await _spin_casino(user_id, username, spins, bot)
    if path == "/api/v1/market" and method == "GET":
        return 200, await _market(query, user_id)
    if path == "/api/v1/market/buy" and method == "POST":
        lot_id = payload.get("lot_id")
        if type(lot_id) is not int or lot_id <= 0:
            return 400, {"error": "Укажи корректный номер лота."}
        result, item = await database.buy_market_lot(user_id, username, lot_id)
        if result == "bought" and item is not None:
            return 200, {
                "status": result,
                "item_id": item["item_id"],
                "message": "Покупка завершена. Открой /inventory в чате бота, чтобы увидеть баннер.",
            }
        messages = {
            "sold": "Лот уже продан или снят.",
            "own": "Нельзя купить собственный лот.",
            "funds": "Недостаточно монет.",
        }
        return 409, {"error": messages.get(result, "Покупку не удалось выполнить.")}
    if path == "/api/v1/market/list" and method == "POST":
        item_id = payload.get("item_id")
        price_cents = payload.get("price_cents")
        title = payload.get("title")
        if type(item_id) is not int or item_id <= 0:
            return 400, {"error": "Выбери баннер из своего инвентаря."}
        if type(price_cents) is not int or not 0 < price_cents <= 1_000_000_000_000:
            return 400, {"error": "Цена должна быть положительной и содержать не более двух знаков после запятой."}
        if title is not None and (not isinstance(title, str) or len(title.strip()) > 48):
            return 400, {"error": "Название лота не должно превышать 48 символов."}
        result = await database.create_market_lot(
            user_id,
            item_id,
            price_cents,
            title=title,
        )
        messages = {
            "listed": "Баннер выставлен на площадку.",
            "debt": "При отрицательном балансе нельзя выставлять баннеры.",
            "missing": "Баннер не найден в твоём инвентаре.",
            "forbidden": "Активный баннер и баннер владельца выставить нельзя.",
            "pending": "Для этого баннера ожидается передача.",
        }
        if result == "listed":
            return 201, {"status": result, "message": messages[result]}
        return 409, {"error": messages.get(result, "Не удалось выставить баннер.")}
    if path == "/api/v1/market/withdraw" and method == "POST":
        lot_id = payload.get("lot_id")
        if type(lot_id) is not int or lot_id <= 0:
            return 400, {"error": "Укажи корректный номер лота."}
        if await database.withdraw_market_lot(user_id, lot_id):
            return 200, {"status": "withdrawn", "message": "Лот снят с продажи."}
        return 404, {"error": "Лот не найден или недоступен."}
    if path == "/api/v1/inventory/activate" and method == "POST":
        item_id = payload.get("item_id")
        if type(item_id) is not int or item_id <= 0:
            return 400, {"error": "Выбери баннер из своего инвентаря."}
        if not await database.activate_banner_item(user_id, item_id):
            return 404, {"error": "Баннер не найден или сейчас недоступен."}
        return 200, {"status": "activated", "message": "Баннер установлен."}
    if path == "/api/v1/inventory/transfer" and method == "POST":
        return await _request_banner_transfer(user_id, payload, bot)
    if path == "/api/v1/inventory/transfer/resolve" and method == "POST":
        item_id = payload.get("item_id")
        action = payload.get("action")
        if type(item_id) is not int or item_id <= 0:
            return 400, {"error": "Укажи корректный номер баннера."}
        if action not in ("accept", "return"):
            return 400, {"error": "Выбери действие: принять или вернуть запрос."}
        if action == "accept":
            result, sender_id = await database.accept_banner_transfer(item_id, user_id)
            if result != "transferred":
                messages = {
                    "missing": "Запрос больше недоступен.",
                    "not_recipient": "Принять передачу может только получатель.",
                    "listed": "Баннер выставлен на площадку и не может быть передан.",
                }
                return 409, {"error": messages.get(result, "Не удалось принять передачу.")}
            if bot is not None:
                _queue_banner_art(bot, [{"item_id": item_id}], "banner_transfer")
            notified = False
            if sender_id is None:
                logging.error(
                    "Передача item_id=%s завершена без ID отправителя для уведомления.",
                    item_id,
                )
            else:
                notified = await _notify_existing_transfer_resolution(
                    bot,
                    sender_id,
                    user_id,
                    item_id,
                    accepted=True,
                )
            message = "Баннер передан и добавлен в инвентарь."
            if bot is not None and not notified:
                message += " Уведомление отправителю не подтверждено."
            return 200, {"status": result, "message": message}

        result, sender_id = await database.return_banner_transfer(item_id, user_id)
        if result != "returned":
            messages = {
                "missing": "Запрос больше недоступен.",
                "not_recipient": "Вернуть запрос может только получатель.",
            }
            return 409, {"error": messages.get(result, "Не удалось вернуть запрос.")}
        notified = False
        if sender_id is None:
            logging.error(
                "Запрос item_id=%s возвращён без ID отправителя для уведомления.",
                item_id,
            )
        else:
            notified = await _notify_existing_transfer_resolution(
                bot,
                sender_id,
                user_id,
                item_id,
                accepted=False,
            )
        message = "Запрос возвращён отправителю."
        if bot is not None and not notified:
            message += " Уведомление отправителю не подтверждено."
        return 200, {"status": result, "message": message}
    if path == "/api/v1/inventory/transfer/cancel" and method == "POST":
        item_id = payload.get("item_id")
        if type(item_id) is not int or item_id <= 0:
            return 400, {"error": "Укажи корректный номер баннера."}
        result, recipient_id = await database.cancel_banner_transfer(item_id, user_id)
        if result != "cancelled":
            messages = {
                "missing": "Запрос больше недоступен.",
                "not_sender": "Отменить передачу может только отправитель.",
            }
            return 409, {"error": messages.get(result, "Не удалось отменить передачу.")}
        message = "Запрос передачи отменён."
        if recipient_id is None:
            logging.error(
                "Запрос item_id=%s отменён без ID получателя для уведомления.",
                item_id,
            )
            message += " Уведомление получателю не отправлено."
        else:
            notified = await _notify_transfer_user(
                bot,
                recipient_id,
                f"Запрос на передачу баннера #{item_id} отменён отправителем.",
            )
            if bot is not None and not notified:
                message += " Уведомление получателю не подтверждено."
        return 200, {"status": result, "message": message}
    if path == "/api/v1/inventory/recycle" and method == "POST":
        item_id = payload.get("item_id")
        if type(item_id) is not int or item_id <= 0:
            return 400, {"error": "Выбери баннер из своего инвентаря."}
        item = await database.get_banner_item(item_id)
        if item is None or int(item["user_id"]) != user_id:
            return 404, {"error": "Баннер не найден в твоём инвентаре."}
        payout = CASINO_RECYCLE_PAYOUT_CENTS[int(item["level"])]
        await database.get_duel_balance(user_id, username)
        result = await database.recycle_banner_item(user_id, item_id, payout)
        if result == "recycled":
            await _delete_recycled_banner_backups(bot, item)
            balance = await database.get_duel_balance(user_id, username)
            return 200, {
                "status": result,
                "payout_cents": payout,
                "balance_cents": balance,
                "message": f"Баннер переработан. Получено {payout / 100:g} монет.",
            }
        messages = {
            "active": "Активный баннер нельзя переработать, пока баланс не в минусе.",
            "forbidden": "Этот баннер нельзя переработать.",
            "pending": "Баннер ожидает передачи и пока недоступен.",
        }
        return 409, {"error": messages.get(result, "Баннер уже недоступен.")}
    if path == "/api/v1/activation" and method == "POST":
        return await _activate_code(user_id, username, payload)
    if path == "/api/v1/admin":
        if not await _has_admin_access(user_id):
            return 403, {"error": "Этот раздел доступен только администраторам."}
        if method == "GET":
            return 200, await _admin_dashboard()
    if path.startswith("/api/v1/admin/access-keys"):
        if not await _has_admin_access(user_id):
            return 403, {"error": "Этот раздел доступен только администраторам."}
        if path == "/api/v1/admin/access-keys" and method == "POST":
            return await _create_access_key(user_id, payload)
        if path == "/api/v1/admin/access-keys/revoke" and method == "POST":
            key = payload.get("key")
            if not isinstance(key, str) or not key:
                return 400, {"error": "Не указан ключ."}
            if await database.revoke_access_key(key):
                return 200, {"status": "revoked", "message": "Ключ отозван."}
            return 404, {"error": "Ключ не найден или уже использован."}
    return 404, {"error": "Маршрут не найден."}
