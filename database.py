"""Асинхронный доступ к SQLite и создание согласованных копий БД."""

import asyncio
import logging
import os
import sqlite3
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiosqlite

from config import (
    DATABASE_BACKUP_PATH,
    DATABASE_PATH,
    MAX_TOTAL_MESSAGES_PER_CHAT,
)

_write_lock = asyncio.Lock()
_dirty_generation = 0
_synced_generation = 0


@asynccontextmanager
async def _connect() -> AsyncGenerator[aiosqlite.Connection, None]:
    connection = await aiosqlite.connect(DATABASE_PATH)
    connection.row_factory = aiosqlite.Row
    await connection.execute("PRAGMA foreign_keys = ON")
    try:
        yield connection
    finally:
        await connection.close()


def _record_write() -> None:
    global _dirty_generation
    _dirty_generation += 1


async def initialize() -> None:
    """Создаёт таблицы схемы проекта, сохраняя существующие данные."""
    global _dirty_generation, _synced_generation
    async with _write_lock:
        async with _connect() as db:
            await db.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER NOT NULL,
                    group_id INTEGER NOT NULL,
                    username TEXT,
                    gender TEXT NOT NULL DEFAULT 'неизвестен',
                    callsign TEXT,
                    notes TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (user_id, group_id)
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    sender_name TEXT NOT NULL,
                    text TEXT NOT NULL,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_messages_chat_id_id
                    ON messages(chat_id, id);
                CREATE TABLE IF NOT EXISTS allowed_groups (
                    group_id INTEGER PRIMARY KEY
                );
                CREATE TABLE IF NOT EXISTS access_keys (
                    key TEXT PRIMARY KEY,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    access_level TEXT NOT NULL DEFAULT 'user',
                    duration_seconds INTEGER,
                    created_by INTEGER
                );
                CREATE TABLE IF NOT EXISTS authorized_users (
                    user_id INTEGER PRIMARY KEY,
                    authorized_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    username TEXT,
                    access_level TEXT NOT NULL DEFAULT 'user',
                    expires_at REAL
                );
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS image_settings (
                    chat_id INTEGER PRIMARY KEY,
                    model_preference TEXT NOT NULL DEFAULT 'auto',
                    pollinations_model TEXT NOT NULL DEFAULT 'flux',
                    width INTEGER NOT NULL DEFAULT 768,
                    height INTEGER NOT NULL DEFAULT 1024,
                    negative_prompt TEXT NOT NULL DEFAULT '3d, photorealistic, realistic, 3d render, bad anatomy, bad hands',
                    seed TEXT
                );
                CREATE TRIGGER IF NOT EXISTS dirty_image_settings_insert
                AFTER INSERT ON image_settings BEGIN
                    INSERT INTO meta(key, value) VALUES ('dirty', '1')
                    ON CONFLICT(key) DO UPDATE SET value = '1';
                END;
                CREATE TRIGGER IF NOT EXISTS dirty_image_settings_update
                AFTER UPDATE ON image_settings BEGIN
                    INSERT INTO meta(key, value) VALUES ('dirty', '1')
                    ON CONFLICT(key) DO UPDATE SET value = '1';
                END;
                CREATE TRIGGER IF NOT EXISTS dirty_users_insert
                AFTER INSERT ON users BEGIN
                    INSERT INTO meta(key, value) VALUES ('dirty', '1')
                    ON CONFLICT(key) DO UPDATE SET value = '1';
                END;
                CREATE TRIGGER IF NOT EXISTS dirty_users_update
                AFTER UPDATE ON users BEGIN
                    INSERT INTO meta(key, value) VALUES ('dirty', '1')
                    ON CONFLICT(key) DO UPDATE SET value = '1';
                END;
                CREATE TRIGGER IF NOT EXISTS dirty_users_delete
                AFTER DELETE ON users BEGIN
                    INSERT INTO meta(key, value) VALUES ('dirty', '1')
                    ON CONFLICT(key) DO UPDATE SET value = '1';
                END;
                CREATE TRIGGER IF NOT EXISTS dirty_messages_insert
                AFTER INSERT ON messages BEGIN
                    INSERT INTO meta(key, value) VALUES ('dirty', '1')
                    ON CONFLICT(key) DO UPDATE SET value = '1';
                END;
                CREATE TRIGGER IF NOT EXISTS dirty_messages_update
                AFTER UPDATE ON messages BEGIN
                    INSERT INTO meta(key, value) VALUES ('dirty', '1')
                    ON CONFLICT(key) DO UPDATE SET value = '1';
                END;
                CREATE TRIGGER IF NOT EXISTS dirty_messages_delete
                AFTER DELETE ON messages BEGIN
                    INSERT INTO meta(key, value) VALUES ('dirty', '1')
                    ON CONFLICT(key) DO UPDATE SET value = '1';
                END;
                CREATE TRIGGER IF NOT EXISTS dirty_allowed_groups_insert
                AFTER INSERT ON allowed_groups BEGIN
                    INSERT INTO meta(key, value) VALUES ('dirty', '1')
                    ON CONFLICT(key) DO UPDATE SET value = '1';
                END;
                CREATE TRIGGER IF NOT EXISTS dirty_allowed_groups_delete
                AFTER DELETE ON allowed_groups BEGIN
                    INSERT INTO meta(key, value) VALUES ('dirty', '1')
                    ON CONFLICT(key) DO UPDATE SET value = '1';
                END;
                CREATE TRIGGER IF NOT EXISTS dirty_allowed_groups_update
                AFTER UPDATE ON allowed_groups BEGIN
                    INSERT INTO meta(key, value) VALUES ('dirty', '1')
                    ON CONFLICT(key) DO UPDATE SET value = '1';
                END;
                CREATE TRIGGER IF NOT EXISTS dirty_access_keys_insert
                AFTER INSERT ON access_keys BEGIN
                    INSERT INTO meta(key, value) VALUES ('dirty', '1')
                    ON CONFLICT(key) DO UPDATE SET value = '1';
                END;
                CREATE TRIGGER IF NOT EXISTS dirty_access_keys_delete
                AFTER DELETE ON access_keys BEGIN
                    INSERT INTO meta(key, value) VALUES ('dirty', '1')
                    ON CONFLICT(key) DO UPDATE SET value = '1';
                END;
                CREATE TRIGGER IF NOT EXISTS dirty_access_keys_update
                AFTER UPDATE ON access_keys BEGIN
                    INSERT INTO meta(key, value) VALUES ('dirty', '1')
                    ON CONFLICT(key) DO UPDATE SET value = '1';
                END;
                CREATE TRIGGER IF NOT EXISTS dirty_authorized_users_insert
                AFTER INSERT ON authorized_users BEGIN
                    INSERT INTO meta(key, value) VALUES ('dirty', '1')
                    ON CONFLICT(key) DO UPDATE SET value = '1';
                END;
                CREATE TRIGGER IF NOT EXISTS dirty_authorized_users_update
                AFTER UPDATE ON authorized_users BEGIN
                    INSERT INTO meta(key, value) VALUES ('dirty', '1')
                    ON CONFLICT(key) DO UPDATE SET value = '1';
                END;
                CREATE TRIGGER IF NOT EXISTS dirty_authorized_users_delete
                AFTER DELETE ON authorized_users BEGIN
                    INSERT INTO meta(key, value) VALUES ('dirty', '1')
                    ON CONFLICT(key) DO UPDATE SET value = '1';
                END;
                """
            )
            await _ensure_columns(
                db,
                "access_keys",
                {
                    "access_level": "TEXT NOT NULL DEFAULT 'user'",
                    "duration_seconds": "INTEGER",
                    "created_by": "INTEGER",
                },
            )
            await _ensure_columns(
                db,
                "authorized_users",
                {
                    "username": "TEXT",
                    "access_level": "TEXT NOT NULL DEFAULT 'user'",
                    "expires_at": "REAL",
                },
            )
            await _ensure_columns(
                db,
                "image_settings",
                {"pollinations_model": "TEXT NOT NULL DEFAULT 'flux'"},
            )
            await db.commit()
            cursor = await db.execute("SELECT value FROM meta WHERE key = 'dirty'")
            dirty = await cursor.fetchone()
    _dirty_generation = 1 if dirty and dirty["value"] == "1" else 0
    _synced_generation = 0
    logging.info("Схема SQLite готова: %s", DATABASE_PATH)


async def _ensure_columns(
    db: aiosqlite.Connection,
    table: str,
    columns: dict[str, str],
) -> None:
    cursor = await db.execute(f"PRAGMA table_info({table})")
    existing = {str(row["name"]) for row in await cursor.fetchall()}
    for column, declaration in columns.items():
        if column not in existing:
            await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")


async def is_group_allowed(group_id: int) -> bool:
    async with _connect() as db:
        cursor = await db.execute(
            "SELECT 1 FROM allowed_groups WHERE group_id = ?",
            (group_id,),
        )
        return await cursor.fetchone() is not None


async def list_allowed_groups() -> list[int]:
    async with _connect() as db:
        cursor = await db.execute("SELECT group_id FROM allowed_groups ORDER BY group_id")
        rows = await cursor.fetchall()
        return [int(row["group_id"]) for row in rows]


async def get_image_settings(chat_id: int) -> dict[str, Any]:
    async with _connect() as db:
        cursor = await db.execute(
            "SELECT model_preference, pollinations_model, width, height, negative_prompt, seed "
            "FROM image_settings WHERE chat_id = ?",
            (chat_id,),
        )
        row = await cursor.fetchone()
    if row is None:
        return {
            "model_preference": "auto",
            "pollinations_model": "flux",
            "width": 768,
            "height": 1024,
            "negative_prompt": "3d, photorealistic, realistic, 3d render, bad anatomy, bad hands",
            "seed": None,
        }
    return dict(row)


async def set_image_setting(chat_id: int, setting: str, value: Any) -> None:
    columns = {
        "model_preference": "model_preference",
        "pollinations_model": "pollinations_model",
        "negative_prompt": "negative_prompt",
        "seed": "seed",
    }
    if setting == "model_preference" and value not in ("auto", "animagine", "anime", "anything"):
        raise ValueError("Недопустимое предпочтение модели AI Horde.")
    if setting == "pollinations_model" and value not in ("flux", "turbo"):
        raise ValueError("Недопустимая модель Pollinations.")
    if setting == "size" and value not in (
        (1024, 576),
        (576, 1024),
        (1024, 1024),
        (768, 768),
    ):
        raise ValueError("Недопустимый размер изображения.")
    if setting == "negative_prompt" and (
        not isinstance(value, str) or len(value) > 500
    ):
        raise ValueError("Негативный промпт должен содержать не более 500 символов.")
    if setting == "seed" and value is not None and (
        not isinstance(value, str) or not value.isdigit() or len(value) > 10
    ):
        raise ValueError("Seed должен быть целым числом или None.")
    if setting not in columns and setting != "size":
        raise ValueError(f"Неизвестная настройка изображения: {setting}")

    async with _write_lock:
        async with _connect() as db:
            await db.execute(
                "INSERT OR IGNORE INTO image_settings(chat_id) VALUES (?)",
                (chat_id,),
            )
            if setting == "size":
                width, height = value
                await db.execute(
                    "UPDATE image_settings SET width = ?, height = ? WHERE chat_id = ?",
                    (width, height, chat_id),
                )
            else:
                column = columns[setting]
                await db.execute(
                    f"UPDATE image_settings SET {column} = ? WHERE chat_id = ?",
                    (value, chat_id),
                )
            await db.commit()
        _record_write()


async def add_allowed_group(group_id: int) -> bool:
    async with _write_lock:
        async with _connect() as db:
            cursor = await db.execute(
                "INSERT OR IGNORE INTO allowed_groups(group_id) VALUES (?)",
                (group_id,),
            )
            await db.commit()
            inserted = cursor.rowcount > 0
        if inserted:
            _record_write()
        return inserted


async def remove_allowed_group(group_id: int) -> bool:
    async with _write_lock:
        async with _connect() as db:
            cursor = await db.execute(
                "DELETE FROM allowed_groups WHERE group_id = ?",
                (group_id,),
            )
            await db.commit()
            deleted = cursor.rowcount > 0
        if deleted:
            _record_write()
        return deleted


async def ensure_user(user_id: int, group_id: int, username: str | None) -> None:
    async with _write_lock:
        async with _connect() as db:
            cursor = await db.execute(
                """
                INSERT OR IGNORE INTO users(user_id, group_id, username)
                VALUES (?, ?, ?)
                """,
                (user_id, group_id, username),
            )
            inserted = cursor.rowcount > 0
            if username is not None:
                update = await db.execute(
                    """
                    UPDATE users SET username = ?
                    WHERE user_id = ? AND group_id = ? AND username IS NOT ?
                    """,
                    (username, user_id, group_id, username),
                )
                inserted = inserted or update.rowcount > 0
            await db.commit()
        if inserted:
            _record_write()


async def get_user_data(user_id: int, group_id: int) -> dict[str, Any] | None:
    async with _connect() as db:
        cursor = await db.execute(
            """
            SELECT user_id, group_id, username, gender, callsign, notes
            FROM users WHERE user_id = ? AND group_id = ?
            """,
            (user_id, group_id),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def get_group_users(group_id: int) -> list[dict[str, Any]]:
    async with _connect() as db:
        cursor = await db.execute(
            """
            SELECT user_id, username, gender, callsign, notes
            FROM users WHERE group_id = ?
            ORDER BY COALESCE(callsign, username, CAST(user_id AS TEXT))
            """,
            (group_id,),
        )
        return [dict(row) for row in await cursor.fetchall()]


async def update_user_profile(
    user_id: int,
    group_id: int,
    *,
    gender: str | None = None,
    callsign: str | None = None,
    note: str | None = None,
) -> bool:
    assignments: list[str] = []
    values: list[str | int] = []
    if gender is not None:
        assignments.append("gender = ?")
        values.append(gender)
    if callsign is not None:
        assignments.append("callsign = ?")
        values.append(callsign)
    if note is not None:
        assignments.append(
            "notes = CASE WHEN notes = '' THEN ? ELSE notes || char(10) || ? END"
        )
        values.extend((note, note))
    if not assignments:
        return False

    async with _write_lock:
        async with _connect() as db:
            cursor = await db.execute(
                f"UPDATE users SET {', '.join(assignments)} "
                "WHERE user_id = ? AND group_id = ?",
                (*values, user_id, group_id),
            )
            await db.commit()
            updated = cursor.rowcount > 0
        if updated:
            _record_write()
        return updated


async def forget_user(user_id: int, group_id: int) -> bool:
    async with _write_lock:
        async with _connect() as db:
            cursor = await db.execute(
                "DELETE FROM users WHERE user_id = ? AND group_id = ?",
                (user_id, group_id),
            )
            await db.commit()
            deleted = cursor.rowcount > 0
        if deleted:
            _record_write()
        return deleted


async def save_message(
    chat_id: int,
    user_id: int,
    message_id: int,
    sender_name: str,
    text: str,
) -> None:
    async with _write_lock:
        async with _connect() as db:
            await db.execute(
                """
                INSERT INTO messages(chat_id, user_id, message_id, sender_name, text)
                VALUES (?, ?, ?, ?, ?)
                """,
                (chat_id, user_id, message_id, sender_name, text),
            )
            cursor = await db.execute(
                "SELECT COUNT(*) FROM messages WHERE chat_id = ?",
                (chat_id,),
            )
            count = int((await cursor.fetchone())[0])
            if count > MAX_TOTAL_MESSAGES_PER_CHAT:
                await db.execute(
                    """
                    DELETE FROM messages
                    WHERE id IN (
                        SELECT id FROM messages WHERE chat_id = ?
                        ORDER BY id ASC LIMIT ?
                    )
                    """,
                    (chat_id, count - MAX_TOTAL_MESSAGES_PER_CHAT),
                )
            await db.commit()
        _record_write()


async def get_recent_history(chat_id: int, limit: int) -> list[tuple[int, str, int, str]]:
    async with _connect() as db:
        cursor = await db.execute(
            """
            SELECT message_id, sender_name, user_id, text
            FROM messages WHERE chat_id = ?
            ORDER BY id DESC LIMIT ?
            """,
            (chat_id, max(1, limit)),
        )
        rows = await cursor.fetchall()
        return [
            (int(row["message_id"]), row["sender_name"], int(row["user_id"]), row["text"])
            for row in reversed(rows)
        ]


async def get_authorized_user(user_id: int) -> dict[str, Any] | None:
    async with _connect() as db:
        cursor = await db.execute(
            """
            SELECT user_id, username, access_level, expires_at
            FROM authorized_users
            WHERE user_id = ? AND (expires_at IS NULL OR expires_at > ?)
            """,
            (user_id, time.time()),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def is_authorized(user_id: int) -> bool:
    return await get_authorized_user(user_id) is not None


async def is_admin_user(user_id: int) -> bool:
    record = await get_authorized_user(user_id)
    return bool(record and record["access_level"] == "admin")


async def consume_access_key(
    key: str,
    user_id: int,
    username: str | None = None,
) -> dict[str, Any] | None:
    """Атомарно расходует ключ и выдаёт выбранный уровень доступа на срок."""
    async with _write_lock:
        async with _connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                """
                SELECT access_level, duration_seconds
                FROM access_keys WHERE key = ?
                """,
                (key,),
            )
            key_record = await cursor.fetchone()
            if key_record is None:
                await db.rollback()
                return None
            cursor = await db.execute(
                """
                SELECT access_level, expires_at
                FROM authorized_users WHERE user_id = ?
                """,
                (user_id,),
            )
            previous_record = await cursor.fetchone()
            expires_at = (
                time.time() + int(key_record["duration_seconds"])
                if key_record["duration_seconds"] is not None
                else None
            )
            granted_level = str(key_record["access_level"])
            if previous_record and previous_record["access_level"] == "admin" and granted_level == "user":
                granted_level = "admin"
                expires_at = previous_record["expires_at"]
            await db.execute("DELETE FROM access_keys WHERE key = ?", (key,))
            await db.execute(
                """
                INSERT INTO authorized_users(
                    user_id, username, access_level, expires_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    username = excluded.username,
                    access_level = excluded.access_level,
                    expires_at = excluded.expires_at,
                    authorized_at = CURRENT_TIMESTAMP
                """,
                (user_id, username, granted_level, expires_at),
            )
            await db.commit()
        _record_write()
        return {
            "access_level": granted_level,
            "expires_at": expires_at,
        }


async def create_access_key(
    key: str,
    *,
    access_level: str = "user",
    duration_seconds: int | None = None,
    created_by: int | None = None,
) -> bool:
    if access_level not in ("user", "admin"):
        raise ValueError("access_level должен быть user или admin.")
    if duration_seconds is not None and duration_seconds <= 0:
        raise ValueError("duration_seconds должен быть положительным.")
    async with _write_lock:
        async with _connect() as db:
            try:
                await db.execute(
                    """
                    INSERT INTO access_keys(
                        key, access_level, duration_seconds, created_by
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (key, access_level, duration_seconds, created_by),
                )
            except aiosqlite.IntegrityError:
                await db.rollback()
                return False
            await db.commit()
        _record_write()
        return True


async def list_access_keys() -> list[dict[str, Any]]:
    async with _connect() as db:
        cursor = await db.execute(
            """
            SELECT key, access_level, duration_seconds, created_by
            FROM access_keys ORDER BY created_at, key
            """
        )
        return [dict(row) for row in await cursor.fetchall()]


async def revoke_access_key(key: str) -> bool:
    async with _write_lock:
        async with _connect() as db:
            cursor = await db.execute("DELETE FROM access_keys WHERE key = ?", (key,))
            await db.commit()
            deleted = cursor.rowcount > 0
        if deleted:
            _record_write()
        return deleted


async def list_authorized_users(
    *,
    access_level: str = "user",
) -> list[dict[str, Any]]:
    if access_level not in ("user", "admin"):
        raise ValueError("access_level должен быть user или admin.")
    async with _connect() as db:
        cursor = await db.execute(
            """
            SELECT user_id, username, access_level, expires_at
            FROM authorized_users
            WHERE access_level = ? AND (expires_at IS NULL OR expires_at > ?)
            ORDER BY user_id
            """,
            (access_level, time.time()),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def update_authorized_username(user_id: int, username: str | None) -> None:
    async with _write_lock:
        async with _connect() as db:
            cursor = await db.execute(
                """
                UPDATE authorized_users SET username = ?
                WHERE user_id = ? AND username IS NOT ?
                """,
                (username, user_id, username),
            )
            await db.commit()
            updated = cursor.rowcount > 0
        if updated:
            _record_write()


async def revoke_authorized_user(user_id: int, *, access_level: str) -> bool:
    if access_level not in ("user", "admin"):
        raise ValueError("access_level должен быть user или admin.")
    async with _write_lock:
        async with _connect() as db:
            cursor = await db.execute(
                "DELETE FROM authorized_users WHERE user_id = ? AND access_level = ?",
                (user_id, access_level),
            )
            await db.commit()
            deleted = cursor.rowcount > 0
        if deleted:
            _record_write()
        return deleted


async def get_meta(key: str) -> str | None:
    async with _connect() as db:
        cursor = await db.execute("SELECT value FROM meta WHERE key = ?", (key,))
        row = await cursor.fetchone()
        return str(row["value"]) if row else None


async def set_backup_meta(file_id: str, message_id: int) -> None:
    async with _write_lock:
        async with _connect() as db:
            await db.executemany(
                "INSERT INTO meta(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (
                    ("backup_file_id", file_id),
                    ("backup_message_id", str(message_id)),
                ),
            )
            await db.commit()


async def create_consistent_backup() -> int | None:
    """Делает online backup; возвращает поколение записей в снимке."""
    async with _write_lock:
        if _dirty_generation <= _synced_generation:
            return None
        snapshot_generation = _dirty_generation
        DATABASE_BACKUP_PATH.parent.mkdir(parents=True, exist_ok=True)
        async with _connect() as source:
            target: sqlite3.Connection | None = await asyncio.to_thread(
                sqlite3.connect,
                DATABASE_BACKUP_PATH,
                check_same_thread=False,
            )
            try:
                await source.backup(target)
                await asyncio.to_thread(_finalize_snapshot, target)
                target = None
            finally:
                if target is not None:
                    await asyncio.to_thread(target.close)
        return snapshot_generation


def _finalize_snapshot(target: sqlite3.Connection) -> None:
    target.execute(
        "INSERT INTO meta(key, value) VALUES ('dirty', '0') "
        "ON CONFLICT(key) DO UPDATE SET value = '0'"
    )
    target.commit()
    target.close()


async def mark_backup_synchronized(generation: int) -> None:
    global _synced_generation
    async with _write_lock:
        if _dirty_generation <= _synced_generation:
            return
        if _dirty_generation <= generation:
            async with _connect() as db:
                await db.execute(
                    "INSERT INTO meta(key, value) VALUES ('dirty', '0') "
                    "ON CONFLICT(key) DO UPDATE SET value = '0'"
                )
                await db.commit()
        _synced_generation = max(_synced_generation, generation)


def validate_sqlite(path: str | Path) -> bool:
    """Проверяет целостность файла SQLite без создания новой базы."""
    file_path = Path(path)
    if not file_path.is_file() or file_path.stat().st_size == 0:
        return False
    try:
        connection = sqlite3.connect(f"file:{file_path.as_posix()}?mode=ro", uri=True)
        try:
            result = connection.execute("PRAGMA integrity_check").fetchone()
            return result is not None and result[0] == "ok"
        finally:
            connection.close()
    except sqlite3.Error:
        logging.exception("Файл не прошёл проверку SQLite: %s", file_path)
        return False


def replace_database_from(source: str | Path) -> None:
    """Атомарно заменяет рабочую БД проверенной локальной копией."""
    source_path = Path(source)
    if not validate_sqlite(source_path):
        raise sqlite3.DatabaseError(f"Некорректная SQLite-база: {source_path}")
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    if source_path.resolve() == DATABASE_PATH.resolve():
        return
    staging = DATABASE_PATH.with_suffix(".restore.tmp")
    source_connection = sqlite3.connect(
        f"file:{source_path.as_posix()}?mode=ro",
        uri=True,
    )
    staging_connection = sqlite3.connect(staging, check_same_thread=False)
    try:
        source_connection.backup(staging_connection)
        staging_connection.commit()
    finally:
        staging_connection.close()
        source_connection.close()
    os.replace(staging, DATABASE_PATH)


def reset_backup_state() -> None:
    global _dirty_generation, _synced_generation
    _dirty_generation = 0
    _synced_generation = 0
