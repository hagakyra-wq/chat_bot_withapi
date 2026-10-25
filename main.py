"""Точка входа: восстановление данных, Render healthcheck и polling."""

import asyncio
import hashlib
import io
import json
import logging
import os
import sqlite3
import threading
from collections.abc import Awaitable
from datetime import datetime, time as datetime_time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet
from telegram import MessageReactionUpdated, Update
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    BaseUpdateProcessor,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)

import database
import autoresponder
import personal_account
from channel_log import (
    TelegramChannelLogHandler,
    install_channel_log_handler,
    remove_channel_log_handler,
    send_channel_event,
)
from config import (
    BACKUP_CHANNEL_ID,
    BACKUP_INTERVAL_SECONDS,
    BACKUP_MAX_DOWNLOAD_SIZE,
    CASINO_BONUS_AUDIT_INTERVAL_SECONDS,
    DATABASE_ARCHIVE_DIR,
    DATABASE_BACKUP_PATH,
    DATABASE_PATH,
    HISTORY_ENCRYPTION_KEY,
    MOSCOW_TIMEZONE,
    PORT,
    TELEGRAM_TOKEN,
)
from duel import handle_player_duel_callback
from casino import (
    audit_subscription_bonuses,
    on_chat_member_update,
    on_game_callback,
    test_banner_command,
)
from handlers_group import (
    on_group_activity,
    on_group_message,
    on_message_reaction,
    on_new_members,
    on_group_profile_stats_callback,
    on_welcome_callback,
)
from handlers_pm import (
    break_command,
    build_admin_conversation_handler,
    build_delete_all_data_handler,
    build_history_conversation_handler,
    build_personal_autoresponder_conversation_handler,
    on_profile_callback,
    on_private_message,
    private_access_callback_guard,
)
from images import create_profile_card, on_image_callback, stop_image_jobs
from banner_store import stop_banner_render_queue


class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, _format: str, *_args: object) -> None:
        del _format, _args


class PerUserUpdateProcessor(BaseUpdateProcessor):
    def __init__(self, max_concurrent_updates: int) -> None:
        super().__init__(max_concurrent_updates)
        self._user_locks: dict[int, tuple[asyncio.Lock, int]] = {}
        self._locks_guard = asyncio.Lock()

    async def do_process_update(
        self,
        update: object,
        coroutine: Awaitable[Any],
    ) -> None:
        user = update.effective_user if isinstance(update, Update) else None
        if user is None:
            await coroutine
            return

        async with self._locks_guard:
            state = self._user_locks.get(user.id)
            lock, references = state if state is not None else (asyncio.Lock(), 0)
            self._user_locks[user.id] = (lock, references + 1)
        try:
            async with lock:
                await coroutine
        finally:
            async with self._locks_guard:
                lock, references = self._user_locks[user.id]
                if references == 1:
                    del self._user_locks[user.id]
                else:
                    self._user_locks[user.id] = (lock, references - 1)

    async def initialize(self) -> None:
        return

    async def shutdown(self) -> None:
        async with self._locks_guard:
            self._user_locks.clear()


async def _restore_from_telegram(bot: object) -> tuple[str, int] | None:
    temporary_path = DATABASE_PATH.with_name("database_restore_download.tmp")
    try:
        chat = await bot.get_chat(BACKUP_CHANNEL_ID)
        pinned_message = chat.pinned_message
        if pinned_message is None or pinned_message.document is None:
            logging.warning("В канале бэкапов нет закреплённого документа базы данных.")
            return None
        document = pinned_message.document
        if document.file_size and document.file_size > BACKUP_MAX_DOWNLOAD_SIZE:
            logging.warning(
                "Бэкап имеет размер %.2f МБ — он превышает лимит скачивания Telegram-ботом 20 МБ.",
                document.file_size / (1024 * 1024),
            )
            return None
        telegram_file = await bot.get_file(document.file_id)
        await telegram_file.download_to_drive(custom_path=str(temporary_path))
        valid = await asyncio.to_thread(database.validate_sqlite, temporary_path)
        if not valid:
            logging.error("Закреплённый в канале файл не прошёл PRAGMA integrity_check.")
            return None
        staged_archives = await _stage_message_archives(bot, temporary_path)
        await asyncio.to_thread(database.replace_database_from, temporary_path)
        await _install_staged_archives(staged_archives)
        database.reset_backup_state()
        logging.info("Рабочая БД восстановлена из закреплённого файла канала.")
        return document.file_id, pinned_message.message_id
    except (TelegramError, OSError, ValueError, sqlite3.Error):
        logging.exception("Не удалось восстановить БД из канала бэкапов.")
        return None
    finally:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            logging.warning("Не удалось удалить временный файл восстановления %s", temporary_path)


