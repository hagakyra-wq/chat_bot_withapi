"""Точка входа: восстановление данных, Render healthcheck и polling."""

import asyncio
import logging
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from telegram import Update
from telegram.error import TelegramError
from telegram.ext import Application, CallbackQueryHandler, ContextTypes, MessageHandler, filters

import database
from config import (
    BACKUP_CHANNEL_ID,
    BACKUP_INTERVAL_SECONDS,
    BACKUP_MAX_DOWNLOAD_SIZE,
    DATABASE_BACKUP_PATH,
    DATABASE_PATH,
    PORT,
    TELEGRAM_TOKEN,
)
from handlers_group import on_group_message
from handlers_pm import build_admin_conversation_handler, on_private_message
from images import on_image_callback, stop_image_jobs


class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, _format: str, *_args: object) -> None:
        del _format, _args


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
        await asyncio.to_thread(database.replace_database_from, temporary_path)
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
    restored_info = await _restore_from_telegram(bot)
    if restored_info is not None:
        return restored_info

    if await asyncio.to_thread(database.validate_sqlite, DATABASE_BACKUP_PATH):
        try:
            await asyncio.to_thread(database.replace_database_from, DATABASE_BACKUP_PATH)
            database.reset_backup_state()
            logging.info("Рабочая БД восстановлена из локального database_backup.db.")
            return None
        except (OSError, ValueError, sqlite3.Error):
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


async def _post_init(application: Application) -> None:
    restored_info = await _restore_database(application.bot)
    await database.initialize()
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


async def _perform_backup(bot: object) -> None:
    try:
        generation = await database.create_consistent_backup()
        if generation is None:
            logging.info("Бэкап пропущен: после последней синхронизации записей не было.")
            return
        size = DATABASE_BACKUP_PATH.stat().st_size
        if size > BACKUP_MAX_DOWNLOAD_SIZE:
            logging.warning(
                "database_backup.db больше лимита скачивания Telegram: %.2f МБ.",
                size / (1024 * 1024),
            )
        previous_message_id = await database.get_meta("backup_message_id")
        caption = "📅 Дата и время: " + datetime.now(
            timezone(timedelta(hours=7))
        ).strftime("%d.%m.%Y в %H:%M:%S")
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


async def _backup_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    await _perform_backup(context.bot)


async def _post_shutdown(application: Application) -> None:
    logging.info("Остановка: проверяю, требуется ли финальный бэкап.")
    await stop_image_jobs()
    await _perform_backup(application.bot)


async def _error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    error = context.error
    if error is not None:
        logging.error(
            "Необработанная ошибка при обработке Telegram update %r",
            update,
            exc_info=(type(error), error, error.__traceback__),
        )


def build_application() -> Application:
    application = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )
    application.add_handler(CallbackQueryHandler(on_image_callback, pattern=r"^img:"))
    application.add_handler(build_admin_conversation_handler())
    application.add_handler(
        MessageHandler(filters.ChatType.GROUPS & filters.TEXT, on_group_message)
    )
    application.add_handler(
        MessageHandler(filters.ChatType.PRIVATE & filters.TEXT, on_private_message)
    )
    application.add_error_handler(_error_handler)
    return application


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
