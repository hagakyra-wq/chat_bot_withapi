"""Управление отдельными Telegram user-сессиями пользователей автоответчика."""

import logging
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from telethon import TelegramClient
from telethon.errors import (
    PasswordHashInvalidError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    SessionPasswordNeededError,
)
from telethon.tl.custom import QRLogin

from config import (
    ADMIN_ID,
    TELEGRAM_API_HASH,
    TELEGRAM_API_ID,
    TELEGRAM_USER_SESSION_PATH,
)

_code_delivery_hints = {
    "SentCodeTypeApp": (
        "в сообщении от Telegram в приложении на уже авторизованном устройстве; "
        "SMS может не прийти"
    ),
    "SentCodeTypeSms": "в SMS",
    "SentCodeTypeCall": "в звонке от Telegram",
    "SentCodeTypeFlashCall": "в автоматическом звонке от Telegram",
    "SentCodeTypeEmailCode": "на привязанной к Telegram почте",
}


@dataclass
class _AccountState:
    client: TelegramClient | None = None
    authorized: bool = False
    qr_login: QRLogin | None = None
    code_delivery_hint: str = (
        "Проверь приложение Telegram на уже авторизованном устройстве; "
        "код может прийти не по SMS."
    )


_accounts: dict[int, _AccountState] = {}
_auth_owners: dict[int, int] = {}


class AuthFlowBusyError(RuntimeError):
    """Другая сессия этого аккаунта уже проходит авторизацию."""


class TwoFactorRequiredError(RuntimeError):
    """Telegram запросил пароль двухфакторной аутентификации."""


class InvalidLoginCodeError(RuntimeError):
    """Telegram отклонил код авторизации."""


class ExpiredLoginCodeError(RuntimeError):
    """Срок действия кода авторизации истёк."""


class InvalidTwoFactorPasswordError(RuntimeError):
    """Telegram отклонил пароль двухфакторной аутентификации."""


def is_configured() -> bool:
    return bool(TELEGRAM_API_ID and TELEGRAM_API_HASH)


def _state(user_id: int = ADMIN_ID) -> _AccountState:
    return _accounts.setdefault(user_id, _AccountState())


def _session_file_path(user_id: int = ADMIN_ID) -> Path:
    base_path = TELEGRAM_USER_SESSION_PATH
    if user_id != ADMIN_ID:
        base_path = Path(f"{base_path}.accounts") / str(user_id)
    if not str(base_path).endswith(".session"):
        base_path = Path(f"{base_path}.session")
    return base_path


def session_file_path(user_id: int = ADMIN_ID) -> Path:
    return _session_file_path(user_id)


def session_user_ids() -> list[int]:
    user_ids = {ADMIN_ID}
    accounts_dir = Path(f"{TELEGRAM_USER_SESSION_PATH}.accounts")
    if accounts_dir.is_dir():
        for account_dir in accounts_dir.iterdir():
            if account_dir.is_dir() and account_dir.name.isdecimal():
                user_ids.add(int(account_dir.name))
    return sorted(user_ids)


def snapshot_session_for_backup(user_id: int, destination: Path) -> None:
    session_path = _session_file_path(user_id)
    if not session_path.is_file():
        raise FileNotFoundError(f"Telegram user-сессия не найдена: {session_path}")
    state = _state(user_id)
    if state.client is not None:
        state.client.session.save()
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    source = sqlite3.connect(str(session_path), timeout=10)
    backup = sqlite3.connect(str(destination), timeout=10)
    try:
        source.backup(backup)
    finally:
        backup.close()
        source.close()
    if os.name != "nt":
        os.chmod(destination, 0o600)


def session_exists(user_id: int = ADMIN_ID) -> bool:
    return _session_file_path(user_id).is_file()


def _enabled_marker_path(user_id: int = ADMIN_ID) -> Path:
    return Path(f"{_session_file_path(user_id)}.enabled")


def is_enabled(user_id: int = ADMIN_ID) -> bool:
    try:
        return _enabled_marker_path(user_id).read_text(encoding="ascii").strip() == "1"
    except FileNotFoundError:
        return False