async def _restore_database(bot: object) -> tuple[str, int] | None:
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    if await asyncio.to_thread(database.has_application_state, DATABASE_PATH):
        try:
            staged_archives = await _stage_message_archives(bot, DATABASE_PATH)
            if staged_archives:
                await _install_staged_archives(staged_archives)
        except (TelegramError, OSError, ValueError, sqlite3.Error):
            logging.exception(
                "Не удалось восстановить архивы к существующей database.db; "
                "текущая рабочая база сохранена."
            )
        logging.info(
            "Используется существующая database.db; более старый закреплённый бэкап "
            "не будет заменять актуальные локальные данные."
        )
        return None

    restored_info = await _restore_from_telegram(bot)
    if restored_info is not None:
        return restored_info

    if await asyncio.to_thread(database.validate_sqlite, DATABASE_BACKUP_PATH):
        try:
            staged_archives = await _stage_message_archives(bot, DATABASE_BACKUP_PATH)
            await asyncio.to_thread(database.replace_database_from, DATABASE_BACKUP_PATH)
            await _install_staged_archives(staged_archives)
            database.reset_backup_state()
            logging.info("Рабочая БД восстановлена из локального database_backup.db.")
            return None
        except (TelegramError, OSError, ValueError, sqlite3.Error):
            logging.exception("Не удалось использовать локальный database_backup.db.")

    if await asyncio.to_thread(database.validate_sqlite, DATABASE_PATH):
        logging.warning("Удалённый и локальный бэкапы недоступны; оставлена текущая database.db.")
        return None

    if DATABASE_PATH.exists():
        damaged_path = DATABASE_PATH.with_name("database.corrupt.db")
        try:
            os.replace(DATABASE_PATH, damaged_path)
            logging.error("Повреждённая БД сохранена отдельно: %s", damaged_path)
        except OSError:
            logging.exception("Не удалось переместить повреждённую database.db.")
            raise
    logging.warning("Рабочая БД и бэкапы отсутствуют или повреждены; будет создана новая база.")
    return None


async def _restore_user_sessions(bot: object) -> None:
    manifest = await database.get_telegram_session_backup_manifest()
    for user_id, entry in manifest.items():
        session_path = personal_account.session_file_path(user_id)
        if session_path.is_file():
            continue
        file_id = entry.get("file_id")
        if not isinstance(file_id, str) or not file_id:
            logging.error("В бэкапе отсутствует file_id Telegram-сессии user_id=%s.", user_id)
            continue
        temporary_path = session_path.with_name(session_path.name + ".restore.tmp")
        try:
            session_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            telegram_file = await bot.get_file(file_id)
            if telegram_file.file_size and telegram_file.file_size > BACKUP_MAX_DOWNLOAD_SIZE:
                raise ValueError(
                    f"Бэкап Telegram-сессии user_id={user_id} превышает лимит скачивания."
                )
            await telegram_file.download_to_drive(custom_path=str(temporary_path))
            if not await asyncio.to_thread(database.validate_sqlite, temporary_path):
                raise sqlite3.DatabaseError(
                    f"Бэкап Telegram-сессии user_id={user_id} повреждён."
                )
            await asyncio.to_thread(os.replace, temporary_path, session_path)
            personal_account.set_enabled(bool(entry.get("enabled", False)), user_id)
            logging.info("Telegram user-сессия восстановлена из бэкапа user_id=%s.", user_id)
        except (TelegramError, OSError, ValueError, sqlite3.Error):
            logging.exception("Не удалось восстановить Telegram user-сессию user_id=%s.", user_id)
        finally:
            temporary_path.unlink(missing_ok=True)


