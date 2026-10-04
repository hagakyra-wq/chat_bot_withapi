"""Асинхронный доступ к SQLite и создание согласованных копий БД."""

import asyncio
import logging
import os
import sqlite3
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import aiosqlite

from config import (
    DATABASE_BACKUP_PATH,
    DATABASE_ARCHIVE_DIR,
    DATABASE_PATH,
    MAX_TOTAL_MESSAGES_PER_CHAT,
    DATABASE_SHARD_THRESHOLD_BYTES,
)

_write_lock = asyncio.Lock()
_dirty_generation = 0
_synced_generation = 0
_CHAT_CONTEXT_ENABLED_KEY = "chat_context_enabled"


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
                CREATE TABLE IF NOT EXISTS global_profiles (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    display_name TEXT,
                    callsign TEXT,
                    age INTEGER,
                    gender TEXT NOT NULL DEFAULT 'неизвестен',
                    notes TEXT NOT NULL DEFAULT ''
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
                    pollinations_model TEXT NOT NULL DEFAULT 'flux',
                    width INTEGER NOT NULL DEFAULT 768,
                    height INTEGER NOT NULL DEFAULT 1024,
                    negative_prompt TEXT NOT NULL DEFAULT '3d, photorealistic, realistic, 3d render, bad anatomy, bad hands',
                    seed TEXT
                );
                CREATE TABLE IF NOT EXISTS duel_players (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    balance_cents INTEGER NOT NULL DEFAULT 500,
                    last_mined_on TEXT NOT NULL
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
                CREATE TRIGGER IF NOT EXISTS dirty_duel_players_insert
                AFTER INSERT ON duel_players BEGIN
                    INSERT INTO meta(key, value) VALUES ('dirty', '1')
                    ON CONFLICT(key) DO UPDATE SET value = '1';
                END;
                CREATE TRIGGER IF NOT EXISTS dirty_duel_players_update
                AFTER UPDATE ON duel_players BEGIN
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
                CREATE TRIGGER IF NOT EXISTS dirty_global_profiles_insert
                AFTER INSERT ON global_profiles BEGIN
                    INSERT INTO meta(key, value) VALUES ('dirty', '1')
                    ON CONFLICT(key) DO UPDATE SET value = '1';
                END;
                CREATE TRIGGER IF NOT EXISTS dirty_global_profiles_update
                AFTER UPDATE ON global_profiles BEGIN
                    INSERT INTO meta(key, value) VALUES ('dirty', '1')
                    ON CONFLICT(key) DO UPDATE SET value = '1';
                END;
                CREATE TRIGGER IF NOT EXISTS dirty_global_profiles_delete
                AFTER DELETE ON global_profiles BEGIN
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
            await _migrate_global_profiles(db)
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


async def _migrate_global_profiles(db: aiosqlite.Connection) -> None:
    cursor = await db.execute(
        "SELECT value FROM meta WHERE key = 'global_profiles_migrated'"
    )
    if await cursor.fetchone():
        return
    cursor = await db.execute(
        """
        SELECT user_id, username, gender, callsign, notes
        FROM users
        ORDER BY group_id
        """
    )
    for row in await cursor.fetchall():
        user_id = int(row["user_id"])
        await db.execute(
            "INSERT OR IGNORE INTO global_profiles(user_id) VALUES (?)",
            (user_id,),
        )
        profile_cursor = await db.execute(
            "SELECT username, gender, callsign, notes FROM global_profiles WHERE user_id = ?",
            (user_id,),
        )
        profile = await profile_cursor.fetchone()
        if profile is None:
            continue
        notes = [line for line in str(profile["notes"] or "").splitlines() if line]
        for line in str(row["notes"] or "").splitlines():
            if line and line not in notes:
                notes.append(line)
        username = row["username"] or profile["username"]
        gender = (
            row["gender"]
            if row["gender"] and row["gender"] != "неизвестен"
            else profile["gender"]
        )
        callsign = row["callsign"] or profile["callsign"]
        await db.execute(
            """
            UPDATE global_profiles
            SET username = ?, gender = ?, callsign = ?, notes = ?
            WHERE user_id = ?
            """,
            (username, gender or "неизвестен", callsign, "\n".join(notes), user_id),
        )
    await db.execute(
        "INSERT INTO meta(key, value) VALUES ('global_profiles_migrated', '1')"
    )


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
            "SELECT pollinations_model, width, height, negative_prompt, seed "
            "FROM image_settings WHERE chat_id = ?",
            (chat_id,),
        )
        row = await cursor.fetchone()
    if row is None:
        return {
            "pollinations_model": "flux",
            "width": 768,
            "height": 1024,
            "negative_prompt": "3d, photorealistic, realistic, 3d render, bad anatomy, bad hands",
            "seed": None,
        }
    return dict(row)