def set_enabled(enabled: bool, user_id: int = ADMIN_ID) -> None:
    marker_path = _enabled_marker_path(user_id)
    marker_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary_path = Path(f"{marker_path}.tmp")
    temporary_path.write_text("1" if enabled else "0", encoding="ascii")
    os.replace(temporary_path, marker_path)
    if os.name != "nt":
        os.chmod(marker_path, 0o600)


def get_status(user_id: int = ADMIN_ID) -> tuple[bool, bool]:
    state = _state(user_id)
    connected = state.client is not None and state.client.is_connected()
    return connected, state.authorized


def get_authorized_client(user_id: int = ADMIN_ID) -> TelegramClient | None:
    state = _state(user_id)
    if state.authorized and state.client is not None and state.client.is_connected():
        return state.client
    return None


def _get_client(user_id: int = ADMIN_ID) -> TelegramClient:
    state = _state(user_id)
    if not is_configured():
        raise RuntimeError(
            "Для user-аккаунта задайте TELEGRAM_API_ID и TELEGRAM_API_HASH."
        )
    try:
        api_id = int(TELEGRAM_API_ID)
    except ValueError as exc:
        raise RuntimeError("TELEGRAM_API_ID должен быть целым числом.") from exc
    if state.client is None:
        session_path = _session_file_path(user_id)
        session_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        state.client = TelegramClient(
            str(session_path),
            api_id,
            TELEGRAM_API_HASH,
        )
    return state.client


async def _connected_client(user_id: int = ADMIN_ID) -> TelegramClient:
    client = _get_client(user_id)
    if not client.is_connected():
        await client.connect()
    return client


async def _disconnect_client(user_id: int = ADMIN_ID) -> None:
    state = _state(user_id)
    client = state.client
    if client is not None and client.is_connected():
        await client.disconnect()
    state.client = None
    state.authorized = False


def _secure_session_file(user_id: int = ADMIN_ID) -> None:
    if os.name != "nt":
        session_path = _session_file_path(user_id)
        if session_path.is_file():
            os.chmod(session_path, 0o600)


async def connect_saved_session(user_id: int = ADMIN_ID) -> bool:
    """Подключает сохранённую сессию указанного владельца."""
    state = _state(user_id)
    if not is_configured():
        logging.warning(
            "User-аккаунт %s не подключён: не заданы TELEGRAM_API_ID и TELEGRAM_API_HASH.",
            user_id,
        )
        return False
    client = await _connected_client(user_id)
    if not await client.is_user_authorized():
        await _disconnect_client(user_id)
        return False
    state.authorized = True
    _secure_session_file(user_id)
    return True


async def startup(user_ids: Iterable[int] = ()) -> list[int]:
    """Восстанавливает только включённые сессии и возвращает подключённые ID."""
    connected_ids: list[int] = []
    for user_id in dict.fromkeys((ADMIN_ID, *user_ids)):
        if not is_enabled(user_id):
            continue
        try:
            if await connect_saved_session(user_id):
                connected_ids.append(user_id)
            else:
                set_enabled(False, user_id)
        except Exception:
            logging.exception("Не удалось восстановить Telegram user-сессию user_id=%s.", user_id)
            await _disconnect_client(user_id)
    return connected_ids


async def begin_login(user_id: int, phone: str) -> str | None:
    """Запрашивает код и возвращает временный phone_code_hash."""
    state = _state(user_id)
    if user_id in _auth_owners:
        raise AuthFlowBusyError
    _auth_owners[user_id] = user_id
    try:
        client = await _connected_client(user_id)
        if await client.is_user_authorized():
            state.authorized = True
            _auth_owners.pop(user_id, None)
            _secure_session_file(user_id)
            return None
        sent_code = await client.send_code_request(phone)
        delivery_type = type(sent_code.type).__name__
        state.code_delivery_hint = _code_delivery_hints.get(
            delivery_type,
            "способ доставки определяет Telegram; проверь приложение Telegram и SMS",
        )
        logging.info(
            "Telegram выбрал способ доставки кода для user_id=%s: %s.",
            user_id,
            delivery_type,
        )
        return sent_code.phone_code_hash
    except Exception:
        _auth_owners.pop(user_id, None)
        if not state.authorized:
            await _disconnect_client(user_id)
        raise


def get_code_delivery_hint(user_id: int = ADMIN_ID) -> str:
    return _state(user_id).code_delivery_hint