async def _stage_message_archives(
    bot: object,
    manifest_path: str | os.PathLike[str],
) -> list[tuple[Path, Path]]:
    manifest = await asyncio.to_thread(database.get_message_archive_manifest, manifest_path)
    if not manifest:
        return []
    DATABASE_ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    staged: list[tuple[Path, Path]] = []
    try:
        for filename, file_id in sorted(manifest.items()):
            if (
                not filename.startswith("messages_")
                or Path(filename).name != filename
                or not filename.endswith(".db")
            ):
                raise ValueError(f"Недопустимое имя архива в manifest: {filename!r}")
            target = DATABASE_ARCHIVE_DIR / filename
            temporary = target.with_name(target.name + ".restore.tmp")
            staged.append((temporary, target))
            telegram_file = await bot.get_file(file_id)
            if telegram_file.file_size and telegram_file.file_size > BACKUP_MAX_DOWNLOAD_SIZE:
                raise ValueError(f"Архив {filename} превышает лимит Telegram на скачивание.")
            await telegram_file.download_to_drive(custom_path=str(temporary))
            if not await asyncio.to_thread(database.validate_sqlite, temporary):
                raise sqlite3.DatabaseError(f"Архив сообщений повреждён: {filename}")
    except Exception:
        for temporary, _target in staged:
            temporary.unlink(missing_ok=True)
        raise
    return staged


async def _install_staged_archives(staged: list[tuple[Path, Path]]) -> None:
    staged_names = {target.name for _temporary, target in staged}
    orphan_dir = DATABASE_ARCHIVE_DIR / "orphaned"
    for archive_path in database.list_message_archives():
        if archive_path.name in staged_names:
            continue
        orphan_dir.mkdir(parents=True, exist_ok=True)
        orphan_path = orphan_dir / archive_path.name
        suffix = 1
        while orphan_path.exists():
            orphan_path = orphan_dir / f"{archive_path.name}.{suffix}"
            suffix += 1
        await asyncio.to_thread(os.replace, archive_path, orphan_path)
    for temporary, target in staged:
        await asyncio.to_thread(os.replace, temporary, target)


async def _post_init(application: Application) -> None:
    log_handler = install_channel_log_handler(application.bot)
    application.bot_data["channel_log_handler"] = log_handler
    await send_channel_event(
        application.bot,
        f"▶️ Юбара запускается. HTTP healthcheck: порт {PORT}; проверяю инициализацию базы данных.",
    )
    restored_info = await _restore_database(application.bot)
    await database.initialize()
    recovered_rounds = await database.recover_pending_casino_rounds()
    if recovered_rounds:
        logging.warning(
            "Возвращены монеты за незавершённые крутки: %s.",
            recovered_rounds,
        )
    await database.purge_private_message_history()
    await database.rotate_messages_if_needed()
    await _restore_user_sessions(application.bot)
    try:
        feature_user_ids = await database.list_feature_user_ids("autoresponder")
        if await personal_account.startup(feature_user_ids):
            logging.info("Сохранённая Telegram user-сессия восстановлена.")
    except Exception:
        logging.exception("Не удалось восстановить Telegram user-сессию.")
    if restored_info is not None:
        await database.set_backup_meta(*restored_info)
    if application.job_queue is None:
        raise RuntimeError(
            "JobQueue недоступен. Установите python-telegram-bot с extra [job-queue]."
        )
    application.job_queue.run_repeating(
        _backup_job,
        interval=BACKUP_INTERVAL_SECONDS,
        first=BACKUP_INTERVAL_SECONDS,
        name="database-backup",
    )
    application.job_queue.run_daily(
        _daily_duel_mining_job,
        time=datetime_time(0, 5, tzinfo=MOSCOW_TIMEZONE),
        name="daily-duel-mining",
    )
    application.job_queue.run_repeating(
        audit_subscription_bonuses,
        interval=CASINO_BONUS_AUDIT_INTERVAL_SECONDS,
        first=CASINO_BONUS_AUDIT_INTERVAL_SECONDS,
        name="subscription-bonus-audit",
    )
    await autoresponder.initialize(application)
    await send_channel_event(
        application.bot,
        "✅ Юбара запущена: polling и резервное копирование активны.",
    )