async def set_image_setting(chat_id: int, setting: str, value: Any) -> None:
    columns = {
        "pollinations_model": "pollinations_model",
        "negative_prompt": "negative_prompt",
        "seed": "seed",
    }
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


async def ensure_user(
    user_id: int,
    group_id: int,
    username: str | None,
    display_name: str | None = None,
) -> None:
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
            await db.execute(
                """
                INSERT INTO global_profiles(user_id, username, display_name)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    username = COALESCE(excluded.username, global_profiles.username),
                    display_name = COALESCE(excluded.display_name, global_profiles.display_name)
                WHERE
                    (excluded.username IS NOT NULL
                     AND global_profiles.username IS NOT excluded.username)
                    OR (excluded.display_name IS NOT NULL
                        AND global_profiles.display_name IS NOT excluded.display_name)
                """,
                (user_id, username, display_name),
            )
            await db.commit()
        if inserted or username is not None or display_name is not None:
            _record_write()


async def get_user_data(user_id: int, group_id: int) -> dict[str, Any] | None:
    async with _connect() as db:
        cursor = await db.execute(
            """
            SELECT
                users.user_id,
                users.group_id,
                COALESCE(global_profiles.username, users.username) AS username,
                global_profiles.display_name,
                global_profiles.gender,
                global_profiles.callsign,
                global_profiles.age,
                global_profiles.notes
            FROM users
            LEFT JOIN global_profiles ON global_profiles.user_id = users.user_id
            WHERE users.user_id = ? AND users.group_id = ?
            """,
            (user_id, group_id),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def get_global_profile(user_id: int) -> dict[str, Any] | None:
    async with _connect() as db:
        cursor = await db.execute(
            "SELECT * FROM global_profiles WHERE user_id = ?",
            (user_id,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def get_user_group_ids(user_id: int) -> list[int]:
    async with _connect() as db:
        cursor = await db.execute(
            "SELECT group_id FROM users WHERE user_id = ? ORDER BY group_id",
            (user_id,),
        )
        return [int(row["group_id"]) for row in await cursor.fetchall()]


async def get_group_users(group_id: int) -> list[dict[str, Any]]:
    async with _connect() as db:
        cursor = await db.execute(
            """
            SELECT
                users.user_id,
                users.group_id,
                COALESCE(global_profiles.username, users.username) AS username,
                global_profiles.display_name,
                global_profiles.gender,
                global_profiles.callsign,
                global_profiles.age,
                global_profiles.notes
            FROM users
            LEFT JOIN global_profiles ON global_profiles.user_id = users.user_id
            WHERE users.group_id = ?
            ORDER BY COALESCE(
                global_profiles.display_name,
                global_profiles.username,
                CAST(users.user_id AS TEXT)
            )
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
    age: int | None = None,
    note: str | None = None,
) -> bool:
    if gender is None and callsign is None and age is None and note is None:
        return False

    async with _write_lock:
        async with _connect() as db:
            cursor = await db.execute(
                "SELECT notes FROM global_profiles WHERE user_id = ?",
                (user_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                return False
            notes = str(row["notes"] or "")
            note_lines = [line for line in notes.splitlines() if line]
            if note is not None and note not in note_lines:
                note_lines.append(note)
            cursor = await db.execute(
                """
                UPDATE global_profiles
                SET gender = COALESCE(?, gender),
                    callsign = COALESCE(?, callsign),
                    age = COALESCE(?, age),
                    notes = ?
                WHERE user_id = ? AND EXISTS (
                    SELECT 1 FROM users
                    WHERE users.user_id = global_profiles.user_id
                      AND users.group_id = ?
                )
                """,
                (gender, callsign, age, "\n".join(note_lines), user_id, group_id),
            )
            await db.commit()
            updated = cursor.rowcount > 0
        if updated:
            _record_write()
        return updated


async def forget_user(user_id: int, group_id: int) -> bool:
    del group_id
    async with _write_lock:
        async with _connect() as db:
            profile_cursor = await db.execute(
                "DELETE FROM global_profiles WHERE user_id = ?",
                (user_id,),
            )
            membership_cursor = await db.execute(
                "DELETE FROM users WHERE user_id = ?",
                (user_id,),
            )
            await db.commit()
            deleted = profile_cursor.rowcount > 0 or membership_cursor.rowcount > 0
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
        await _archive_messages_if_needed()


async def get_recent_history(chat_id: int, limit: int) -> list[tuple[int, str, int, str]]:
    rows = await _get_message_rows(chat_id, max(1, limit))
    rows.sort(
        key=lambda row: (
            str(row["created_at"]),
            int(row["message_id"]),
            int(row["id"]),
        ),
        reverse=True,
    )
    rows = rows[:max(1, limit)]
    return [
        (int(row["message_id"]), row["sender_name"], int(row["user_id"]), row["text"])
        for row in reversed(rows)
    ]


async def load_chat_history(
    chat_id: int,
    *,
    summary: bool,
    limit: int,
    hours: int = 12,
) -> list[tuple[int, str, int, str]]:
    try:
        if (await get_meta(_CHAT_CONTEXT_ENABLED_KEY)) == "0":
            return []
        if summary:
            return await get_history_for_summary(chat_id, hours=hours)
        return await get_recent_history(chat_id, limit + 1)
    except Exception:
        logging.exception(
            "Не удалось загрузить историю чата %s; продолжаю без контекста.",
            chat_id,
        )
        return []


async def get_recent_bot_message_ids(
    chat_id: int,
    bot_user_id: int,
    limit: int,
) -> list[int]:
    if limit <= 0:
        return []
    rows: list[tuple[int, str, int]] = []
    database_paths = [DATABASE_PATH, *_message_archive_paths()]
    for database_path in database_paths:
        try:
            connection = await aiosqlite.connect(database_path)
            try:
                cursor = await connection.execute(
                    """
                    SELECT message_id, created_at, id
                    FROM messages
                    WHERE chat_id = ? AND user_id = ?
                    ORDER BY message_id DESC, created_at DESC, id DESC
                    LIMIT ?
                    """,
                    (chat_id, bot_user_id, min(limit, 2_147_483_647)),
                )
                rows.extend(
                    (int(row[0]), str(row[1]), int(row[2]))
                    for row in await cursor.fetchall()
                )
            finally:
                await connection.close()
        except (OSError, sqlite3.Error):
            logging.exception(
                "Не удалось найти сообщения бота в истории %s.",
                database_path,
            )
            raise

    rows.sort(key=lambda row: (row[0], row[1], row[2]), reverse=True)
    message_ids: list[int] = []
    seen: set[int] = set()
    for message_id, _created_at, _row_id in rows:
        if message_id in seen:
            continue
        seen.add(message_id)
        message_ids.append(message_id)
        if len(message_ids) >= limit:
            break
    return message_ids


async def delete_message_records(
    chat_id: int,
    user_id: int,
    message_ids: list[int],
) -> int:
    if not message_ids:
        return 0
    placeholders = ",".join("?" for _ in message_ids)
    total_deleted = 0
    changed_archives: list[str] = []
    async with _write_lock:
        for database_path in [DATABASE_PATH, *_message_archive_paths()]:
            async with aiosqlite.connect(database_path) as db:
                cursor = await db.execute(
                    f"""
                    DELETE FROM messages
                    WHERE chat_id = ? AND user_id = ?
                    AND message_id IN ({placeholders})
                    """,
                    (chat_id, user_id, *message_ids),
                )
                total_deleted += cursor.rowcount
                await db.commit()
            if database_path != DATABASE_PATH and cursor.rowcount:
                changed_archives.append(database_path.name)

        if changed_archives:
            async with _connect() as db:
                for filename in changed_archives:
                    await db.executemany(
                        "DELETE FROM meta WHERE key = ?",
                        (
                            (f"message_archive_file_id:{filename}",),
                            (f"message_archive_message_id:{filename}",),
                        ),
                    )
                await db.commit()

        if total_deleted:
            _record_write()
    return total_deleted


async def get_history_for_summary(
    chat_id: int,
    *,
    hours: int = 12,
    limit: int | None = None,
) -> list[tuple[int, str, int, str]]:
    rows = await _get_message_rows(
        chat_id,
        max(1, limit) if limit is not None else None,
        hours=hours,
    )
    rows.sort(
        key=lambda row: (
            str(row["created_at"]),
            int(row["message_id"]),
            int(row["id"]),
        ),
        reverse=True,
    )
    if limit is not None:
        rows = rows[:max(1, limit)]
    return [
        (int(row["message_id"]), row["sender_name"], int(row["user_id"]), row["text"])
        for row in reversed(rows)
    ]


async def _get_message_rows(
    chat_id: int,
    per_database_limit: int | None,
    *,
    hours: int | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    query = (
        "SELECT id, message_id, sender_name, user_id, text, created_at FROM messages "
        "WHERE chat_id = ? "
    )
    params: tuple[object, ...] = (chat_id,)
    if hours is not None:
        query += "AND created_at >= datetime('now', ?) "
        params += (f"-{max(1, hours)} hours",)
    query += "ORDER BY created_at DESC, message_id DESC, id DESC"
    if per_database_limit is not None:
        query += " LIMIT ?"
        params += (per_database_limit,)

    async with _connect() as db:
        cursor = await db.execute(query, params)
        rows.extend(dict(row) for row in await cursor.fetchall())

    for archive_path in _message_archive_paths():
        try:
            archive = await aiosqlite.connect(archive_path)
            archive.row_factory = aiosqlite.Row
            try:
                cursor = await archive.execute(query, params)
                rows.extend(dict(row) for row in await cursor.fetchall())
            finally:
                await archive.close()
        except (OSError, sqlite3.Error):
            logging.exception("Не удалось прочитать архив сообщений %s.", archive_path)
    return rows


def _utc7_date() -> date:
    return datetime.now(timezone(timedelta(hours=7))).date()


async def get_duel_balance(user_id: int, username: str | None) -> int:
    today = _utc7_date()
    today_text = today.isoformat()
    async with _write_lock:
        async with _connect() as db:
            cursor = await db.execute(
                """
                INSERT OR IGNORE INTO duel_players(user_id, username, balance_cents, last_mined_on)
                VALUES (?, ?, 500, ?)
                """,
                (user_id, username, today_text),
            )
            changed = cursor.rowcount > 0
            cursor = await db.execute(
                "SELECT balance_cents, last_mined_on FROM duel_players WHERE user_id = ?",
                (user_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                raise sqlite3.DatabaseError(f"Не удалось создать игровой профиль user_id={user_id}.")
            last_mined = datetime.fromisoformat(str(row["last_mined_on"])).date()
            elapsed_days = max(0, (today - last_mined).days)
            balance = int(row["balance_cents"]) + elapsed_days * 20
            if elapsed_days:
                await db.execute(
                    """
                    UPDATE duel_players
                    SET balance_cents = ?, last_mined_on = ?, username = ?
                    WHERE user_id = ?
                    """,
                    (balance, today_text, username, user_id),
                )
                changed = True
            elif username is not None:
                cursor = await db.execute(
                    "UPDATE duel_players SET username = ? WHERE user_id = ? AND username IS NOT ?",
                    (username, user_id, username),
                )
                changed = changed or cursor.rowcount > 0
            await db.commit()
        if changed:
            _record_write()
    return balance


async def apply_duel_result(user_id: int, username: str | None, delta_cents: int) -> int:
    async with _write_lock:
        async with _connect() as db:
            cursor = await db.execute(
                """
                UPDATE duel_players
                SET balance_cents = MAX(0, balance_cents + ?), username = ?
                WHERE user_id = ?
                """,
                (delta_cents, username, user_id),
            )
            if cursor.rowcount != 1:
                raise sqlite3.DatabaseError(f"Не найден игровой профиль user_id={user_id}.")
            await db.commit()
            cursor = await db.execute(
                "SELECT balance_cents FROM duel_players WHERE user_id = ?",
                (user_id,),
            )
            row = await cursor.fetchone()
        _record_write()
    if row is None:
        raise sqlite3.DatabaseError(f"Не удалось прочитать баланс user_id={user_id}.")
    return int(row["balance_cents"])


async def mine_duel_coins_daily() -> int:
    today = _utc7_date()
    today_text = today.isoformat()
    mined_players = 0
    async with _write_lock:
        async with _connect() as db:
            cursor = await db.execute(
                "SELECT user_id, balance_cents, last_mined_on FROM duel_players"
            )
            players = await cursor.fetchall()
            for player in players:
                last_mined = datetime.fromisoformat(str(player["last_mined_on"])).date()
                elapsed_days = max(0, (today - last_mined).days)
                if elapsed_days == 0:
                    continue
                await db.execute(
                    """
                    UPDATE duel_players
                    SET balance_cents = ?, last_mined_on = ?
                    WHERE user_id = ?
                    """,
                    (
                        int(player["balance_cents"]) + elapsed_days * 20,
                        today_text,
                        int(player["user_id"]),
                    ),
                )
                mined_players += 1
            await db.commit()
        if mined_players:
            _record_write()
    return mined_players


def _message_archive_paths() -> list[Path]:
    return sorted(DATABASE_ARCHIVE_DIR.glob("messages_*.db")) if DATABASE_ARCHIVE_DIR.exists() else []


async def rotate_messages_if_needed() -> bool:
    async with _write_lock:
        rotated = await _archive_messages_if_needed()
        if rotated:
            _record_write()
        return rotated


async def _archive_messages_if_needed() -> bool:
    if not DATABASE_PATH.is_file() or DATABASE_PATH.stat().st_size < DATABASE_SHARD_THRESHOLD_BYTES:
        return False
    DATABASE_ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    archive_paths = _message_archive_paths()
    archive_path = DATABASE_ARCHIVE_DIR / f"messages_{len(archive_paths) + 1:04d}.db"
    if archive_path.exists():
        raise FileExistsError(f"Архив БД уже существует: {archive_path}")
    async with _connect() as db:
        cursor = await db.execute("SELECT COUNT(*) FROM messages")
        message_count = int((await cursor.fetchone())[0])
        if message_count == 0:
            logging.warning(
                "БД достигла порога %.2f МБ, но таблица messages пуста; ротация пропущена.",
                DATABASE_SHARD_THRESHOLD_BYTES / (1024 * 1024),
            )
            return False
        attached = False
        committed = False
        try:
            await db.execute("ATTACH DATABASE ? AS message_archive", (str(archive_path),))
            attached = True
            await db.execute("BEGIN IMMEDIATE")
            await db.execute(
                """
                CREATE TABLE message_archive.messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    sender_name TEXT NOT NULL,
                    text TEXT NOT NULL,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            await db.execute(
                "CREATE INDEX message_archive.idx_messages_chat_id_id "
                "ON messages(chat_id, id)"
            )
            await db.execute(
                """
                INSERT INTO message_archive.messages
                    (id, chat_id, user_id, message_id, sender_name, text, created_at)
                SELECT id, chat_id, user_id, message_id, sender_name, text, created_at
                FROM main.messages
                """
            )
            cursor = await db.execute("SELECT COUNT(*) FROM message_archive.messages")
            archived_count = int((await cursor.fetchone())[0])
            if archived_count != message_count:
                raise sqlite3.DatabaseError(
                    f"Архивировано {archived_count} сообщений вместо {message_count}."
                )
            await db.execute("DELETE FROM main.messages")
            await db.commit()
            committed = True
            await db.execute("DETACH DATABASE message_archive")
            attached = False
            try:
                await db.execute("VACUUM main")
            except sqlite3.Error:
                logging.exception(
                    "Архив %s сохранён, но не удалось уменьшить размер активной БД.",
                    archive_path.name,
                )
            logging.info(
                "Создан архив сообщений %s: %s записей; активная БД освобождена.",
                archive_path.name,
                archived_count,
            )
            return True
        except Exception:
            if not committed:
                await db.rollback()
            if attached:
                await db.execute("DETACH DATABASE message_archive")
            if not committed:
                archive_path.unlink(missing_ok=True)
            raise


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


async def is_chat_context_enabled() -> bool:
    return (await get_meta(_CHAT_CONTEXT_ENABLED_KEY)) != "0"


async def set_chat_context_enabled(enabled: bool) -> None:
    value = "1" if enabled else "0"
    async with _write_lock:
        async with _connect() as db:
            await db.execute(
                """
                INSERT INTO meta(key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (_CHAT_CONTEXT_ENABLED_KEY, value),
            )
            await db.commit()
        _record_write()


def list_message_archives() -> list[Path]:
    return _message_archive_paths()


async def get_message_archive_file_id(filename: str) -> str | None:
    key = f"message_archive_file_id:{Path(filename).name}"
    return await get_meta(key)


async def set_message_archive_backup_meta(
    filename: str,
    file_id: str,
    message_id: int,
) -> None:
    archive_name = Path(filename).name
    if not archive_name.startswith("messages_") or not archive_name.endswith(".db"):
        raise ValueError("Недопустимое имя архива сообщений.")
    async with _write_lock:
        async with _connect() as db:
            await db.executemany(
                "INSERT INTO meta(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (
                    (f"message_archive_file_id:{archive_name}", file_id),
                    (f"message_archive_message_id:{archive_name}", str(message_id)),
                ),
            )
            await db.commit()
        _record_write()


def get_message_archive_manifest(path: str | Path) -> dict[str, str]:
    file_path = Path(path)
    connection = sqlite3.connect(f"file:{file_path.as_posix()}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "SELECT key, value FROM meta WHERE key LIKE 'message_archive_file_id:%'"
        ).fetchall()
    except sqlite3.Error:
        logging.exception("Не удалось прочитать перечень архивов из %s.", file_path)
        raise
    finally:
        connection.close()
    return {
        str(key).removeprefix("message_archive_file_id:"): str(file_id)
        for key, file_id in rows
        if str(key).removeprefix("message_archive_file_id:").startswith("messages_")
    }


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


def has_application_state(path: str | Path) -> bool:
    """Проверяет, содержит ли валидная база сохранённое состояние бота."""
    file_path = Path(path)
    if not validate_sqlite(file_path):
        return False
    try:
        connection = sqlite3.connect(f"file:{file_path.as_posix()}?mode=ro", uri=True)
        try:
            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            if not {"meta", "messages", "users", "duel_players"} <= tables:
                return False
            for table in ("messages", "users", "duel_players", "allowed_groups"):
                if table not in tables:
                    continue
                if connection.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone():
                    return True
            return False
        finally:
            connection.close()
    except sqlite3.Error:
        logging.exception("Не удалось проверить сохранённые данные в %s.", file_path)
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
