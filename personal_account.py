"""Управление Telegram user-сессией администратора."""

import logging
import os
from pathlib import Path

from telethon import TelegramClient
from telethon.tl.custom import QRLogin
from telethon.errors import (
    PasswordHashInvalidError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    SessionPasswordNeededError,
)

from config import TELEGRAM_API_HASH, TELEGRAM_API_ID, TELEGRAM_USER_SESSION_PATH

_client: TelegramClient | None = None
_authorized = False
_auth_owner_id: int | None = None
_qr_login: QRLogin | None = None
_code_delivery_hint = (
    "Проверь приложение Telegram на уже авторизованном устройстве; код может прийти не по SMS."
)


class AuthFlowBusyError(RuntimeError):
    """Другая админ-сессия уже проходит авторизацию."""


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


def _session_file_path() -> Path:
    path = TELEGRAM_USER_SESSION_PATH
    if not str(path).endswith(".session"):
        path = Path(f"{path}.session")
    return path


def session_exists() -> bool:
    return _session_file_path().is_file()


def _enabled_marker_path() -> Path:
    return Path(f"{_session_file_path()}.enabled")


def is_enabled() -> bool:
    try:
        return _enabled_marker_path().read_text(encoding="ascii").strip() == "1"
    except FileNotFoundError:
        return False


def set_enabled(enabled: bool) -> None:
    marker_path = _enabled_marker_path()
    marker_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary_path = Path(f"{marker_path}.tmp")
    temporary_path.write_text("1" if enabled else "0", encoding="ascii")
    os.replace(temporary_path, marker_path)
    if os.name != "nt":
        os.chmod(marker_path, 0o600)


def get_status() -> tuple[bool, bool]:
    connected = _client is not None and _client.is_connected()
    return connected, _authorized


def get_authorized_client() -> TelegramClient | None:
    if _authorized and _client is not None and _client.is_connected():
        return _client
    return None


def _get_client() -> TelegramClient:
    global _client
    if not is_configured():
        raise RuntimeError(
            "Для user-аккаунта задайте TELEGRAM_API_ID и TELEGRAM_API_HASH."
        )
    try:
        api_id = int(TELEGRAM_API_ID)
    except ValueError as exc:
        raise RuntimeError("TELEGRAM_API_ID должен быть целым числом.") from exc
    if _client is None:
        session_path = _session_file_path()
        session_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        _client = TelegramClient(
            str(session_path),
            api_id,
            TELEGRAM_API_HASH,
        )
    return _client


async def _connected_client() -> TelegramClient:
    client = _get_client()
    if not client.is_connected():
        await client.connect()
    return client


async def _disconnect_client() -> None:
    global _client, _authorized
    client = _client
    if client is not None and client.is_connected():
        await client.disconnect()
    _client = None
    _authorized = False


def _secure_session_file() -> None:
    if os.name != "nt":
        session_path = _session_file_path()
        if session_path.is_file():
            os.chmod(session_path, 0o600)


async def connect_saved_session() -> bool:
    """Подключает сохранённую сессию, если она уже авторизована."""
    global _authorized
    if not is_configured():
        logging.warning(
            "User-аккаунт не подключён: не заданы TELEGRAM_API_ID и TELEGRAM_API_HASH."
        )
        return False
    client = await _connected_client()
    if not await client.is_user_authorized():
        await _disconnect_client()
        return False
    _authorized = True
    _secure_session_file()
    return True


async def startup() -> bool:
    if not is_enabled():
        return False
    return await connect_saved_session()


async def begin_login(user_id: int, phone: str) -> str | None:
    """Запрашивает код и возвращает только временный phone_code_hash."""
    global _auth_owner_id, _authorized, _code_delivery_hint
    if _auth_owner_id not in (None, user_id):
        raise AuthFlowBusyError
    _auth_owner_id = user_id
    try:
        client = await _connected_client()
        if await client.is_user_authorized():
            _authorized = True
            _auth_owner_id = None
            _secure_session_file()
            return None
        sent_code = await client.send_code_request(phone)
        delivery_type = type(sent_code.type).__name__
        delivery_hints = {
            "SentCodeTypeApp": (
                "в сообщении от Telegram в приложении на уже авторизованном устройстве; "
                "SMS может не прийти"
            ),
            "SentCodeTypeSms": "в SMS",
            "SentCodeTypeCall": "в звонке от Telegram",
            "SentCodeTypeFlashCall": "в автоматическом звонке от Telegram",
            "SentCodeTypeEmailCode": "на привязанной к Telegram почте",
        }
        _code_delivery_hint = delivery_hints.get(
            delivery_type,
            "способ доставки определяет Telegram; проверь приложение Telegram и SMS",
        )
        logging.info("Telegram выбрал способ доставки кода: %s.", delivery_type)
        return sent_code.phone_code_hash
    except Exception:
        _auth_owner_id = None
        if not _authorized:
            await _disconnect_client()
        raise