async def _perform_backup(bot: object) -> None:
    try:
        caption = "📅 Дата и время: " + datetime.now(
            MOSCOW_TIMEZONE
        ).strftime("%d.%m.%Y в %H:%M:%S")
        for archive_path in database.list_message_archives():
            if await database.get_message_archive_file_id(archive_path.name):
                continue
            previous_message_id = await database.get_message_archive_message_id(
                archive_path.name
            )
            archive_size = archive_path.stat().st_size
            if archive_size >= BACKUP_MAX_DOWNLOAD_SIZE - 1024 * 1024:
                logging.warning(
                    "Архив %s приблизился к лимиту скачивания Telegram: %.2f МБ.",
                    archive_path.name,
                    archive_size / (1024 * 1024),
                )
            with archive_path.open("rb") as archive_file:
                archived_message = await bot.send_document(
                    chat_id=BACKUP_CHANNEL_ID,
                    document=archive_file,
                    filename=archive_path.name,
                    caption=f"{caption}\n🗄 Архив сообщений: {archive_path.name}",
                    disable_notification=True,
                )
            if archived_message.document is None:
                raise RuntimeError(
                    f"Telegram принял отправку архива {archive_path.name} без document."
                )
            await database.set_message_archive_backup_meta(
                archive_path.name,
                archived_message.document.file_id,
                archived_message.message_id,
            )
            if previous_message_id is not None and previous_message_id != archived_message.message_id:
                try:
                    await bot.delete_message(BACKUP_CHANNEL_ID, previous_message_id)
                except TelegramError:
                    logging.exception(
                        "Не удалось удалить старый зашифрованный архив %s.",
                        archive_path.name,
                    )
            logging.info(
                "Архив сообщений загружен в канал (file=%s, message_id=%s).",
                archive_path.name,
                archived_message.message_id,
            )

        await _backup_encrypted_histories(bot, caption)
        await _backup_profile_banners(bot, caption)

        session_manifest = await database.get_telegram_session_backup_manifest()
        for user_id in personal_account.session_user_ids():
            session_path = personal_account.session_file_path(user_id)
            if not session_path.is_file():
                continue
            source_stat = session_path.stat()
            previous = session_manifest.get(user_id, {})
            session_enabled = personal_account.is_enabled(user_id)
            if (
                previous.get("modified_ns") == source_stat.st_mtime_ns
                and previous.get("size") == source_stat.st_size
                and previous.get("enabled") == session_enabled
            ):
                continue
            temporary_path = session_path.with_name(session_path.name + ".backup.tmp")
            try:
                if not await database.is_telegram_session_backup_warning_sent(user_id):
                    await bot.send_message(
                        chat_id=user_id,
                        text=(
                            "⚠️ Важно: файл Telegram-сессии даёт полный доступ к аккаунту. "
                            "Перед сохранением в канал резервных копий убедись, что "
                            "BACKUP_CHANNEL_ID доступен только доверенным людям."
                        ),
                        disable_notification=True,
                    )
                    await database.mark_telegram_session_backup_warning_sent(user_id)
                await asyncio.to_thread(
                    personal_account.snapshot_session_for_backup,
                    user_id,
                    temporary_path,
                )
                if not await asyncio.to_thread(database.validate_sqlite, temporary_path):
                    raise sqlite3.DatabaseError(
                        f"Снимок Telegram-сессии user_id={user_id} повреждён."
                    )
                if temporary_path.stat().st_size > BACKUP_MAX_DOWNLOAD_SIZE:
                    raise ValueError(
                        f"Снимок Telegram-сессии user_id={user_id} превышает лимит Telegram."
                    )
                with temporary_path.open("rb") as session_file:
                    session_message = await bot.send_document(
                        chat_id=BACKUP_CHANNEL_ID,
                        document=session_file,
                        filename=f"telegram_session_{user_id}.session",
                        caption=(
                            f"{caption}\n🔐 Telegram user-сессия пользователя {user_id}; "
                            "даёт доступ к аккаунту — храните канал бэкапов приватным."
                        ),
                        disable_notification=True,
                    )
                if session_message.document is None:
                    raise RuntimeError(
                        f"Telegram принял файл сессии user_id={user_id} без document."
                    )
                await database.set_telegram_session_backup(
                    user_id,
                    file_id=session_message.document.file_id,
                    message_id=session_message.message_id,
                    modified_ns=source_stat.st_mtime_ns,
                    size=source_stat.st_size,
                    enabled=session_enabled,
                )
                old_message_id = previous.get("message_id")
                if isinstance(old_message_id, int) and old_message_id != session_message.message_id:
                    try:
                        await bot.delete_message(BACKUP_CHANNEL_ID, old_message_id)
                    except TelegramError:
                        logging.exception(
                            "Не удалось удалить предыдущий бэкап Telegram-сессии user_id=%s.",
                            user_id,
                        )
            except Exception:
                logging.exception(
                    "Не удалось сохранить Telegram user-сессию user_id=%s в бэкап.",
                    user_id,
                )
            finally:
                temporary_path.unlink(missing_ok=True)

        generation = await database.create_consistent_backup()
        if generation is None:
            logging.info("Бэкап пропущен: после последней синхронизации записей не было.")
            return
        size = DATABASE_BACKUP_PATH.stat().st_size
        if size > BACKUP_MAX_DOWNLOAD_SIZE:
            logging.warning(
                "database_backup.db приблизилась к лимиту скачивания Telegram: %.2f МБ.",
                size / (1024 * 1024),
            )
        previous_message_id = await database.get_meta("backup_message_id")
        with DATABASE_BACKUP_PATH.open("rb") as backup_file:
            sent_message = await bot.send_document(
                chat_id=BACKUP_CHANNEL_ID,
                document=backup_file,
                filename="database_backup.db",
                caption=caption,
                disable_notification=True,
            )
        await bot.pin_chat_message(
            chat_id=BACKUP_CHANNEL_ID,
            message_id=sent_message.message_id,
            disable_notification=True,
        )
        if sent_message.document is None:
            raise RuntimeError("Telegram принял отправку без document в ответе.")

        if previous_message_id:
            try:
                old_id = int(previous_message_id)
                if old_id != sent_message.message_id:
                    await bot.delete_message(BACKUP_CHANNEL_ID, old_id)
            except (ValueError, TelegramError):
                logging.exception("Не удалось удалить предыдущий файл бэкапа из канала.")

        await database.set_backup_meta(
            sent_message.document.file_id if sent_message.document else "",
            sent_message.message_id,
        )
        await database.mark_backup_synchronized(generation)
        logging.info("База успешно опубликована в канале бэкапов (message_id=%s).", sent_message.message_id)
    except Exception:
        logging.exception("Ошибка публикации базы данных в канал бэкапов.")


