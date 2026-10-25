"""Telegram-authenticated API operations for the Mini App."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from typing import Any
from urllib.parse import quote

import httpx
import database
from config import ADMIN_ID, TELEGRAM_TOKEN
from yubari_banner import PRESET_NAMES

_MEDIA_LINK_LIFETIME_SECONDS = 600
_MAX_BANNER_MEDIA_SIZE = 20 * 1024 * 1024


def _is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID


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
    }


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
    if not is_public_listing and int(item["user_id"]) != user_id:
        return 404, b"Banner not found.", "text/plain; charset=utf-8"

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
) -> tuple[int, dict[str, Any]]:
    if path == "/api/v1/profile" and method == "GET":
        return 200, await _profile(user_id, first_name, username)
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