def get_code_delivery_hint() -> str:
    return _code_delivery_hint


async def begin_qr_login(user_id: int) -> QRLogin | None:
    """Создаёт QR-токен для входа с ранее авторизованного Telegram-устройства."""
    global _auth_owner_id, _authorized, _qr_login
    if _auth_owner_id not in (None, user_id):
        raise AuthFlowBusyError
    _auth_owner_id = user_id
    try:
        client = await _connected_client()
        if await client.is_user_authorized():
            _authorized = True
            _auth_owner_id = None
            _secure_session_file()
            return None
        _qr_login = await client.qr_login()
        return _qr_login
    except Exception:
        if _auth_owner_id == user_id:
            _auth_owner_id = None
        raise


def get_qr_login(user_id: int) -> QRLogin:
    _check_auth_owner(user_id)
    if _qr_login is None:
        raise RuntimeError("QR-авторизация не запущена.")
    return _qr_login


async def wait_for_qr_login(user_id: int) -> None:
    global _auth_owner_id, _authorized, _qr_login
    qr_login = get_qr_login(user_id)
    try:
        await qr_login.wait()
    except SessionPasswordNeededError as exc:
        raise TwoFactorRequiredError from exc
    _authorized = True
    _auth_owner_id = None
    _qr_login = None
    _secure_session_file()


async def recreate_qr_login(user_id: int) -> QRLogin:
    global _qr_login
    _check_auth_owner(user_id)
    if _qr_login is None:
        raise RuntimeError("QR-авторизация не запущена.")
    await _qr_login.recreate()
    return _qr_login


def _check_auth_owner(user_id: int) -> None:
    if _auth_owner_id != user_id:
        raise RuntimeError("Шаг авторизации уже отменён или истёк.")


async def submit_login_code(
    user_id: int,
    phone: str,
    code: str,
    phone_code_hash: str,
) -> None:
    global _auth_owner_id, _authorized
    _check_auth_owner(user_id)
    client = await _connected_client()
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
    _authorized = True
    _auth_owner_id = None
    _secure_session_file()


async def submit_two_factor_password(user_id: int, password: str) -> None:
    global _auth_owner_id, _authorized
    _check_auth_owner(user_id)
    client = await _connected_client()
    try:
        await client.sign_in(password=password)
    except PasswordHashInvalidError as exc:
        raise InvalidTwoFactorPasswordError from exc
    _authorized = True
    _auth_owner_id = None
    _secure_session_file()


async def cancel_login(user_id: int) -> None:
    global _auth_owner_id, _qr_login
    if _auth_owner_id == user_id:
        _auth_owner_id = None
        _qr_login = None
        if not _authorized:
            await _disconnect_client()


async def disconnect() -> None:
    """Отключает клиент, сохраняя файл авторизованной сессии."""
    global _auth_owner_id, _qr_login
    _auth_owner_id = None
    _qr_login = None
    await _disconnect_client()


async def reset_session() -> list[Path]:
    """Отключает user-аккаунт и удаляет только его SQLite session-файлы."""
    global _auth_owner_id
    _auth_owner_id = None
    await _disconnect_client()
    session_path = _session_file_path()
    removed: list[Path] = []
    for path in (
        session_path,
        Path(f"{session_path}-journal"),
        Path(f"{session_path}-wal"),
        Path(f"{session_path}-shm"),
        _enabled_marker_path(),
        Path(f"{_enabled_marker_path()}.tmp"),
    ):
        if path.exists():
            path.unlink()
            removed.append(path)
    return removed