async def _backup_profile_banners(bot: object, caption: str) -> None:
    try:
        profiles = await database.get_profile_banner_records()
    except Exception:
        logging.exception("Не удалось получить список баннеров профилей для бэкапа.")
        return
    for profile in profiles:
        user_id = int(profile["user_id"])
        if await database.get_active_banner_item(user_id) is not None:
            continue
        nickname = str(profile["nickname"])[:48]
        decoration = str(profile["decoration"])
        signature = hashlib.sha256(
            f"profile-banner-v1:{user_id}:{nickname}:{decoration}".encode("utf-8")
        ).hexdigest()
        meta_key = f"profile_banner_backup:{user_id}"
        previous_text = await database.get_meta(meta_key)
        try:
            previous = json.loads(previous_text) if previous_text else {}
            if previous.get("signature") == signature:
                continue
            banner = create_profile_card(nickname, decoration)
            sent_message = await bot.send_photo(
                chat_id=BACKUP_CHANNEL_ID,
                photo=banner,
                caption=(
                    f"{caption}\n🖼 Баннер профиля\n"
                    f"ID пользователя: {user_id}\nКличка: {nickname}"
                ),
                disable_notification=True,
            )
            if sent_message.photo is None:
                raise RuntimeError(
                    f"Telegram не вернул изображение баннера user_id={user_id}."
                )
            await database.set_meta(
                meta_key,
                json.dumps(
                    {
                        "signature": signature,
                        "file_id": sent_message.photo[-1].file_id,
                        "message_id": sent_message.message_id,
                    },
                    ensure_ascii=False,
                ),
            )
            old_message_id = previous.get("message_id")
            if isinstance(old_message_id, int):
                try:
                    await bot.delete_message(BACKUP_CHANNEL_ID, old_message_id)
                except TelegramError:
                    logging.exception(
                        "Не удалось удалить старый баннер профиля user_id=%s.",
                        user_id,
                    )
            logging.info("Баннер профиля user_id=%s сохранён в Telegram-бэкап.", user_id)
        except Exception:
            logging.exception(
                "Не удалось отправить баннер профиля user_id=%s в Telegram-бэкап.",
                user_id,
            )