async def begin_qr_login(user_id: int) -> QRLogin | None:
    """Создаёт QR-токен для входа с ранее авторизованного Telegram-устройства."""
    state = _state(user_id)
    if user_id in _auth_owners:
        raise AuthFlowBusyError
    _auth_owners[user_id] = user_id
    try:
        client = await _connected_client(user_id)
        if await client.is_user_authorized():
            state.authorized = True
            _auth_owners.pop(user_id, None)
            _secure_session_file(user_id)
            return None
        state.qr_login = await client.qr_login()
        return state.qr_login
    except Exception:
        _auth_owners.pop(user_id, None)
        raise


def get_qr_login(user_id: int) -> QRLogin:
    _check_auth_owner(user_id)
    qr_login = _state(user_id).qr_login
    if qr_login is None:
        raise RuntimeError("QR-авторизация не запущена.")
    return qr_login


async def wait_for_qr_login(user_id: int) -> None:
    state = _state(user_id)
    qr_login = get_qr_login(user_id)
    try:
        await qr_login.wait()
    except SessionPasswordNeededError as exc:
        raise TwoFactorRequiredError from exc
    state.authorized = True
    _auth_owners.pop(user_id, None)
    state.qr_login = None
    _secure_session_file(user_id)


async def recreate_qr_login(user_id: int) -> QRLogin:
    qr_login = get_qr_login(user_id)
    await qr_login.recreate()
    return qr_login


def _check_auth_owner(user_id: int) -> None:
    if _auth_owners.get(user_id) != user_id:
        raise RuntimeError("Шаг авторизации уже отменён или истёк.")


async def submit_login_code(
    user_id: int,
    phone: str,
    code: str,
    phone_code_hash: str,
) -> None:
    state = _state(user_id)
    _check_auth_owner(user_id)
    client = await _connected_client(user_id)
    try:
        await client.sign_in(
            phone=phone,
            code=code,
            phone_code_hash=phone_code_hash,
        )
    except SessionPasswordNeededError as exc:
        raise TwoFactorRequiredError from exc
    except PhoneCodeInvalidError as exc:
        raise InvalidLoginCodeError from exc
    except PhoneCodeExpiredError as exc:
        raise ExpiredLoginCodeError from exc
    state.authorized = True
    _auth_owners.pop(user_id, None)
    _secure_session_file(user_id)


async def submit_two_factor_password(user_id: int, password: str) -> None:
    state = _state(user_id)
    _check_auth_owner(user_id)
    client = await _connected_client(user_id)
    try:
        await client.sign_in(password=password)
    except PasswordHashInvalidError as exc:
        raise InvalidTwoFactorPasswordError from exc
    state.authorized = True
    _auth_owners.pop(user_id, None)
    state.qr_login = None
    _secure_session_file(user_id)


async def cancel_login(user_id: int) -> None:
    state = _state(user_id)
    if _auth_owners.get(user_id) == user_id:
        _auth_owners.pop(user_id, None)
        state.qr_login = None
        if not state.authorized:
            await _disconnect_client(user_id)


async def disconnect(user_id: int = ADMIN_ID) -> None:
    """Отключает user-клиент, сохраняя файл сессии."""
    state = _state(user_id)
    _auth_owners.pop(user_id, None)
    state.qr_login = None
    await _disconnect_client(user_id)


async def disconnect_all() -> None:
    """Отключает все user-сессии, известные этому процессу."""
    for user_id in tuple(_accounts):
        await disconnect(user_id)


async def reset_session(user_id: int = ADMIN_ID) -> list[Path]:
    """Отключает аккаунт и удаляет только файлы его сессии."""
    state = _state(user_id)
    _auth_owners.pop(user_id, None)
    state.qr_login = None
    await _disconnect_client(user_id)
    session_path = _session_file_path(user_id)
    marker_path = _enabled_marker_path(user_id)
    removed: list[Path] = []
    for path in (
        session_path,
        Path(f"{session_path}-journal"),
        Path(f"{session_path}-wal"),
        Path(f"{session_path}-shm"),
        marker_path,
        Path(f"{marker_path}.tmp"),
    ):
        if path.exists():
            path.unlink()
            removed.append(path)
    _accounts.pop(user_id, None)
    return removed