async def _backup_encrypted_histories(bot: object, caption: str) -> None:
    cipher = Fernet(HISTORY_ENCRYPTION_KEY.encode("ascii"))
    for group_id in await database.list_allowed_groups():
        try:
            rows = await database.get_group_history_export_rows(group_id)
            content = database.format_group_history_export(rows)
            digest = hashlib.sha256(content).hexdigest()
            meta_key = f"history_backup:{group_id}"
            previous_text = await database.get_meta(meta_key)
            previous = json.loads(previous_text) if previous_text else {}
            if previous.get("digest") == digest:
                continue
            encrypted = io.BytesIO(cipher.encrypt(content))
            encrypted.name = f"history_{group_id}.txt.enc"
            sent_message = await bot.send_document(
                chat_id=BACKUP_CHANNEL_ID,
                document=encrypted,
                filename=encrypted.name,
                caption=f"{caption}\n🔐 Зашифрованная история группы {group_id}.",
                disable_notification=True,
            )
            if sent_message.document is None:
                raise RuntimeError(
                    f"Telegram не вернул document для истории группы {group_id}."
                )
            await database.set_meta(
                meta_key,
                json.dumps(
                    {
                        "digest": digest,
                        "file_id": sent_message.document.file_id,
                        "message_id": sent_message.message_id,
                    },
                    ensure_ascii=False,
                ),
            )
            previous_message_id = previous.get("message_id")
            if isinstance(previous_message_id, int):
                try:
                    await bot.delete_message(BACKUP_CHANNEL_ID, previous_message_id)
                except TelegramError:
                    logging.exception(
                        "Не удалось удалить старый зашифрованный архив истории группы %s.",
                        group_id,
                    )
            logging.info(
                "Зашифрованная история группы %s сохранена в Telegram-бэкап.",
                group_id,
            )
        except Exception:
            logging.exception(
                "Не удалось сохранить зашифрованную историю группы %s.",
                group_id,
            )


async def _backup_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    await _perform_backup(context.bot)


async def _daily_duel_mining_job(_context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await database.mine_duel_coins_daily()
    except Exception:
        logging.exception("Не удалось начислить ежедневные игровые монеты.")


async def _post_shutdown(application: Application) -> None:
    logging.info("Остановка: проверяю, требуется ли финальный бэкап.")
    await send_channel_event(application.bot, "⏹ Юбара останавливается; завершаю задачи и проверяю бэкап.")
    try:
        await autoresponder.shutdown()
    except Exception:
        logging.exception("Не удалось корректно остановить автоответчик.")
    try:
        await personal_account.disconnect_all()
    except Exception:
        logging.exception("Не удалось корректно отключить Telegram user-сессию.")
    await stop_image_jobs()
    await stop_banner_render_queue()
    await _perform_backup(application.bot)
    log_handler = application.bot_data.pop("channel_log_handler", None)
    if isinstance(log_handler, TelegramChannelLogHandler):
        remove_channel_log_handler(log_handler)


async def _error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    error = context.error
    if error is not None:
        logging.error(
            "Необработанная ошибка при обработке Telegram update_id=%s",
            update.update_id if isinstance(update, Update) else "unknown",
            exc_info=(type(error), error, error.__traceback__),
        )


def build_application() -> Application:
    application = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .concurrent_updates(PerUserUpdateProcessor(max_concurrent_updates=16))
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )
    application.add_handler(
        CallbackQueryHandler(private_access_callback_guard),
        group=-1,
    )
    application.add_handler(CallbackQueryHandler(on_image_callback, pattern=r"^img:"))
    application.add_handler(
        CallbackQueryHandler(
            on_game_callback,
            pattern=r"^(?:casino:|inv:|menu:|mkt:|bonus:)",
        ),
        group=1,
    )
    application.add_handler(
        CallbackQueryHandler(
            on_group_profile_stats_callback,
            pattern=r"^profile:groupstats:",
        )
    )
    application.add_handler(
        CallbackQueryHandler(on_profile_callback, pattern=r"^profile:"),
        group=1,
    )
    application.add_handler(
        CallbackQueryHandler(on_welcome_callback, pattern=r"^(?:wg|wa):")
    )
    application.add_handler(
        TypeHandler(MessageReactionUpdated, on_message_reaction)
    )
    application.add_handler(
        ChatMemberHandler(on_chat_member_update, ChatMemberHandler.CHAT_MEMBER)
    )
    application.add_handler(
        CallbackQueryHandler(handle_player_duel_callback, pattern=r"^pvp:")
    )
    application.add_handler(
        CommandHandler("break", break_command, filters=filters.ChatType.PRIVATE)
    )
    application.add_handler(
        CommandHandler(
            "testbanner",
            test_banner_command,
            filters=filters.ChatType.PRIVATE,
        )
    )
    application.add_handler(build_admin_conversation_handler())
    application.add_handler(build_delete_all_data_handler())
    application.add_handler(build_history_conversation_handler())
    application.add_handler(build_personal_autoresponder_conversation_handler())
    application.add_handler(
        MessageHandler(
            filters.ChatType.GROUPS & filters.StatusUpdate.NEW_CHAT_MEMBERS,
            on_new_members,
        )
    )
    application.add_handler(
        MessageHandler(filters.ChatType.GROUPS, on_group_activity),
        group=-2,
    )
    application.add_handler(
        MessageHandler(filters.ChatType.GROUPS & filters.TEXT, on_group_message)
    )
    application.add_handler(
        MessageHandler(filters.ChatType.PRIVATE & filters.TEXT, on_private_message)
    )
    application.add_handler(
        MessageHandler(
            filters.UpdateType.CHANNEL_POST & filters.StatusUpdate.PINNED_MESSAGE,
            _delete_channel_pin_notification,
        )
    )
    application.add_error_handler(_error_handler)
    return application


async def _delete_channel_pin_notification(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    message = update.channel_post
    if (
        message is None
        or message.chat_id != BACKUP_CHANNEL_ID
        or message.pinned_message is None
    ):
        return
    try:
        await context.bot.delete_message(
            chat_id=message.chat_id,
            message_id=message.message_id,
        )
        logging.info(
            "Удалено служебное уведомление о закреплении в канале (message_id=%s).",
            message.message_id,
        )
    except TelegramError:
        logging.exception(
            "Не удалось удалить служебное уведомление о закреплении в канале (message_id=%s).",
            message.message_id,
        )


def main() -> None:
    logging.info("Запуск Telegram-бота Юбары.")
    try:
        http_server = ThreadingHTTPServer(("0.0.0.0", PORT), HealthCheckHandler)
    except OSError:
        logging.exception("Не удалось запустить HTTP healthcheck на порту %s.", PORT)
        raise
    http_thread = threading.Thread(
        target=http_server.serve_forever,
        name="render-healthcheck",
        daemon=True,
    )
    http_thread.start()
    logging.info("HTTP healthcheck запущен на порту %s.", PORT)

    event_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(event_loop)
    try:
        build_application().run_polling(allowed_updates=Update.ALL_TYPES)
    finally:
        if not event_loop.is_closed():
            event_loop.close()
        asyncio.set_event_loop(None)
        http_server.shutdown()
        http_server.server_close()
        http_thread.join(timeout=5)
        logging.info("HTTP healthcheck остановлен.")


if __name__ == "__main__":
    main()
