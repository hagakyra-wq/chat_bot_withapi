"""Асинхронный доступ к SQLite и создание согласованных копий БД."""

import asyncio
import difflib
import hashlib
import json
import logging
import os
import re
import sqlite3
import time
from datetime import date, datetime, timedelta, timezone
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiosqlite
from cryptography.fernet import Fernet, InvalidToken

from config import (
    ADMIN_ID,
    DATABASE_BACKUP_PATH,
    DATABASE_ARCHIVE_DIR,
    DATABASE_PATH,
    MAX_TOTAL_MESSAGES_PER_CHAT,
    DATABASE_SHARD_THRESHOLD_BYTES,
    HISTORY_ENCRYPTION_KEY,
    MOSCOW_TIMEZONE,
)

_write_lock = asyncio.Lock()
_dirty_generation = 0
_synced_generation = 0
_CHAT_CONTEXT_ENABLED_KEY = "chat_context_enabled"
_AUTORESPONDER_TIMEZONE_KEY = "autoresponder_timezone_offset"
_AUTORESPONDER_NIGHT_ENABLED_KEY = "autoresponder_night_enabled"
_AUTORESPONDER_PRIVATE_ENABLED_KEY = "autoresponder_private_enabled"
_AUTORESPONDER_GROUPS_KEY = "autoresponder_night_groups"
_AUTORESPONDER_DEFAULT_TIMEZONE_OFFSET = 3
_AUTORESPONDER_DEFAULT_NIGHT_TEXT = (
    "Всем сладких снов!!! "
    "(хоть это и автоответчик, я мысленно пожелал вам это <3!)"
)
_AUTORESPONDER_DEFAULT_PRIVATE_TEXT = (
    "Это автоответчик, ожидайте, это не займет слишком много времени "
    "(около 5 лет)"
)
_TELEGRAM_SESSION_BACKUPS_KEY = "telegram_session_backups"
_history_cipher = Fernet(HISTORY_ENCRYPTION_KEY.encode("ascii"))
_MESSAGE_ACTIVITY_BACKFILL_KEY = "message_activity_backfill_v2"
_MOSCOW_TIMESTAMP_MIGRATION_KEY = "moscow_timestamp_migration_v1"
_OWNER_BANNER_USER_ID_META_KEY = "temporary_owner_banner_user_id"
_MOSCOW_TIMESTAMP_COLUMNS: dict[str, tuple[str, ...]] = {
    "messages": ("created_at",),
    "group_promo_codes": ("created_at",),
    "coin_promo_codes": ("created_at", "redeemed_at"),
    "coin_penalty_codes": ("created_at", "redeemed_at"),
    "access_keys": ("created_at",),
    "authorized_users": ("authorized_at",),
    "user_features": ("authorized_at",),
    "banner_items": ("obtained_at",),
    "group_owner_banners": ("created_at",),
    "casino_rounds": ("created_at",),
    "market_lots": ("created_at",),
    "pending_banner_transfers": ("created_at",),
    "bonus_claims": ("claimed_at", "penalized_at"),
}


def _message_activity_period_dates(
    period: str,
    *,
    today: date | None = None,
) -> tuple[str, str]:
    current_date = today or datetime.now(MOSCOW_TIMEZONE).date()
    if period == "d":
        start_date = current_date
    elif period == "w":
        start_date = current_date - timedelta(days=current_date.weekday())
    elif period == "m":
        start_date = current_date.replace(day=1)
    else:
        raise ValueError(f"Неизвестный период статистики сообщений: {period!r}.")
    return start_date.isoformat(), current_date.isoformat()


class HistoryDecryptionError(ValueError):
    """Raised when saved history cannot be opened with the configured key."""


def _autoresponder_meta_key(key: str, user_id: int | None) -> str:
    if user_id is None or user_id == ADMIN_ID:
        return key
    return f"{key}:{user_id}"


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
                    is_bot INTEGER NOT NULL DEFAULT 0,
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
                    created_at TIMESTAMP NOT NULL DEFAULT (datetime('now', '+3 hours')),
                    payload_version INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_messages_chat_id_id
                    ON messages(chat_id, id);
                CREATE TABLE IF NOT EXISTS message_activity (
                    chat_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    activity_date TEXT NOT NULL,
                    message_count INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (chat_id, user_id, activity_date)
                );
                CREATE INDEX IF NOT EXISTS idx_message_activity_user_date
                    ON message_activity(user_id, activity_date);
                CREATE INDEX IF NOT EXISTS idx_message_activity_chat_date
                    ON message_activity(chat_id, activity_date);
                CREATE TABLE IF NOT EXISTS allowed_groups (
                    group_id INTEGER PRIMARY KEY
                );
                CREATE TABLE IF NOT EXISTS user_group_memberships (
                    group_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    PRIMARY KEY (group_id, user_id)
                );
                CREATE TABLE IF NOT EXISTS group_promo_codes (
                    code TEXT PRIMARY KEY,
                    created_by INTEGER NOT NULL,
                    created_at TIMESTAMP NOT NULL DEFAULT (datetime('now', '+3 hours'))
                );
                CREATE TABLE IF NOT EXISTS coin_promo_codes (
                    code TEXT PRIMARY KEY,
                    amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
                    created_by INTEGER NOT NULL,
                    created_at TIMESTAMP NOT NULL DEFAULT (datetime('now', '+3 hours')),
                    redeemed_by INTEGER,
                    redeemed_at TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS coin_penalty_codes (
                    code TEXT PRIMARY KEY,
                    amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
                    created_by INTEGER NOT NULL,
                    created_at TIMESTAMP NOT NULL DEFAULT (datetime('now', '+3 hours')),
                    redeemed_by INTEGER,
                    redeemed_at TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS access_keys (
                    key TEXT PRIMARY KEY,
                    created_at TIMESTAMP NOT NULL DEFAULT (datetime('now', '+3 hours')),
                    access_level TEXT NOT NULL DEFAULT 'user',
                    duration_seconds INTEGER,
                    created_by INTEGER
                );
                CREATE TABLE IF NOT EXISTS authorized_users (
                    user_id INTEGER PRIMARY KEY,
                    authorized_at TIMESTAMP NOT NULL DEFAULT (datetime('now', '+3 hours')),
                    username TEXT,
                    access_level TEXT NOT NULL DEFAULT 'user',
                    expires_at REAL
                );
                CREATE TABLE IF NOT EXISTS user_features (
                    user_id INTEGER NOT NULL,
                    feature TEXT NOT NULL,
                    authorized_at TIMESTAMP NOT NULL DEFAULT (datetime('now', '+3 hours')),
                    expires_at REAL,
                    PRIMARY KEY (user_id, feature)
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
                    last_mined_on TEXT NOT NULL,
                    is_bot INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS banner_items (
                    item_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    level INTEGER NOT NULL CHECK(level BETWEEN 1 AND 6),
                    seed TEXT NOT NULL,
                    preset TEXT,
                    source TEXT NOT NULL,
                    obtained_at TIMESTAMP NOT NULL DEFAULT (datetime('now', '+3 hours')),
                    file_id TEXT,
                    backup_msg_id INTEGER,
                    content_hash TEXT,
                    rendered_at REAL,
                    is_active INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_banner_items_user
                    ON banner_items(user_id, obtained_at DESC, item_id DESC);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_banner_items_active_user
                    ON banner_items(user_id) WHERE is_active = 1;
                CREATE TABLE IF NOT EXISTS group_owner_banners (
                    group_id INTEGER PRIMARY KEY,
                    owner_user_id INTEGER NOT NULL,
                    seed TEXT NOT NULL,
                    preset TEXT NOT NULL,
                    file_id TEXT,
                    content_hash TEXT,
                    backup_msg_id INTEGER,
                    created_at TIMESTAMP NOT NULL DEFAULT (datetime('now', '+3 hours'))
                );
                CREATE TRIGGER IF NOT EXISTS dirty_group_owner_banners_insert
                AFTER INSERT ON group_owner_banners BEGIN
                    INSERT INTO meta(key, value) VALUES ('dirty', '1')
                    ON CONFLICT(key) DO UPDATE SET value = '1';
                END;
                CREATE TRIGGER IF NOT EXISTS dirty_group_owner_banners_update
                AFTER UPDATE ON group_owner_banners BEGIN
                    INSERT INTO meta(key, value) VALUES ('dirty', '1')
                    ON CONFLICT(key) DO UPDATE SET value = '1';
                END;
                CREATE TRIGGER IF NOT EXISTS dirty_group_owner_banners_delete
                AFTER DELETE ON group_owner_banners BEGIN
                    INSERT INTO meta(key, value) VALUES ('dirty', '1')
                    ON CONFLICT(key) DO UPDATE SET value = '1';
                END;
                CREATE TABLE IF NOT EXISTS casino_rounds (
                    round_id TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    cost_cents INTEGER NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('pending', 'completed', 'failed')),
                    results TEXT,
                    created_at TIMESTAMP NOT NULL DEFAULT (datetime('now', '+3 hours'))
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_casino_rounds_pending_user
                    ON casino_rounds(user_id) WHERE status = 'pending';
                CREATE TABLE IF NOT EXISTS market_lots (
                    lot_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL UNIQUE,
                    seller_id INTEGER NOT NULL,
                    price_cents INTEGER NOT NULL CHECK(price_cents > 0),
                    title TEXT,
                    created_at TIMESTAMP NOT NULL DEFAULT (datetime('now', '+3 hours')),
                    status TEXT NOT NULL CHECK(status IN ('open', 'sold', 'withdrawn'))
                );
                CREATE INDEX IF NOT EXISTS idx_market_lots_open
                    ON market_lots(status, created_at DESC);
                CREATE TABLE IF NOT EXISTS pending_banner_transfers (
                    item_id INTEGER PRIMARY KEY,
                    sender_id INTEGER NOT NULL,
                    recipient_id INTEGER NOT NULL,
                    created_at TIMESTAMP NOT NULL DEFAULT (datetime('now', '+3 hours'))
                );
                CREATE INDEX IF NOT EXISTS idx_pending_banner_transfers_recipient
                    ON pending_banner_transfers(recipient_id, created_at);
                CREATE TABLE IF NOT EXISTS bonus_claims (
                    claim_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    claimed_at TIMESTAMP NOT NULL DEFAULT (datetime('now', '+3 hours')),
                    status TEXT NOT NULL CHECK(status IN ('subscribed', 'unsubscribed')),
                    penalized_at TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_bonus_claims_user
                    ON bonus_claims(user_id, claim_id DESC);
                CREATE TRIGGER IF NOT EXISTS dirty_banner_items_insert
                AFTER INSERT ON banner_items BEGIN
                    INSERT INTO meta(key, value) VALUES ('dirty', '1')
                    ON CONFLICT(key) DO UPDATE SET value = '1';
                END;
                CREATE TRIGGER IF NOT EXISTS dirty_banner_items_update
                AFTER UPDATE ON banner_items BEGIN
                    INSERT INTO meta(key, value) VALUES ('dirty', '1')
                    ON CONFLICT(key) DO UPDATE SET value = '1';
                END;
                CREATE TRIGGER IF NOT EXISTS dirty_banner_items_delete
                AFTER DELETE ON banner_items BEGIN
                    INSERT INTO meta(key, value) VALUES ('dirty', '1')
                    ON CONFLICT(key) DO UPDATE SET value = '1';
                END;
                CREATE TRIGGER IF NOT EXISTS dirty_casino_rounds_insert
                AFTER INSERT ON casino_rounds BEGIN
                    INSERT INTO meta(key, value) VALUES ('dirty', '1')
                    ON CONFLICT(key) DO UPDATE SET value = '1';
                END;
                CREATE TRIGGER IF NOT EXISTS dirty_casino_rounds_update
                AFTER UPDATE ON casino_rounds BEGIN
                    INSERT INTO meta(key, value) VALUES ('dirty', '1')
                    ON CONFLICT(key) DO UPDATE SET value = '1';
                END;
                CREATE TRIGGER IF NOT EXISTS dirty_market_lots_insert
                AFTER INSERT ON market_lots BEGIN
                    INSERT INTO meta(key, value) VALUES ('dirty', '1')
                    ON CONFLICT(key) DO UPDATE SET value = '1';
                END;
                CREATE TRIGGER IF NOT EXISTS dirty_market_lots_update
                AFTER UPDATE ON market_lots BEGIN
                    INSERT INTO meta(key, value) VALUES ('dirty', '1')
                    ON CONFLICT(key) DO UPDATE SET value = '1';
                END;
                CREATE TRIGGER IF NOT EXISTS dirty_pending_banner_transfers_insert
                AFTER INSERT ON pending_banner_transfers BEGIN
                    INSERT INTO meta(key, value) VALUES ('dirty', '1')
                    ON CONFLICT(key) DO UPDATE SET value = '1';
                END;
                CREATE TRIGGER IF NOT EXISTS dirty_pending_banner_transfers_delete
                AFTER DELETE ON pending_banner_transfers BEGIN
                    INSERT INTO meta(key, value) VALUES ('dirty', '1')
                    ON CONFLICT(key) DO UPDATE SET value = '1';
                END;
                CREATE TRIGGER IF NOT EXISTS dirty_bonus_claims_insert
                AFTER INSERT ON bonus_claims BEGIN
                    INSERT INTO meta(key, value) VALUES ('dirty', '1')
                    ON CONFLICT(key) DO UPDATE SET value = '1';
                END;
                CREATE TRIGGER IF NOT EXISTS dirty_bonus_claims_update
                AFTER UPDATE ON bonus_claims BEGIN
                    INSERT INTO meta(key, value) VALUES ('dirty', '1')
                    ON CONFLICT(key) DO UPDATE SET value = '1';
                END;
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
                CREATE TRIGGER IF NOT EXISTS dirty_user_group_memberships_insert
                AFTER INSERT ON user_group_memberships BEGIN
                    INSERT INTO meta(key, value) VALUES ('dirty', '1')
                    ON CONFLICT(key) DO UPDATE SET value = '1';
                END;
                CREATE TRIGGER IF NOT EXISTS dirty_user_group_memberships_delete
                AFTER DELETE ON user_group_memberships BEGIN
                    INSERT INTO meta(key, value) VALUES ('dirty', '1')
                    ON CONFLICT(key) DO UPDATE SET value = '1';
                END;
                CREATE TRIGGER IF NOT EXISTS dirty_group_promo_codes_insert
                AFTER INSERT ON group_promo_codes BEGIN
                    INSERT INTO meta(key, value) VALUES ('dirty', '1')
                    ON CONFLICT(key) DO UPDATE SET value = '1';
                END;
                CREATE TRIGGER IF NOT EXISTS dirty_group_promo_codes_delete
                AFTER DELETE ON group_promo_codes BEGIN
                    INSERT INTO meta(key, value) VALUES ('dirty', '1')
                    ON CONFLICT(key) DO UPDATE SET value = '1';
                END;
                CREATE TRIGGER IF NOT EXISTS dirty_coin_promo_codes_insert
                AFTER INSERT ON coin_promo_codes BEGIN
                    INSERT INTO meta(key, value) VALUES ('dirty', '1')
                    ON CONFLICT(key) DO UPDATE SET value = '1';
                END;
                CREATE TRIGGER IF NOT EXISTS dirty_coin_promo_codes_update
                AFTER UPDATE ON coin_promo_codes BEGIN
                    INSERT INTO meta(key, value) VALUES ('dirty', '1')
                    ON CONFLICT(key) DO UPDATE SET value = '1';
                END;
                CREATE TRIGGER IF NOT EXISTS dirty_coin_penalty_codes_insert
                AFTER INSERT ON coin_penalty_codes BEGIN
                    INSERT INTO meta(key, value) VALUES ('dirty', '1')
                    ON CONFLICT(key) DO UPDATE SET value = '1';
                END;
                CREATE TRIGGER IF NOT EXISTS dirty_coin_penalty_codes_update
                AFTER UPDATE ON coin_penalty_codes BEGIN
                    INSERT INTO meta(key, value) VALUES ('dirty', '1')
                    ON CONFLICT(key) DO UPDATE SET value = '1';
                END;
                CREATE TRIGGER IF NOT EXISTS dirty_coin_penalty_codes_delete
                AFTER DELETE ON coin_penalty_codes BEGIN
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
                CREATE TRIGGER IF NOT EXISTS dirty_user_features_insert
                AFTER INSERT ON user_features BEGIN
                    INSERT INTO meta(key, value) VALUES ('dirty', '1')
                    ON CONFLICT(key) DO UPDATE SET value = '1';
                END;
                CREATE TRIGGER IF NOT EXISTS dirty_user_features_update
                AFTER UPDATE ON user_features BEGIN
                    INSERT INTO meta(key, value) VALUES ('dirty', '1')
                    ON CONFLICT(key) DO UPDATE SET value = '1';
                END;
                CREATE TRIGGER IF NOT EXISTS dirty_user_features_delete
                AFTER DELETE ON user_features BEGIN
                    INSERT INTO meta(key, value) VALUES ('dirty', '1')
                    ON CONFLICT(key) DO UPDATE SET value = '1';
                END;
                """
            )
            await db.execute(
                "UPDATE banner_items SET is_active = 0 "
                "WHERE source = 'owner' AND is_active = 1 "
                "AND user_id != COALESCE("
                "(SELECT CAST(value AS INTEGER) FROM meta WHERE key = ?), -1"
                ")",
                (_OWNER_BANNER_USER_ID_META_KEY,),
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
            await _ensure_columns(db, "users", {"is_bot": "INTEGER NOT NULL DEFAULT 0"})
            await _ensure_columns(
                db,
                "duel_players",
                {"is_bot": "INTEGER NOT NULL DEFAULT 0"},
            )
            await _ensure_columns(
                db,
                "messages",
                {"payload_version": "INTEGER NOT NULL DEFAULT 0"},
            )
            await _ensure_columns(
                db,
                "banner_items",
                {
                    "market_file_id": "TEXT",
                    "market_backup_msg_id": "INTEGER",
                },
            )
            cursor = await db.execute("PRAGMA table_info(market_lots)")
            had_market_lot_title = any(
                str(row["name"]) == "title" for row in await cursor.fetchall()
            )
            await _ensure_columns(db, "market_lots", {"title": "TEXT"})
            if not had_market_lot_title:
                await db.execute(
                    "INSERT INTO meta(key, value) VALUES ('dirty', '1') "
                    "ON CONFLICT(key) DO UPDATE SET value = '1'"
                )
            await _migrate_global_profiles(db)
            await _migrate_message_payloads(db)
            await db.commit()
            cursor = await db.execute("SELECT value FROM meta WHERE key = 'dirty'")
            dirty = await cursor.fetchone()
    _dirty_generation = 1 if dirty and dirty["value"] == "1" else 0
    _synced_generation = 0
    logging.info("Схема SQLite готова: %s", DATABASE_PATH)
    await _migrate_timestamps_to_moscow()
    await _backfill_message_activity()


def _to_moscow_storage_timestamp(value: str) -> str:
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise sqlite3.DatabaseError(
            f"Некорректная временная метка в базе: {value!r}."
        ) from exc
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return timestamp.astimezone(MOSCOW_TIMEZONE).replace(tzinfo=None).isoformat(sep=" ")


async def _ensure_moscow_timestamp_triggers(
    db: aiosqlite.Connection,
) -> None:
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    )
    tables = {str(row[0]) for row in await cursor.fetchall()}
    for table, columns in _MOSCOW_TIMESTAMP_COLUMNS.items():
        if table not in tables:
            continue
        cursor = await db.execute(f'PRAGMA table_info("{table}")')
        existing_columns = {str(row[1]) for row in await cursor.fetchall()}
        for column in columns:
            if column not in existing_columns:
                continue
            trigger = f"moscow_default_{table}_{column}"
            await db.execute(
                f"""
                CREATE TRIGGER IF NOT EXISTS "{trigger}"
                AFTER INSERT ON "{table}"
                WHEN NEW."{column}" = CURRENT_TIMESTAMP
                BEGIN
                    UPDATE "{table}"
                    SET "{column}" = datetime(NEW."{column}", '+3 hours')
                    WHERE rowid = NEW.rowid;
                END
                """
            )


async def _migrate_timestamps_to_moscow() -> None:
    async with _write_lock:
        async with _connect() as main_db:
            cursor = await main_db.execute(
                "SELECT value FROM meta WHERE key = ?",
                (_MOSCOW_TIMESTAMP_MIGRATION_KEY,),
            )
            if await cursor.fetchone() is not None:
                await _ensure_moscow_timestamp_triggers(main_db)
                await main_db.commit()
                return

            invalidated_archives: list[str] = []
            for database_path in [DATABASE_PATH, *_message_archive_paths()]:
                if not database_path.is_file():
                    continue
                async with aiosqlite.connect(database_path) as target:
                    for table, columns in _MOSCOW_TIMESTAMP_COLUMNS.items():
                        cursor = await target.execute(
                            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                            (table,),
                        )
                        if await cursor.fetchone() is None:
                            continue
                        cursor = await target.execute(f'PRAGMA table_info("{table}")')
                        existing_columns = {
                            str(row[1]) for row in await cursor.fetchall()
                        }
                        for column in columns:
                            if column not in existing_columns:
                                continue
                            cursor = await target.execute(
                                f'SELECT rowid, "{column}" FROM "{table}" '
                                f'WHERE "{column}" IS NOT NULL'
                            )
                            rows = await cursor.fetchall()
                            updates = [
                                (
                                    _to_moscow_storage_timestamp(str(row[1])),
                                    int(row[0]),
                                )
                                for row in rows
                            ]
                            if updates:
                                await target.executemany(
                                    f'UPDATE "{table}" SET "{column}" = ? WHERE rowid = ?',
                                    updates,
                                )
                                if database_path != DATABASE_PATH:
                                    invalidated_archives.append(database_path.name)
                    await _ensure_moscow_timestamp_triggers(target)
                    await target.commit()

            for archive_name in set(invalidated_archives):
                await main_db.execute(
                    "DELETE FROM meta WHERE key IN (?, ?)",
                    (
                        f"message_archive_file_id:{archive_name}",
                        f"message_archive_message_id:{archive_name}",
                    ),
                )
            await main_db.execute(
                """
                UPDATE meta SET value = '3'
                WHERE key LIKE 'autoresponder_timezone_offset%'
                  AND value = '5'
                """
            )
            await main_db.execute(
                "INSERT INTO meta(key, value) VALUES (?, '1')",
                (_MOSCOW_TIMESTAMP_MIGRATION_KEY,),
            )
            await main_db.commit()
        _record_write()


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


def _encrypt_message_payload(
    sender_name: str,
    text: str,
    *,
    gender: str = "неизвестен",
    age: int | None = None,
    reactions: list[dict[str, object]] | None = None,
    reply_to: str | None = None,
) -> str:
    payload = {
        "sender_name": sender_name,
        "text": text,
        "gender": gender,
        "age": age,
        "reactions": reactions or [],
        "reply_to": reply_to,
    }
    return _history_cipher.encrypt(
        json.dumps(payload, ensure_ascii=False).encode("utf-8")
    ).decode("ascii")


def _decrypt_message_payload(token: str) -> dict[str, Any]:
    try:
        payload = json.loads(_history_cipher.decrypt(token.encode("ascii")))
    except (InvalidToken, UnicodeDecodeError, UnicodeEncodeError, json.JSONDecodeError) as exc:
        raise HistoryDecryptionError(
            "Не удалось расшифровать историю. Проверьте HISTORY_ENCRYPTION_KEY."
        ) from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("text"), str):
        raise HistoryDecryptionError("Зашифрованная запись истории имеет неверный формат.")
    return payload


async def _migrate_message_payloads(db: aiosqlite.Connection) -> None:
    migrated = await _encrypt_legacy_messages(db)
    for archive_path in _message_archive_paths():
        try:
            async with aiosqlite.connect(archive_path) as archive:
                await _ensure_columns(
                    archive,
                    "messages",
                    {"payload_version": "INTEGER NOT NULL DEFAULT 0"},
                )
                archive_migrated = await _encrypt_legacy_messages(archive)
                await archive.commit()
            if archive_migrated:
                await db.execute(
                    "DELETE FROM meta WHERE key = ?",
                    (f"message_archive_file_id:{archive_path.name}",),
                )
                migrated += archive_migrated
        except (OSError, sqlite3.Error):
            logging.exception(
                "Не удалось подготовить шифрование архива истории %s.",
                archive_path,
            )
            raise
    if migrated:
        await db.execute(
            """
            INSERT INTO meta(key, value) VALUES ('dirty', '1')
            ON CONFLICT(key) DO UPDATE SET value = '1'
            """
        )


async def _encrypt_legacy_messages(db: aiosqlite.Connection) -> int:
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        """
        SELECT id, sender_name, text
        FROM messages
        WHERE payload_version = 0
        """
    )
    rows = await cursor.fetchall()
    for row in rows:
        encrypted_text = _encrypt_message_payload(
            str(row["sender_name"]),
            str(row["text"]),
        )
        await db.execute(
            """
            UPDATE messages
            SET sender_name = '[encrypted]', text = ?, payload_version = 1
            WHERE id = ?
            """,
            (encrypted_text, int(row["id"])),
        )
    return len(rows)


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


async def set_group_membership(
    group_id: int,
    user_id: int,
    *,
    is_member: bool,
) -> bool:
    async with _write_lock:
        async with _connect() as db:
            if is_member:
                cursor = await db.execute(
                    "INSERT OR IGNORE INTO user_group_memberships(group_id, user_id) "
                    "VALUES (?, ?)",
                    (group_id, user_id),
                )
            else:
                cursor = await db.execute(
                    "DELETE FROM user_group_memberships WHERE group_id = ? AND user_id = ?",
                    (group_id, user_id),
                )
            await db.commit()
        if cursor.rowcount:
            _record_write()
    return cursor.rowcount == 1


async def list_user_group_memberships(user_id: int) -> list[int]:
    async with _connect() as db:
        cursor = await db.execute(
            "SELECT group_id FROM user_group_memberships WHERE user_id = ? ORDER BY group_id",
            (user_id,),
        )
        rows = await cursor.fetchall()
        return [int(row["group_id"]) for row in rows]


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


async def create_group_promo_code(code: str, created_by: int) -> bool:
    normalized = code.strip().upper()
    if not re.fullmatch(r"YUBARA-[A-F0-9]{12}", normalized):
        raise ValueError("Промокод группы имеет неверный формат.")
    async with _write_lock:
        async with _connect() as db:
            try:
                await db.execute(
                    "INSERT INTO group_promo_codes(code, created_by) VALUES (?, ?)",
                    (normalized, created_by),
                )
            except aiosqlite.IntegrityError:
                await db.rollback()
                return False
            await db.commit()
        _record_write()
        return True


async def redeem_group_promo_code(code: str, group_id: int) -> str:
    normalized = code.strip().upper()
    if not re.fullmatch(r"YUBARA-[A-F0-9]{12}", normalized):
        return "invalid"
    async with _write_lock:
        async with _connect() as db:
            cursor = await db.execute(
                "SELECT 1 FROM allowed_groups WHERE group_id = ?",
                (group_id,),
            )
            if await cursor.fetchone() is not None:
                return "already_allowed"
            cursor = await db.execute(
                "SELECT 1 FROM group_promo_codes WHERE code = ?",
                (normalized,),
            )
            if await cursor.fetchone() is None:
                return "invalid"
            cursor = await db.execute(
                "DELETE FROM group_promo_codes WHERE code = ?",
                (normalized,),
            )
            if cursor.rowcount != 1:
                await db.rollback()
                return "invalid"
            await db.execute(
                "INSERT INTO allowed_groups(group_id) VALUES (?)",
                (group_id,),
            )
            await db.commit()
        _record_write()
        return "activated"


async def create_coin_promo_code(
    code: str,
    amount_cents: int,
    created_by: int,
) -> bool:
    normalized = code.strip().upper()
    if not re.fullmatch(r"YUBARA-COIN-[A-F0-9]{12}", normalized):
        raise ValueError("Промокод монет имеет неверный формат.")
    if not 1 <= amount_cents <= 10_000_000:
        raise ValueError("Сумма промокода должна быть от 1 копейки до 100 000 монет.")
    async with _write_lock:
        async with _connect() as db:
            try:
                await db.execute(
                    "INSERT INTO coin_promo_codes(code, amount_cents, created_by) "
                    "VALUES (?, ?, ?)",
                    (normalized, amount_cents, created_by),
                )
            except aiosqlite.IntegrityError:
                await db.rollback()
                return False
            await db.commit()
        _record_write()
    return True


async def redeem_coin_promo_code(
    code: str,
    user_id: int,
    username: str | None,
) -> tuple[str, int | None, int | None]:
    normalized = code.strip().upper()
    if not re.fullmatch(r"YUBARA-COIN-[A-F0-9]{12}", normalized):
        return "invalid", None, None
    today_text = _moscow_date().isoformat()
    async with _write_lock:
        async with _connect() as db:
            await db.execute("BEGIN")
            cursor = await db.execute(
                "SELECT amount_cents, redeemed_by FROM coin_promo_codes WHERE code = ?",
                (normalized,),
            )
            promo = await cursor.fetchone()
            if promo is None:
                await db.rollback()
                return "invalid", None, None
            if promo["redeemed_by"] is not None:
                await db.rollback()
                return "used", None, None
            cursor = await db.execute(
                "UPDATE coin_promo_codes SET redeemed_by = ?, "
                "redeemed_at = datetime('now', '+3 hours') "
                "WHERE code = ? AND redeemed_by IS NULL",
                (user_id, normalized),
            )
            if cursor.rowcount != 1:
                await db.rollback()
                return "used", None, None
            await db.execute(
                "INSERT OR IGNORE INTO duel_players("
                "user_id, username, balance_cents, last_mined_on, is_bot"
                ") VALUES (?, ?, 500, ?, 0)",
                (user_id, username, today_text),
            )
            await db.execute(
                "UPDATE duel_players SET username = COALESCE(?, username), "
                "balance_cents = balance_cents + ? WHERE user_id = ?",
                (username, int(promo["amount_cents"]), user_id),
            )
            cursor = await db.execute(
                "SELECT balance_cents FROM duel_players WHERE user_id = ?",
                (user_id,),
            )
            balance_row = await cursor.fetchone()
            if balance_row is None:
                await db.rollback()
                raise sqlite3.DatabaseError(
                    f"Не удалось начислить монеты user_id={user_id}."
                )
            balance = int(balance_row["balance_cents"])
            await db.commit()
        _record_write()
    return "redeemed", int(promo["amount_cents"]), balance


async def create_coin_penalty_code(
    code: str,
    amount_cents: int,
    created_by: int,
) -> bool:
    normalized = code.strip().upper()
    if not re.fullmatch(r"YUBARA-PEN-[A-F0-9]{12}", normalized):
        raise ValueError("Штрафной ключ имеет неверный формат.")
    if not 1 <= amount_cents <= 9_223_372_036_854_775_807:
        raise ValueError("Сумма штрафа должна быть положительным числом.")
    async with _write_lock:
        async with _connect() as db:
            try:
                await db.execute(
                    "INSERT INTO coin_penalty_codes(code, amount_cents, created_by) "
                    "VALUES (?, ?, ?)",
                    (normalized, amount_cents, created_by),
                )
            except aiosqlite.IntegrityError:
                await db.rollback()
                return False
            await db.commit()
        _record_write()
    return True


async def list_coin_penalty_codes() -> list[dict[str, Any]]:
    async with _connect() as db:
        cursor = await db.execute(
            """
            SELECT code, amount_cents, created_by, redeemed_by
            FROM coin_penalty_codes ORDER BY created_at, code
            """
        )
        return [dict(row) for row in await cursor.fetchall()]


async def revoke_coin_penalty_code(code: str) -> bool:
    normalized = code.strip().upper()
    async with _write_lock:
        async with _connect() as db:
            cursor = await db.execute(
                "DELETE FROM coin_penalty_codes WHERE code = ? AND redeemed_by IS NULL",
                (normalized,),
            )
            await db.commit()
            deleted = cursor.rowcount > 0
        if deleted:
            _record_write()
        return deleted


async def redeem_coin_penalty_code(
    code: str,
    user_id: int,
    username: str | None,
) -> tuple[str, int | None, int | None]:
    normalized = code.strip().upper()
    if not re.fullmatch(r"YUBARA-PEN-[A-F0-9]{12}", normalized):
        return "invalid", None, None
    today_text = _moscow_date().isoformat()
    async with _write_lock:
        async with _connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "SELECT amount_cents, redeemed_by FROM coin_penalty_codes WHERE code = ?",
                (normalized,),
            )
            penalty = await cursor.fetchone()
            if penalty is None:
                await db.rollback()
                return "invalid", None, None
            if penalty["redeemed_by"] is not None:
                await db.rollback()
                return "used", None, None
            await db.execute(
                "INSERT OR IGNORE INTO duel_players("
                "user_id, username, balance_cents, last_mined_on, is_bot"
                ") VALUES (?, ?, 0, ?, 0)",
                (user_id, username, today_text),
            )
            amount_cents = int(penalty["amount_cents"])
            cursor = await db.execute(
                "UPDATE duel_players SET username = COALESCE(?, username), "
                "balance_cents = balance_cents - ? WHERE user_id = ?",
                (username, amount_cents, user_id),
            )
            if cursor.rowcount != 1:
                await db.rollback()
                raise sqlite3.DatabaseError(
                    f"Не удалось списать штраф с user_id={user_id}."
                )
            cursor = await db.execute(
                "UPDATE coin_penalty_codes SET redeemed_by = ?, "
                "redeemed_at = datetime('now', '+3 hours') "
                "WHERE code = ? AND redeemed_by IS NULL",
                (user_id, normalized),
            )
            if cursor.rowcount != 1:
                await db.rollback()
                return "used", None, None
            cursor = await db.execute(
                "SELECT balance_cents FROM duel_players WHERE user_id = ?",
                (user_id,),
            )
            balance_row = await cursor.fetchone()
            if balance_row is None:
                await db.rollback()
                raise sqlite3.DatabaseError(
                    f"Не удалось прочитать баланс user_id={user_id} после штрафа."
                )
            balance = int(balance_row["balance_cents"])
            await db.commit()
        _record_write()
    return "redeemed", amount_cents, balance


async def find_user_id_by_username(username: str) -> int | None:
    normalized = username.strip().lstrip("@").casefold()
    if not normalized:
        return None
    async with _connect() as db:
        cursor = await db.execute(
            "SELECT user_id FROM global_profiles WHERE lower(username) = ?",
            (normalized,),
        )
        row = await cursor.fetchone()
    return int(row["user_id"]) if row is not None else None


async def ensure_user(
    user_id: int,
    group_id: int,
    username: str | None,
    display_name: str | None = None,
    is_bot: bool = False,
) -> None:
    async with _write_lock:
        async with _connect() as db:
            cursor = await db.execute(
                """
                INSERT OR IGNORE INTO users(user_id, group_id, username, is_bot)
                VALUES (?, ?, ?, ?)
                """,
                (user_id, group_id, username, int(is_bot)),
            )
            inserted = cursor.rowcount > 0
            if is_bot:
                update = await db.execute(
                    """
                    UPDATE users SET is_bot = 1
                    WHERE user_id = ? AND group_id = ? AND is_bot = 0
                    """,
                    (user_id, group_id),
                )
                inserted = inserted or update.rowcount > 0
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
                users.is_bot,
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


async def get_or_create_profile_decoration(user_id: int) -> str:
    key = f"profile_decoration:{user_id}"
    decoration = await get_meta(key)
    if decoration is not None:
        return decoration
    decoration = hashlib.sha256(f"yubara-profile:{user_id}".encode("ascii")).hexdigest()
    await set_meta(key, decoration)
    return decoration


async def get_profile_banner_records() -> list[dict[str, Any]]:
    async with _connect() as db:
        cursor = await db.execute(
            """
            SELECT user_id, username, display_name, callsign
            FROM global_profiles
            ORDER BY user_id
            """
        )
        profiles = [dict(row) for row in await cursor.fetchall()]
    banners: list[dict[str, Any]] = []
    for profile in profiles:
        user_id = int(profile["user_id"])
        nickname = str(
            profile.get("callsign")
            or profile.get("display_name")
            or profile.get("username")
            or f"Пользователь {user_id}"
        )
        banners.append(
            {
                "user_id": user_id,
                "nickname": nickname,
                "decoration": await get_or_create_profile_decoration(user_id),
            }
        )
    return banners


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


async def get_group_user_genders(group_id: int) -> dict[int, str]:
    async with _connect() as db:
        cursor = await db.execute(
            """
            SELECT users.user_id, global_profiles.gender
            FROM users
            LEFT JOIN global_profiles ON global_profiles.user_id = users.user_id
            WHERE users.group_id = ?
            """,
            (group_id,),
        )
        return {
            int(row["user_id"]): str(row["gender"] or "неизвестен")
            for row in await cursor.fetchall()
        }


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
            activity_cursor = await db.execute(
                "DELETE FROM message_activity WHERE user_id = ?",
                (user_id,),
            )
            await db.commit()
            deleted = (
                profile_cursor.rowcount > 0
                or membership_cursor.rowcount > 0
                or activity_cursor.rowcount > 0
            )
        if deleted:
            _record_write()
        return deleted


async def save_message(
    chat_id: int,
    user_id: int,
    message_id: int,
    sender_name: str,
    text: str,
    *,
    gender: str = "неизвестен",
    age: int | None = None,
    reactions: list[dict[str, object]] | None = None,
    reply_to: str | None = None,
) -> None:
    if chat_id > 0:
        return
    encrypted_text = _encrypt_message_payload(
        sender_name,
        text,
        gender=gender,
        age=age,
        reactions=reactions,
        reply_to=reply_to,
    )
    async with _write_lock:
        async with _connect() as db:
            await db.execute(
                """
                INSERT INTO messages(
                    chat_id, user_id, message_id, sender_name, text, payload_version
                )
                VALUES (?, ?, ?, '[encrypted]', ?, 1)
                """,
                (chat_id, user_id, message_id, encrypted_text),
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


async def record_group_message_activity(chat_id: int, user_id: int) -> None:
    if chat_id >= 0:
        return
    activity_date = datetime.now(MOSCOW_TIMEZONE).date().isoformat()
    async with _write_lock:
        async with _connect() as db:
            await db.execute(
                """
                INSERT INTO message_activity(chat_id, user_id, activity_date, message_count)
                VALUES (?, ?, ?, 1)
                ON CONFLICT(chat_id, user_id, activity_date)
                DO UPDATE SET message_count = message_count + 1
                """,
                (chat_id, user_id, activity_date),
            )
            await db.commit()
        _record_write()


async def get_user_message_activity(user_id: int) -> dict[str, int]:
    today = datetime.now(MOSCOW_TIMEZONE).date()
    day_start, _ = _message_activity_period_dates("d", today=today)
    week_start, _ = _message_activity_period_dates("w", today=today)
    month_start, _ = _message_activity_period_dates("m", today=today)
    async with _connect() as db:
        cursor = await db.execute(
            """
            SELECT
                COALESCE(SUM(CASE
                    WHEN activity_date BETWEEN ? AND ? THEN message_count
                    ELSE 0
                END), 0) AS day_count,
                COALESCE(SUM(CASE
                    WHEN activity_date BETWEEN ? AND ? THEN message_count
                    ELSE 0
                END), 0) AS week_count,
                COALESCE(SUM(CASE
                    WHEN activity_date BETWEEN ? AND ? THEN message_count
                    ELSE 0
                END), 0) AS month_count
            FROM message_activity
            WHERE user_id = ?
            """,
            (
                day_start,
                today,
                week_start,
                today,
                month_start,
                today,
                user_id,
            ),
        )
        row = await cursor.fetchone()
    return {
        "d": int(row["day_count"]),
        "w": int(row["week_count"]),
        "m": int(row["month_count"]),
    }


async def get_group_message_activity(
    group_id: int,
) -> dict[int, dict[str, int]]:
    today = datetime.now(MOSCOW_TIMEZONE).date()
    day_start, _ = _message_activity_period_dates("d", today=today)
    week_start, _ = _message_activity_period_dates("w", today=today)
    month_start, _ = _message_activity_period_dates("m", today=today)
    async with _connect() as db:
        cursor = await db.execute(
            """
            SELECT
                user_id,
                COALESCE(SUM(CASE
                    WHEN activity_date BETWEEN ? AND ? THEN message_count ELSE 0
                END), 0) AS day_count,
                COALESCE(SUM(CASE
                    WHEN activity_date BETWEEN ? AND ? THEN message_count ELSE 0
                END), 0) AS week_count,
                COALESCE(SUM(CASE
                    WHEN activity_date BETWEEN ? AND ? THEN message_count ELSE 0
                END), 0) AS month_count
            FROM message_activity
            WHERE chat_id = ? AND activity_date BETWEEN ? AND ?
            GROUP BY user_id
            """,
            (
                day_start,
                today,
                week_start,
                today,
                month_start,
                today,
                group_id,
                month_start,
                today,
            ),
        )
        rows = await cursor.fetchall()
    return {
        int(row["user_id"]): {
            "d": int(row["day_count"]),
            "w": int(row["week_count"]),
            "m": int(row["month_count"]),
        }
        for row in rows
    }


async def get_group_message_leaderboard(
    chat_id: int,
    period: str,
    *,
    limit: int = 10,
) -> list[dict[str, Any]]:
    start_date, end_date = _message_activity_period_dates(period)
    safe_limit = min(max(1, limit), 50)
    async with _connect() as db:
        cursor = await db.execute(
            """
            WITH counts AS (
                SELECT user_id, SUM(message_count) AS message_count
                FROM message_activity
                WHERE chat_id = ? AND activity_date BETWEEN ? AND ?
                GROUP BY user_id
            )
            SELECT
                counts.user_id,
                counts.message_count,
                COALESCE(
                    NULLIF(global_profiles.callsign, ''),
                    NULLIF(global_profiles.display_name, ''),
                    NULLIF(group_users.username, ''),
                    'Участник'
                ) AS display_name,
                COALESCE(global_profiles.username, group_users.username) AS username
            FROM counts
            LEFT JOIN users AS group_users
              ON group_users.user_id = counts.user_id
             AND group_users.group_id = ?
            LEFT JOIN global_profiles ON global_profiles.user_id = counts.user_id
            ORDER BY counts.message_count DESC, display_name COLLATE NOCASE
            LIMIT ?
            """,
            (chat_id, start_date, end_date, chat_id, safe_limit),
        )
        return [dict(row) for row in await cursor.fetchall()]


async def has_chat_message(chat_id: int, message_id: int) -> bool:
    for database_path in [DATABASE_PATH, *_message_archive_paths()]:
        async with aiosqlite.connect(database_path) as db:
            cursor = await db.execute(
                "SELECT 1 FROM messages WHERE chat_id = ? AND message_id = ? LIMIT 1",
                (chat_id, message_id),
            )
            if await cursor.fetchone() is not None:
                return True
    return False


async def purge_private_message_history() -> int:
    """Removes previously retained direct-message history from every message shard."""
    deleted = 0
    changed_archives: list[str] = []
    for database_path in [DATABASE_PATH, *_message_archive_paths()]:
        async with aiosqlite.connect(database_path) as db:
            cursor = await db.execute("DELETE FROM messages WHERE chat_id > 0")
            deleted += cursor.rowcount
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
    if deleted:
        _record_write()
        logging.info("Удалена ранее сохранённая история личных сообщений: %s записей.", deleted)
    return deleted


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
    summary_minutes: int | None = None,
) -> list[tuple[int, str, int, str]]:
    try:
        if (await get_meta(_CHAT_CONTEXT_ENABLED_KEY)) == "0":
            return []
        if summary:
            return await get_history_for_summary(
                chat_id,
                hours=hours,
                minutes=summary_minutes,
            )
        return await get_recent_history(chat_id, limit + 1)
    except Exception as exc:
        logging.exception(
            "Не удалось загрузить историю чата %s.",
            chat_id,
        )
        if summary or isinstance(exc, HistoryDecryptionError):
            raise
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


async def get_recent_chat_message_ids(
    chat_id: int,
    limit: int,
    *,
    exclude_message_id: int | None = None,
) -> list[int]:
    if limit <= 0:
        return []
    rows: list[tuple[int, str, int]] = []
    for database_path in [DATABASE_PATH, *_message_archive_paths()]:
        async with aiosqlite.connect(database_path) as db:
            cursor = await db.execute(
                """
                SELECT message_id, created_at, id
                FROM messages
                WHERE chat_id = ?
                ORDER BY created_at DESC, message_id DESC, id DESC
                LIMIT ?
                """,
                (chat_id, limit),
            )
            rows.extend(
                (int(row[0]), str(row[1]), int(row[2]))
                for row in await cursor.fetchall()
            )
    rows.sort(key=lambda row: (row[1], row[0], row[2]), reverse=True)
    message_ids: list[int] = []
    seen: set[int] = set()
    for message_id, _created_at, _row_id in rows:
        if message_id == exclude_message_id or message_id in seen:
            continue
        seen.add(message_id)
        message_ids.append(message_id)
        if len(message_ids) >= limit:
            break
    return message_ids


async def delete_chat_message_records(chat_id: int, message_ids: list[int]) -> int:
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
                    WHERE chat_id = ? AND message_id IN ({placeholders})
                    """,
                    (chat_id, *message_ids),
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
    minutes: int | None = None,
    limit: int | None = None,
) -> list[tuple[int, str, int, str]]:
    rows = await _get_message_rows(
        chat_id,
        max(1, limit) if limit is not None else None,
        hours=hours,
        minutes=minutes,
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
    minutes: int | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    query = (
        "SELECT id, message_id, sender_name, user_id, text, created_at, payload_version "
        "FROM messages "
        "WHERE chat_id = ? "
    )
    params: tuple[object, ...] = (chat_id,)
    if minutes is not None:
        query += "AND created_at >= datetime('now', '+3 hours', ?) "
        params += (f"-{max(1, minutes)} minutes",)
    elif hours is not None:
        query += "AND created_at >= datetime('now', '+3 hours', ?) "
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
    for row in rows:
        if int(row.get("payload_version") or 0) == 1:
            payload = _decrypt_message_payload(str(row["text"]))
            row.update(payload)
        else:
            row.setdefault("gender", "неизвестен")
            row.setdefault("age", None)
            row.setdefault("reactions", [])
            row.setdefault("reply_to", None)
    return rows


async def get_group_history_export_rows(chat_id: int) -> list[dict[str, Any]]:
    rows = await _get_message_rows(chat_id, None)
    rows.sort(
        key=lambda row: (
            str(row["created_at"]),
            int(row["message_id"]),
            int(row["id"]),
        )
    )
    return rows


async def get_summary_history_rows(
    chat_id: int,
    period_minutes: int,
) -> list[dict[str, Any]]:
    rows = await _get_message_rows(chat_id, None, minutes=period_minutes)
    rows.sort(
        key=lambda row: (
            str(row["created_at"]),
            int(row["message_id"]),
            int(row["id"]),
        )
    )
    return rows


def format_group_history_export(rows: list[dict[str, Any]]) -> bytes:
    def field(value: object) -> str:
        return str(value if value is not None else "").replace("\r", " ").replace(
            "\n", " ⏎ "
        ).replace("|", "\\|")

    lines = [
        "время | имя | пол | возраст | текст | реакции и от кого | reply-to"
    ]
    for row in rows:
        reactions = row.get("reactions")
        reaction_text = ", ".join(
            f"{item.get('reaction', '')} от {item.get('name', 'участник')}"
            for item in reactions
            if isinstance(item, dict)
        ) if isinstance(reactions, list) else ""
        lines.append(
            " | ".join(
                (
                    field(row.get("created_at")),
                    field(row.get("sender_name")),
                    field(row.get("gender") or "неизвестен"),
                    field(row.get("age")),
                    field(row.get("text")),
                    field(reaction_text),
                    field(row.get("reply_to")),
                )
            )
        )
    return ("\n".join(lines) + "\n").encode("utf-8")


async def update_message_reaction(
    chat_id: int,
    message_id: int,
    actor_id: int,
    actor_name: str,
    reaction: str | None,
) -> bool:
    async with _write_lock:
        for database_path in [DATABASE_PATH, *_message_archive_paths()]:
            async with aiosqlite.connect(database_path) as db:
                db.row_factory = aiosqlite.Row
                cursor = await db.execute(
                    """
                    SELECT id, sender_name, text, payload_version
                    FROM messages
                    WHERE chat_id = ? AND message_id = ?
                    ORDER BY id DESC LIMIT 1
                    """,
                    (chat_id, message_id),
                )
                row = await cursor.fetchone()
                if row is None:
                    continue
                if int(row["payload_version"] or 0) == 1:
                    payload = _decrypt_message_payload(str(row["text"]))
                else:
                    payload = {
                        "sender_name": str(row["sender_name"]),
                        "text": str(row["text"]),
                        "gender": "неизвестен",
                        "age": None,
                        "reactions": [],
                        "reply_to": None,
                    }
                reactions = [
                    item
                    for item in payload.get("reactions", [])
                    if not isinstance(item, dict) or item.get("user_id") != actor_id
                ]
                if reaction:
                    reactions.append(
                        {
                            "user_id": actor_id,
                            "name": actor_name,
                            "reaction": reaction,
                        }
                    )
                payload["reactions"] = reactions
                encrypted_text = _encrypt_message_payload(
                    str(payload.get("sender_name") or "Без имени"),
                    str(payload["text"]),
                    gender=str(payload.get("gender") or "неизвестен"),
                    age=payload.get("age")
                    if isinstance(payload.get("age"), int)
                    else None,
                    reactions=reactions,
                    reply_to=(
                        str(payload["reply_to"])
                        if payload.get("reply_to") is not None
                        else None
                    ),
                )
                await db.execute(
                    """
                    UPDATE messages
                    SET sender_name = '[encrypted]', text = ?, payload_version = 1
                    WHERE id = ?
                    """,
                    (encrypted_text, int(row["id"])),
                )
                await db.commit()
                if database_path != DATABASE_PATH:
                    async with _connect() as main_db:
                        await main_db.execute(
                            "DELETE FROM meta WHERE key = ?",
                            (f"message_archive_file_id:{database_path.name}",),
                        )
                        await main_db.commit()
                _record_write()
                return True
    return False


def _moscow_date() -> date:
    return datetime.now(MOSCOW_TIMEZONE).date()


async def get_duel_balance(
    user_id: int,
    username: str | None,
    *,
    is_bot: bool = False,
) -> int:
    today = _moscow_date()
    today_text = today.isoformat()
    async with _write_lock:
        async with _connect() as db:
            cursor = await db.execute(
                """
                INSERT OR IGNORE INTO duel_players(
                    user_id, username, balance_cents, last_mined_on, is_bot
                )
                VALUES (?, ?, 500, ?, ?)
                """,
                (user_id, username, today_text, int(is_bot)),
            )
            changed = cursor.rowcount > 0
            if is_bot:
                cursor = await db.execute(
                    "UPDATE duel_players SET is_bot = 1 WHERE user_id = ? AND is_bot = 0",
                    (user_id,),
                )
                changed = changed or cursor.rowcount > 0
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


class InsufficientDuelBalanceError(ValueError):
    """Баланс проигравшего меньше ставки в одну монету."""


async def settle_player_duel(
    winner_id: int,
    winner_username: str | None,
    loser_id: int,
    loser_username: str | None,
) -> tuple[int, int]:
    if winner_id == loser_id:
        raise ValueError("Игрок не может выиграть дуэль у самого себя.")

    async with _write_lock:
        async with _connect() as db:
            try:
                cursor = await db.execute(
                    "SELECT user_id, balance_cents FROM duel_players WHERE user_id IN (?, ?)",
                    (winner_id, loser_id),
                )
                balances = {
                    int(row["user_id"]): int(row["balance_cents"])
                    for row in await cursor.fetchall()
                }
                if winner_id not in balances or loser_id not in balances:
                    raise sqlite3.DatabaseError("Не найден игровой профиль участника дуэли.")
                if balances[loser_id] < 100:
                    raise InsufficientDuelBalanceError(
                        "Баланс проигравшего меньше ставки в одну монету."
                    )

                cursor = await db.execute(
                    "UPDATE duel_players SET balance_cents = balance_cents - 100, username = ? "
                    "WHERE user_id = ? AND balance_cents >= 100",
                    (loser_username, loser_id),
                )
                if cursor.rowcount != 1:
                    raise InsufficientDuelBalanceError(
                        "Баланс проигравшего меньше ставки в одну монету."
                    )
                await db.execute(
                    "UPDATE duel_players SET balance_cents = balance_cents + 100, username = ? "
                    "WHERE user_id = ?",
                    (winner_username, winner_id),
                )
                cursor = await db.execute(
                    "SELECT user_id, balance_cents FROM duel_players WHERE user_id IN (?, ?)",
                    (winner_id, loser_id),
                )
                settled_balances = {
                    int(row["user_id"]): int(row["balance_cents"])
                    for row in await cursor.fetchall()
                }
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        _record_write()
    return settled_balances[winner_id], settled_balances[loser_id]


async def mine_duel_coins_daily() -> int:
    today = _moscow_date()
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


async def get_duel_leaderboard(limit: int = 10) -> list[dict[str, Any]]:
    await mine_duel_coins_daily()
    safe_limit = max(1, min(limit, 50))
    async with _connect() as db:
        cursor = await db.execute(
            """
            SELECT
                duel_players.user_id,
                duel_players.username,
                COALESCE(
                    NULLIF(global_profiles.callsign, ''),
                    NULLIF(global_profiles.display_name, ''),
                    CASE
                        WHEN NULLIF(duel_players.username, '') IS NOT NULL
                        THEN '@' || duel_players.username
                    END,
                    'Игрок ' || duel_players.user_id
                ) AS display_name,
                duel_players.balance_cents
            FROM duel_players
            LEFT JOIN global_profiles ON global_profiles.user_id = duel_players.user_id
            WHERE duel_players.is_bot = 0
            ORDER BY duel_players.balance_cents DESC, duel_players.user_id ASC
            LIMIT ?
            """,
            (safe_limit,),
        )
        return [dict(row) for row in await cursor.fetchall()]


def _message_archive_paths() -> list[Path]:
    return sorted(DATABASE_ARCHIVE_DIR.glob("messages_*.db")) if DATABASE_ARCHIVE_DIR.exists() else []


async def _backfill_message_activity() -> None:
    activity: dict[tuple[int, int, str], int] = {}
    database_paths = [DATABASE_PATH, *_message_archive_paths()]
    try:
        async with _connect() as db:
            cursor = await db.execute(
                "SELECT group_id, user_id FROM users WHERE is_bot = 0"
            )
            known_memberships = {
                (int(row["group_id"]), int(row["user_id"]))
                for row in await cursor.fetchall()
            }
        for database_path in database_paths:
            async with aiosqlite.connect(database_path) as source:
                cursor = await source.execute(
                    """
                    SELECT
                        chat_id,
                        user_id,
                        date(created_at) AS activity_date,
                        COUNT(*) AS message_count
                    FROM messages
                    WHERE chat_id < 0
                      AND created_at >= datetime('now', '+3 hours', '-32 days')
                      AND date(created_at) IS NOT NULL
                    GROUP BY chat_id, user_id, activity_date
                    """
                )
                for row in await cursor.fetchall():
                    chat_id, user_id = int(row[0]), int(row[1])
                    if (chat_id, user_id) not in known_memberships:
                        continue
                    key = (chat_id, user_id, str(row[2]))
                    activity[key] = activity.get(key, 0) + int(row[3])
    except (OSError, sqlite3.Error):
        logging.exception("Не удалось перенести доступную историю в статистику сообщений.")
        return

    async with _write_lock:
        async with _connect() as db:
            cursor = await db.execute(
                "SELECT 1 FROM meta WHERE key = ?",
                (_MESSAGE_ACTIVITY_BACKFILL_KEY,),
            )
            if await cursor.fetchone() is not None:
                return
            await db.executemany(
                """
                INSERT OR IGNORE INTO message_activity(
                    chat_id, user_id, activity_date, message_count
                )
                VALUES (?, ?, ?, ?)
                """,
                (
                    (chat_id, user_id, activity_date, count)
                    for (chat_id, user_id, activity_date), count in activity.items()
                ),
            )
            await db.execute(
                "INSERT INTO meta(key, value) VALUES (?, '1')",
                (_MESSAGE_ACTIVITY_BACKFILL_KEY,),
            )
            await db.commit()
        _record_write()


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
                    created_at TIMESTAMP NOT NULL DEFAULT (datetime('now', '+3 hours'))
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


async def is_feature_authorized(user_id: int, feature: str) -> bool:
    async with _connect() as db:
        cursor = await db.execute(
            """
            SELECT 1 FROM user_features
            WHERE user_id = ? AND feature = ?
              AND (expires_at IS NULL OR expires_at > ?)
            """,
            (user_id, feature, time.time()),
        )
        return await cursor.fetchone() is not None


async def list_feature_user_ids(
    feature: str,
    include_expired: bool = False,
) -> list[int]:
    async with _connect() as db:
        if include_expired:
            cursor = await db.execute(
                """
                SELECT user_id FROM user_features
                WHERE feature = ?
                ORDER BY user_id
                """,
                (feature,),
            )
        else:
            cursor = await db.execute(
                """
                SELECT user_id FROM user_features
                WHERE feature = ? AND (expires_at IS NULL OR expires_at > ?)
                ORDER BY user_id
                """,
                (feature, time.time()),
            )
        return [int(row["user_id"]) for row in await cursor.fetchall()]


async def list_feature_authorized_users(feature: str) -> list[dict[str, Any]]:
    async with _connect() as db:
        cursor = await db.execute(
            """
            SELECT user_id, authorized_at, expires_at
            FROM user_features
            WHERE feature = ? AND (expires_at IS NULL OR expires_at > ?)
            ORDER BY user_id
            """,
            (feature, time.time()),
        )
        return [dict(row) for row in await cursor.fetchall()]


async def revoke_feature_access(user_id: int, feature: str) -> bool:
    async with _write_lock:
        async with _connect() as db:
            cursor = await db.execute(
                "DELETE FROM user_features WHERE user_id = ? AND feature = ?",
                (user_id, feature),
            )
            await db.commit()
            deleted = cursor.rowcount > 0
        if deleted:
            _record_write()
        return deleted


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
            if granted_level == "autoresponder":
                await db.execute("DELETE FROM access_keys WHERE key = ?", (key,))
                await db.execute(
                    """
                    INSERT INTO user_features(user_id, feature, expires_at)
                    VALUES (?, 'autoresponder', ?)
                    ON CONFLICT(user_id, feature) DO UPDATE SET
                        expires_at = excluded.expires_at,
                        authorized_at = datetime('now', '+3 hours')
                    """,
                    (user_id, expires_at),
                )
                await db.commit()
                _record_write()
                return {
                    "access_level": "autoresponder",
                    "expires_at": expires_at,
                }
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
                    authorized_at = datetime('now', '+3 hours')
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
    if access_level not in ("user", "admin", "autoresponder"):
        raise ValueError("access_level должен быть user, admin или autoresponder.")
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


async def set_meta(key: str, value: str) -> None:
    async with _write_lock:
        async with _connect() as db:
            await db.execute(
                """
                INSERT INTO meta(key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                WHERE meta.value IS NOT excluded.value
                """,
                (key, value),
            )
            await db.commit()
        _record_write()


async def get_known_backup_message_ids() -> set[int]:
    async with _connect() as db:
        cursor = await db.execute("SELECT key, value FROM meta")
        records = await cursor.fetchall()
        cursor = await db.execute(
            "SELECT backup_msg_id FROM group_owner_banners "
            "WHERE backup_msg_id IS NOT NULL"
        )
        owner_banner_backup_ids = {
            int(row["backup_msg_id"]) for row in await cursor.fetchall()
        }
    message_ids: set[int] = owner_banner_backup_ids
    for row in records:
        key = str(row["key"])
        value = str(row["value"])
        if key == "backup_message_id" or key.startswith(
            "message_archive_message_id:"
        ):
            try:
                message_ids.add(int(value))
            except ValueError:
                logging.warning("Некорректный ID резервного сообщения в meta: %s.", key)
        elif key == _TELEGRAM_SESSION_BACKUPS_KEY or key.startswith(
            ("history_backup:", "profile_banner_backup:")
        ):
            try:
                payload = json.loads(value)
                if key == _TELEGRAM_SESSION_BACKUPS_KEY and isinstance(payload, dict):
                    message_ids.update(
                        int(entry["message_id"])
                        for entry in payload.values()
                        if isinstance(entry, dict)
                        and str(entry.get("message_id", "")).isdigit()
                    )
                elif isinstance(payload, dict) and str(
                    payload.get("message_id", "")
                ).isdigit():
                    message_ids.add(int(payload["message_id"]))
            except (TypeError, ValueError, json.JSONDecodeError):
                logging.exception("Повреждены метаданные бэкапа %s.", key)
    return message_ids


async def reset_all_application_data() -> int:
    global _dirty_generation, _synced_generation
    archive_paths = list(DATABASE_ARCHIVE_DIR.rglob("messages_*.db"))
    for archive_path in archive_paths:
        await asyncio.to_thread(archive_path.unlink)
    if DATABASE_BACKUP_PATH.exists():
        await asyncio.to_thread(DATABASE_BACKUP_PATH.unlink)
    async with _write_lock:
        async with _connect() as db:
            cursor = await db.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'table'
                  AND name NOT LIKE 'sqlite_%'
                """
            )
            table_names = [str(row["name"]) for row in await cursor.fetchall()]
            deleted_rows = 0
            for table_name in table_names:
                cursor = await db.execute(f'DELETE FROM "{table_name}"')
                deleted_rows += max(cursor.rowcount, 0)
            await db.execute(
                "INSERT INTO meta(key, value) VALUES ('dirty', '1')"
            )
            await db.commit()
        _record_write()
    return deleted_rows


async def get_telegram_session_backup_manifest() -> dict[int, dict[str, Any]]:
    raw_manifest = await get_meta(_TELEGRAM_SESSION_BACKUPS_KEY)
    if raw_manifest is None:
        return {}
    try:
        stored = json.loads(raw_manifest)
        if not isinstance(stored, dict):
            raise ValueError("Список бэкапов Telegram-сессий должен быть объектом.")
        return {
            int(user_id): dict(entry)
            for user_id, entry in stored.items()
            if isinstance(entry, dict)
        }
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("Повреждён список бэкапов Telegram-сессий.") from exc


async def is_telegram_session_backup_warning_sent(user_id: int) -> bool:
    return await get_meta(f"telegram_session_backup_warning:{user_id}") == "1"


async def mark_telegram_session_backup_warning_sent(user_id: int) -> None:
    await set_meta(f"telegram_session_backup_warning:{user_id}", "1")


async def set_telegram_session_backup(
    user_id: int,
    *,
    file_id: str,
    message_id: int,
    modified_ns: int,
    size: int,
    enabled: bool,
) -> None:
    async with _write_lock:
        async with _connect() as db:
            cursor = await db.execute(
                "SELECT value FROM meta WHERE key = ?",
                (_TELEGRAM_SESSION_BACKUPS_KEY,),
            )
            row = await cursor.fetchone()
            manifest: dict[str, Any] = {}
            if row is not None:
                parsed = json.loads(str(row["value"]))
                if not isinstance(parsed, dict):
                    raise ValueError("Список бэкапов Telegram-сессий должен быть объектом.")
                manifest = parsed
            manifest[str(user_id)] = {
                "file_id": file_id,
                "message_id": message_id,
                "modified_ns": modified_ns,
                "size": size,
                "enabled": enabled,
            }
            await db.execute(
                """
                INSERT INTO meta(key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (_TELEGRAM_SESSION_BACKUPS_KEY, json.dumps(manifest, sort_keys=True)),
            )
            await db.commit()
        _record_write()


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


async def get_autoresponder_settings(
    user_id: int | None = None,
) -> tuple[int, bool, bool]:
    """Возвращает смещение UTC, статус ночной рассылки и автоответа в ЛС."""
    timezone_value = await get_meta(
        _autoresponder_meta_key(_AUTORESPONDER_TIMEZONE_KEY, user_id)
    )
    night_value = await get_meta(
        _autoresponder_meta_key(_AUTORESPONDER_NIGHT_ENABLED_KEY, user_id)
    )
    private_value = await get_meta(
        _autoresponder_meta_key(_AUTORESPONDER_PRIVATE_ENABLED_KEY, user_id)
    )

    try:
            timezone_offset = (
                _AUTORESPONDER_DEFAULT_TIMEZONE_OFFSET
                if timezone_value is None
                else int(timezone_value)
            )
    except ValueError as exc:
            raise ValueError("В базе сохранено некорректное смещение UTC.") from exc
    if not -12 <= timezone_offset <= 14:
            raise ValueError("Смещение UTC должно быть от -12 до +14 часов.")

    if night_value not in (None, "0", "1") or private_value not in (
            None,
            "0",
            "1",
    ):
            raise ValueError("В базе сохранено некорректное состояние автоответчика.")
    return timezone_offset, night_value == "1", private_value == "1"


async def set_autoresponder_timezone_offset(
    offset: int,
    user_id: int | None = None,
) -> None:
    if not -12 <= offset <= 14:
            raise ValueError("Смещение UTC должно быть от -12 до +14 часов.")
    await set_meta(
        _autoresponder_meta_key(_AUTORESPONDER_TIMEZONE_KEY, user_id),
        str(offset),
    )


async def set_autoresponder_night_enabled(
    enabled: bool,
    user_id: int | None = None,
) -> None:
    await set_meta(
        _autoresponder_meta_key(_AUTORESPONDER_NIGHT_ENABLED_KEY, user_id),
        "1" if enabled else "0",
    )


async def set_autoresponder_private_enabled(
    enabled: bool,
    user_id: int | None = None,
) -> None:
    await set_meta(
        _autoresponder_meta_key(_AUTORESPONDER_PRIVATE_ENABLED_KEY, user_id),
        "1" if enabled else "0",
    )


async def get_autoresponder_night_groups(user_id: int | None = None) -> list[int]:
    raw_value = await get_meta(_autoresponder_meta_key(_AUTORESPONDER_GROUPS_KEY, user_id))
    if raw_value is None:
        return []
    try:
        values = json.loads(raw_value)
        if not isinstance(values, list) or any(
            isinstance(value, bool) or not isinstance(value, int)
            for value in values
        ):
            raise ValueError
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValueError("В базе сохранён некорректный список групп авторассылки.") from exc
    return sorted(set(values))


async def set_autoresponder_night_groups(
    group_ids: list[int],
    user_id: int | None = None,
) -> None:
    if any(isinstance(group_id, bool) or not isinstance(group_id, int) for group_id in group_ids):
        raise ValueError("ID группы должен быть целым числом.")
    await set_meta(
        _autoresponder_meta_key(_AUTORESPONDER_GROUPS_KEY, user_id),
        json.dumps(sorted(set(group_ids)), separators=(",", ":")),
    )


async def get_autoresponder_night_text(user_id: int | None = None) -> str:
    value = await get_meta(
        _autoresponder_meta_key("autoresponder_night_text", user_id)
    )
    return _AUTORESPONDER_DEFAULT_NIGHT_TEXT if value is None else value


async def set_autoresponder_night_text(
    text: str,
    user_id: int | None = None,
) -> None:
    if not text.strip():
        raise ValueError("Текст ночного пожелания не может быть пустым.")
    if len(text) > 1000:
        raise ValueError("Текст ночного пожелания не должен превышать 1000 символов.")
    await set_meta(
        _autoresponder_meta_key("autoresponder_night_text", user_id),
        text,
    )


async def get_autoresponder_private_text(user_id: int | None = None) -> str:
    value = await get_meta(
        _autoresponder_meta_key("autoresponder_private_text", user_id)
    )
    return _AUTORESPONDER_DEFAULT_PRIVATE_TEXT if value is None else value


async def set_autoresponder_private_text(
    text: str,
    user_id: int | None = None,
) -> None:
    if not text.strip():
        raise ValueError("Текст автоответа в личных сообщениях не может быть пустым.")
    if len(text) > 1000:
        raise ValueError("Текст автоответа не должен превышать 1000 символов.")
    await set_meta(
        _autoresponder_meta_key("autoresponder_private_text", user_id),
        text,
    )


def list_message_archives() -> list[Path]:
    return _message_archive_paths()


async def get_message_archive_file_id(filename: str) -> str | None:
    key = f"message_archive_file_id:{Path(filename).name}"
    return await get_meta(key)


async def get_message_archive_message_id(filename: str) -> int | None:
    key = f"message_archive_message_id:{Path(filename).name}"
    value = await get_meta(key)
    try:
        return int(value) if value is not None else None
    except ValueError:
        logging.exception("Некорректный ID сообщения архива %s в SQLite.", filename)
        return None


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


class InsufficientCasinoBalanceError(ValueError):
    """The player's balance cannot cover a casino round."""


class ActiveCasinoRoundError(ValueError):
    """The player already has an unfinished casino round."""


async def get_banner_item(item_id: int) -> dict[str, Any] | None:
    async with _connect() as db:
        cursor = await db.execute("SELECT * FROM banner_items WHERE item_id = ?", (item_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None


async def request_banner_transfer(
    sender_id: int,
    recipient_id: int,
    item_id: int,
) -> str:
    if sender_id == recipient_id:
        return "same_user"
    async with _write_lock:
        async with _connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "SELECT user_id, source FROM banner_items WHERE item_id = ?",
                (item_id,),
            )
            item = await cursor.fetchone()
            if item is None or int(item["user_id"]) != sender_id:
                await db.rollback()
                return "missing"
            if item["source"] == "owner":
                await db.rollback()
                return "owner_item"
            cursor = await db.execute(
                "SELECT 1 FROM pending_banner_transfers WHERE item_id = ?",
                (item_id,),
            )
            if await cursor.fetchone() is not None:
                await db.rollback()
                return "already_pending"
            cursor = await db.execute(
                "SELECT 1 FROM market_lots WHERE item_id = ? AND status = 'open'",
                (item_id,),
            )
            if await cursor.fetchone() is not None:
                await db.rollback()
                return "listed"
            cursor = await db.execute(
                "SELECT 1 FROM global_profiles WHERE user_id = ?",
                (recipient_id,),
            )
            if await cursor.fetchone() is None:
                await db.rollback()
                return "recipient_not_found"
            cursor = await db.execute(
                "INSERT INTO pending_banner_transfers(item_id, sender_id, recipient_id) "
                "VALUES (?, ?, ?)",
                (item_id, sender_id, recipient_id),
            )
            if cursor.rowcount != 1:
                await db.rollback()
                return "already_pending"
            await db.commit()
        _record_write()
    return "pending"


async def get_pending_banner_transfer(item_id: int) -> dict[str, Any] | None:
    async with _connect() as db:
        cursor = await db.execute(
            """
            SELECT item_id, sender_id, recipient_id, created_at
            FROM pending_banner_transfers
            WHERE item_id = ?
            """,
            (item_id,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def cancel_banner_transfer(item_id: int, sender_id: int) -> tuple[str, int | None]:
    async with _write_lock:
        async with _connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "SELECT sender_id, recipient_id FROM pending_banner_transfers "
                "WHERE item_id = ?",
                (item_id,),
            )
            transfer = await cursor.fetchone()
            if transfer is None:
                await db.rollback()
                return "missing", None
            if int(transfer["sender_id"]) != sender_id:
                await db.rollback()
                return "not_sender", None
            recipient_id = int(transfer["recipient_id"])
            await db.execute(
                "DELETE FROM pending_banner_transfers WHERE item_id = ?",
                (item_id,),
            )
            await db.commit()
        _record_write()
    return "cancelled", recipient_id


async def accept_banner_transfer(
    item_id: int,
    recipient_id: int,
) -> tuple[str, int | None]:
    async with _write_lock:
        async with _connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "SELECT sender_id, recipient_id FROM pending_banner_transfers "
                "WHERE item_id = ?",
                (item_id,),
            )
            transfer = await cursor.fetchone()
            if transfer is None:
                await db.rollback()
                return "missing", None
            if int(transfer["recipient_id"]) != recipient_id:
                await db.rollback()
                return "not_recipient", None
            sender_id = int(transfer["sender_id"])
            cursor = await db.execute(
                "SELECT source FROM banner_items WHERE item_id = ? AND user_id = ?",
                (item_id, sender_id),
            )
            item = await cursor.fetchone()
            if item is None or item["source"] == "owner":
                await db.execute(
                    "DELETE FROM pending_banner_transfers WHERE item_id = ?",
                    (item_id,),
                )
                await db.commit()
                _record_write()
                return "missing", sender_id
            cursor = await db.execute(
                "SELECT 1 FROM market_lots WHERE item_id = ? AND status = 'open'",
                (item_id,),
            )
            if await cursor.fetchone() is not None:
                await db.rollback()
                return "listed", sender_id
            cursor = await db.execute(
                """
                UPDATE banner_items
                SET user_id = ?, is_active = 0,
                    obtained_at = datetime('now', '+3 hours')
                WHERE item_id = ? AND user_id = ?
                """,
                (recipient_id, item_id, sender_id),
            )
            if cursor.rowcount != 1:
                await db.rollback()
                return "missing", sender_id
            await db.execute(
                "DELETE FROM pending_banner_transfers WHERE item_id = ?",
                (item_id,),
            )
            await db.commit()
        _record_write()
    return "transferred", sender_id


async def return_banner_transfer(
    item_id: int,
    recipient_id: int,
) -> tuple[str, int | None]:
    async with _write_lock:
        async with _connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "SELECT sender_id, recipient_id FROM pending_banner_transfers "
                "WHERE item_id = ?",
                (item_id,),
            )
            transfer = await cursor.fetchone()
            if transfer is None:
                await db.rollback()
                return "missing", None
            if int(transfer["recipient_id"]) != recipient_id:
                await db.rollback()
                return "not_recipient", None
            sender_id = int(transfer["sender_id"])
            await db.execute(
                "DELETE FROM pending_banner_transfers WHERE item_id = ?",
                (item_id,),
            )
            await db.commit()
        _record_write()
    return "returned", sender_id


async def get_active_banner_item(user_id: int) -> dict[str, Any] | None:
    async with _connect() as db:
        cursor = await db.execute(
            """
            SELECT * FROM banner_items
            WHERE user_id = ? AND is_active = 1
              AND (
                  source != 'owner'
                  OR user_id = CAST(
                      (SELECT value FROM meta WHERE key = ?) AS INTEGER
                  )
              )
            """,
            (user_id, _OWNER_BANNER_USER_ID_META_KEY),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def get_user_banner_items(
    user_id: int,
    *,
    limit: int = 8,
    offset: int = 0,
    level: int | None = None,
    sort_by: str = "date",
) -> list[dict[str, Any]]:
    order_by = (
        "item.level DESC, item.obtained_at DESC, item.item_id DESC"
        if sort_by == "level"
        else "item.obtained_at DESC, item.item_id DESC"
    )
    async with _connect() as db:
        cursor = await db.execute(
            f"""
            SELECT item.*,
                   EXISTS(
                       SELECT 1 FROM market_lots lot
                       WHERE lot.item_id = item.item_id AND lot.status = 'open'
                   ) AS is_listed,
                   EXISTS(
                       SELECT 1 FROM pending_banner_transfers transfer
                       WHERE transfer.item_id = item.item_id
                   ) AS is_transfer_pending
            FROM banner_items item
            WHERE item.user_id = ?
              AND (
                  item.source != 'owner'
                  OR item.user_id = CAST(
                      (SELECT value FROM meta WHERE key = ?) AS INTEGER
                  )
              )
              AND (? IS NULL OR item.level = ?)
              AND NOT EXISTS(
                  SELECT 1 FROM market_lots lot
                  WHERE lot.item_id = item.item_id AND lot.status = 'open'
              )
            ORDER BY {order_by}
            LIMIT ? OFFSET ?
            """,
            (user_id, _OWNER_BANNER_USER_ID_META_KEY, level, level, limit, offset),
        )
        return [dict(row) for row in await cursor.fetchall()]


async def count_user_banner_items(user_id: int, *, level: int | None = None) -> int:
    async with _connect() as db:
        cursor = await db.execute(
            """
            SELECT COUNT(*) AS total FROM banner_items item
            WHERE item.user_id = ?
              AND (
                  item.source != 'owner'
                  OR item.user_id = CAST(
                      (SELECT value FROM meta WHERE key = ?) AS INTEGER
                  )
              )
              AND (? IS NULL OR item.level = ?)
              AND NOT EXISTS(
                  SELECT 1 FROM market_lots lot
                  WHERE lot.item_id = item.item_id AND lot.status = 'open'
              )
            """,
            (user_id, _OWNER_BANNER_USER_ID_META_KEY, level, level),
        )
        row = await cursor.fetchone()
        return int(row["total"]) if row else 0


async def set_banner_item_rendering(
    item_id: int,
    *,
    file_id: str,
    backup_msg_id: int,
    content_hash: str,
) -> None:
    async with _write_lock:
        async with _connect() as db:
            cursor = await db.execute(
                """
                UPDATE banner_items
                SET file_id = ?, backup_msg_id = ?, content_hash = ?, rendered_at = ?
                WHERE item_id = ?
                """,
                (file_id, backup_msg_id, content_hash, time.time(), item_id),
            )
            if cursor.rowcount != 1:
                raise sqlite3.DatabaseError(f"Баннер item_id={item_id} не найден.")
            await db.commit()
        _record_write()


async def set_market_banner_rendering(
    item_id: int,
    *,
    file_id: str,
    backup_msg_id: int,
) -> None:
    async with _write_lock:
        async with _connect() as db:
            cursor = await db.execute(
                "UPDATE banner_items SET market_file_id = ?, market_backup_msg_id = ? "
                "WHERE item_id = ?",
                (file_id, backup_msg_id, item_id),
            )
            if cursor.rowcount != 1:
                raise sqlite3.DatabaseError(f"Баннер item_id={item_id} не найден.")
            await db.commit()
        _record_write()


async def activate_banner_item(user_id: int, item_id: int) -> bool:
    async with _write_lock:
        async with _connect() as db:
            await db.execute("BEGIN")
            cursor = await db.execute(
                """
                SELECT 1 FROM banner_items
                WHERE item_id = ? AND user_id = ?
                  AND NOT EXISTS(
                      SELECT 1 FROM pending_banner_transfers transfer
                      WHERE transfer.item_id = banner_items.item_id
                  )
                  AND (
                      source != 'owner'
                      OR user_id = CAST(
                          (SELECT value FROM meta WHERE key = ?) AS INTEGER
                      )
                  )
                """,
                (item_id, user_id, _OWNER_BANNER_USER_ID_META_KEY),
            )
            if await cursor.fetchone() is None:
                await db.rollback()
                return False
            await db.execute(
                "UPDATE banner_items SET is_active = 0 WHERE user_id = ? AND is_active = 1",
                (user_id,),
            )
            await db.execute(
                "UPDATE banner_items SET is_active = 1 WHERE item_id = ? AND user_id = ?",
                (item_id, user_id),
            )
            await db.commit()
        _record_write()
    return True


async def get_or_create_group_owner_banner(
    group_id: int,
    owner_user_id: int,
    *,
    seed: str,
    preset: str,
) -> dict[str, Any]:
    async with _write_lock:
        async with _connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "SELECT * FROM group_owner_banners WHERE group_id = ?",
                (group_id,),
            )
            current = await cursor.fetchone()
            if current is None:
                await db.execute(
                    """
                    INSERT INTO group_owner_banners(
                        group_id, owner_user_id, seed, preset
                    )
                    VALUES (?, ?, ?, ?)
                    """,
                    (group_id, owner_user_id, seed, preset),
                )
            elif int(current["owner_user_id"]) != owner_user_id:
                await db.execute(
                    """
                    UPDATE group_owner_banners
                    SET owner_user_id = ?, seed = ?, preset = ?,
                        file_id = NULL, content_hash = NULL,
                        created_at = datetime('now', '+3 hours')
                    WHERE group_id = ?
                    """,
                    (owner_user_id, seed, preset, group_id),
                )
            await db.commit()
        _record_write()
    banner = await get_group_owner_banner(group_id, owner_user_id)
    if banner is None:
        raise sqlite3.DatabaseError(
            f"Не удалось сохранить баннер владельца группы group_id={group_id}."
        )
    return banner


async def get_group_owner_banner(
    group_id: int,
    owner_user_id: int,
) -> dict[str, Any] | None:
    async with _connect() as db:
        cursor = await db.execute(
            """
            SELECT * FROM group_owner_banners
            WHERE group_id = ? AND owner_user_id = ?
            """,
            (group_id, owner_user_id),
        )
        row = await cursor.fetchone()
    return dict(row) if row else None


async def set_group_owner_banner_rendering(
    group_id: int,
    owner_user_id: int,
    *,
    file_id: str,
    content_hash: str,
    backup_msg_id: int,
) -> bool:
    if not file_id or not content_hash or backup_msg_id <= 0:
        raise ValueError(
            "Для баннера владельца нужны Telegram file_id, хэш содержимого и message_id."
        )
    async with _write_lock:
        async with _connect() as db:
            cursor = await db.execute(
                """
                UPDATE group_owner_banners
                SET file_id = ?, content_hash = ?, backup_msg_id = ?
                WHERE group_id = ? AND owner_user_id = ?
                """,
                (file_id, content_hash, backup_msg_id, group_id, owner_user_id),
            )
            await db.commit()
        if cursor.rowcount:
            _record_write()
    return cursor.rowcount == 1


async def begin_casino_round(
    round_id: str,
    user_id: int,
    username: str | None,
    cost_cents: int,
) -> None:
    today_text = _moscow_date().isoformat()
    async with _write_lock:
        async with _connect() as db:
            await db.execute("BEGIN")
            await db.execute(
                """
                INSERT OR IGNORE INTO duel_players(
                    user_id, username, balance_cents, last_mined_on, is_bot
                )
                VALUES (?, ?, 500, ?, 0)
                """,
                (user_id, username, today_text),
            )
            cursor = await db.execute(
                "UPDATE duel_players SET username = ?, balance_cents = balance_cents - ? "
                "WHERE user_id = ? AND balance_cents >= ?",
                (username, cost_cents, user_id, cost_cents),
            )
            if cursor.rowcount != 1:
                await db.rollback()
                raise InsufficientCasinoBalanceError("Недостаточно монет для крутки.")
            try:
                await db.execute(
                    """
                    INSERT INTO casino_rounds(round_id, user_id, cost_cents, status)
                    VALUES (?, ?, ?, 'pending')
                    """,
                    (round_id, user_id, cost_cents),
                )
            except sqlite3.IntegrityError as exc:
                await db.rollback()
                if "idx_casino_rounds_pending_user" in str(exc) or "UNIQUE constraint" in str(exc):
                    raise ActiveCasinoRoundError("У пользователя уже есть активная крутка.") from exc
                raise
            await db.commit()
        _record_write()


async def complete_casino_round(
    round_id: str,
    results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    async with _write_lock:
        async with _connect() as db:
            await db.execute("BEGIN")
            cursor = await db.execute(
                "SELECT user_id, status FROM casino_rounds WHERE round_id = ?",
                (round_id,),
            )
            round_record = await cursor.fetchone()
            if round_record is None or round_record["status"] != "pending":
                await db.rollback()
                raise sqlite3.DatabaseError(f"Крутка {round_id} не ожидает завершения.")
            user_id = int(round_record["user_id"])
            cursor = await db.execute(
                "SELECT 1 FROM banner_items WHERE user_id = ? AND is_active = 1",
                (user_id,),
            )
            activate_first = await cursor.fetchone() is None
            items: list[dict[str, Any]] = []
            for index, result in enumerate(results):
                cursor = await db.execute(
                    """
                    INSERT INTO banner_items(
                        user_id, level, seed, preset, source, is_active
                    )
                    VALUES (?, ?, ?, ?, 'casino', ?)
                    """,
                    (
                        user_id,
                        int(result["level"]),
                        str(result["seed"]),
                        str(result["preset"]) if result.get("preset") else None,
                        int(activate_first and index == 0),
                    ),
                )
                item_id = int(cursor.lastrowid)
                items.append(
                    {
                        "item_id": item_id,
                        "user_id": user_id,
                        "level": int(result["level"]),
                        "seed": str(result["seed"]),
                        "preset": result.get("preset"),
                        "source": "casino",
                        "is_active": int(activate_first and index == 0),
                    }
                )
            await db.execute(
                "UPDATE casino_rounds SET status = 'completed', results = ? WHERE round_id = ?",
                (json.dumps(results, ensure_ascii=False), round_id),
            )
            await db.commit()
        _record_write()
    return items


async def fail_casino_round(round_id: str) -> bool:
    async with _write_lock:
        async with _connect() as db:
            await db.execute("BEGIN")
            cursor = await db.execute(
                "SELECT user_id, cost_cents FROM casino_rounds "
                "WHERE round_id = ? AND status = 'pending'",
                (round_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                await db.rollback()
                return False
            await db.execute(
                "UPDATE duel_players SET balance_cents = balance_cents + ? WHERE user_id = ?",
                (int(row["cost_cents"]), int(row["user_id"])),
            )
            await db.execute(
                "UPDATE casino_rounds SET status = 'failed' WHERE round_id = ?",
                (round_id,),
            )
            await db.commit()
        _record_write()
    return True


async def recover_pending_casino_rounds() -> int:
    async with _write_lock:
        async with _connect() as db:
            await db.execute("BEGIN")
            cursor = await db.execute(
                "SELECT round_id, user_id, cost_cents FROM casino_rounds WHERE status = 'pending'"
            )
            rows = await cursor.fetchall()
            for row in rows:
                await db.execute(
                    "UPDATE duel_players SET balance_cents = balance_cents + ? WHERE user_id = ?",
                    (int(row["cost_cents"]), int(row["user_id"])),
                )
                await db.execute(
                    "UPDATE casino_rounds SET status = 'failed' WHERE round_id = ?",
                    (str(row["round_id"]),),
                )
            await db.commit()
        if rows:
            _record_write()
    return len(rows)


async def recycle_banner_item(user_id: int, item_id: int, payout_cents: int) -> str:
    async with _write_lock:
        async with _connect() as db:
            await db.execute("BEGIN")
            cursor = await db.execute(
                """
                SELECT item.level, item.source, item.is_active,
                       EXISTS(
                           SELECT 1 FROM market_lots lot
                           WHERE lot.item_id = item.item_id AND lot.status = 'open'
                       ) AS is_listed,
                       EXISTS(
                           SELECT 1 FROM pending_banner_transfers transfer
                           WHERE transfer.item_id = item.item_id
                       ) AS is_transfer_pending,
                       player.balance_cents
                FROM banner_items item
                JOIN duel_players player ON player.user_id = item.user_id
                WHERE item.item_id = ? AND item.user_id = ?
                """,
                (item_id, user_id),
            )
            row = await cursor.fetchone()
            if row is None:
                await db.rollback()
                return "missing"
            if row["source"] == "owner" or row["is_listed"]:
                await db.rollback()
                return "forbidden"
            if row["is_transfer_pending"]:
                await db.rollback()
                return "pending"
            in_debt = int(row["balance_cents"]) < 0
            if row["is_active"] and not in_debt:
                await db.rollback()
                return "active"
            cursor = await db.execute(
                "UPDATE banner_items SET is_active = 0 WHERE item_id = ? AND user_id = ?",
                (item_id, user_id),
            )
            if cursor.rowcount != 1:
                await db.rollback()
                return "missing"
            await db.execute("DELETE FROM banner_items WHERE item_id = ?", (item_id,))
            await db.execute(
                "UPDATE duel_players SET balance_cents = balance_cents + ? WHERE user_id = ?",
                (payout_cents, user_id),
            )
            await db.commit()
        _record_write()
    return "recycled"


async def recycle_banner_items_by_level(
    user_id: int,
    level: int,
    quantity: int,
    payout_cents: int,
) -> tuple[str, list[dict[str, Any]], int | None]:
    if not 1 <= level <= 6 or quantity <= 0 or payout_cents < 0:
        raise ValueError("Некорректные параметры переработки.")
    async with _write_lock:
        async with _connect() as db:
            await db.execute("BEGIN")
            cursor = await db.execute(
                """
                SELECT item.item_id, item.backup_msg_id, item.market_backup_msg_id
                FROM banner_items item
                WHERE item.user_id = ? AND item.level = ?
                  AND item.source != 'owner' AND item.is_active = 0
                  AND NOT EXISTS(
                      SELECT 1 FROM market_lots lot
                      WHERE lot.item_id = item.item_id AND lot.status = 'open'
                  )
                  AND NOT EXISTS(
                      SELECT 1 FROM pending_banner_transfers transfer
                      WHERE transfer.item_id = item.item_id
                  )
                ORDER BY item.obtained_at ASC, item.item_id ASC
                LIMIT ?
                """,
                (user_id, level, quantity),
            )
            items = [dict(row) for row in await cursor.fetchall()]
            if len(items) < quantity:
                await db.rollback()
                return "insufficient", items, None
            cursor = await db.execute(
                "SELECT balance_cents FROM duel_players WHERE user_id = ?",
                (user_id,),
            )
            player = await cursor.fetchone()
            if player is None:
                await db.rollback()
                return "missing", [], None
            placeholders = ",".join("?" for _ in items)
            await db.execute(
                f"DELETE FROM banner_items WHERE user_id = ? "
                f"AND item_id IN ({placeholders})",
                (user_id, *(int(item["item_id"]) for item in items)),
            )
            await db.execute(
                "UPDATE duel_players SET balance_cents = balance_cents + ? WHERE user_id = ?",
                (payout_cents * quantity, user_id),
            )
            cursor = await db.execute(
                "SELECT balance_cents FROM duel_players WHERE user_id = ?",
                (user_id,),
            )
            balance_row = await cursor.fetchone()
            if balance_row is None:
                await db.rollback()
                raise sqlite3.DatabaseError(
                    f"Не удалось прочитать баланс user_id={user_id} после переработки."
                )
            balance = int(balance_row["balance_cents"])
            await db.commit()
        _record_write()
    return "recycled", items, balance


async def create_market_lot(
    user_id: int,
    item_id: int,
    price_cents: int,
    *,
    title: str | None = None,
) -> str:
    if title is not None:
        title = re.sub(r"\s+", " ", title).strip()
        if not title or len(title) > 48:
            raise ValueError("Название лота должно содержать от 1 до 48 символов.")
    if price_cents <= 0:
        raise ValueError("Цена лота должна быть больше нуля.")
    async with _write_lock:
        async with _connect() as db:
            await db.execute("BEGIN")
            cursor = await db.execute(
                "SELECT balance_cents FROM duel_players WHERE user_id = ?",
                (user_id,),
            )
            balance = await cursor.fetchone()
            if balance is None or int(balance["balance_cents"]) < 0:
                await db.rollback()
                return "debt"
            cursor = await db.execute(
                "SELECT source, is_active FROM banner_items WHERE item_id = ? AND user_id = ?",
                (item_id, user_id),
            )
            item = await cursor.fetchone()
            if item is None:
                await db.rollback()
                return "missing"
            if item["source"] == "owner" or item["is_active"]:
                await db.rollback()
                return "forbidden"
            cursor = await db.execute(
                "SELECT 1 FROM pending_banner_transfers WHERE item_id = ?",
                (item_id,),
            )
            if await cursor.fetchone() is not None:
                await db.rollback()
                return "pending"
            cursor = await db.execute(
                "SELECT 1 FROM market_lots WHERE item_id = ? AND status = 'open'",
                (item_id,),
            )
            if await cursor.fetchone() is not None:
                await db.rollback()
                return "listed"
            await db.execute(
                "INSERT INTO market_lots(item_id, seller_id, price_cents, title, status) "
                "VALUES (?, ?, ?, ?, 'open')",
                (item_id, user_id, price_cents, title),
            )
            await db.commit()
        _record_write()
    return "listed"


async def get_market_lot(lot_id: int) -> dict[str, Any] | None:
    async with _connect() as db:
        cursor = await db.execute(
            """
            SELECT lot.*, item.level, item.seed, item.preset, item.file_id,
                   item.market_file_id, item.market_backup_msg_id,
                   item.content_hash, item.source
            FROM market_lots lot
            JOIN banner_items item ON item.item_id = lot.item_id
            WHERE lot.lot_id = ? AND lot.status = 'open'
            """,
            (lot_id,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def get_market_lot_for_item(item_id: int) -> dict[str, Any] | None:
    async with _connect() as db:
        cursor = await db.execute(
            "SELECT * FROM market_lots WHERE item_id = ? AND status = 'open'",
            (item_id,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def get_market_lots(
    *,
    level: int | None = None,
    sort_by: str = "date",
    limit: int = 8,
    offset: int = 0,
) -> list[dict[str, Any]]:
    sort = (
        "lot.price_cents ASC, lot.created_at DESC, lot.lot_id DESC"
        if sort_by == "price"
        else "lot.created_at DESC, lot.lot_id DESC"
    )
    query = f"""
        SELECT lot.lot_id, lot.item_id, lot.seller_id, lot.price_cents, lot.title,
               lot.created_at, item.level, item.seed, item.preset, item.file_id,
               item.market_file_id
        FROM market_lots lot
        JOIN banner_items item ON item.item_id = lot.item_id
        WHERE lot.status = 'open' AND (? IS NULL OR item.level = ?)
        ORDER BY {sort}
        LIMIT ? OFFSET ?
    """
    async with _connect() as db:
        cursor = await db.execute(query, (level, level, limit, offset))
        return [dict(row) for row in await cursor.fetchall()]


async def count_market_lots(*, level: int | None = None) -> int:
    async with _connect() as db:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM market_lots lot "
            "JOIN banner_items item ON item.item_id = lot.item_id "
            "WHERE lot.status = 'open' AND (? IS NULL OR item.level = ?)",
            (level, level),
        )
        row = await cursor.fetchone()
        return int(row[0]) if row else 0


_MARKET_SEARCH_RARITIES: dict[int, tuple[str, ...]] = {
    1: ("обычный", "обычная", "common"),
    2: ("необычный", "необычная", "uncommon"),
    3: ("редкий", "редкая", "rare"),
    4: ("эпический", "эпическая", "epic"),
    5: ("элитный", "элитная", "elite"),
    6: ("легендарный", "легендарная", "legendary"),
}
_MARKET_SEARCH_WORD_RE = re.compile(r"[a-zа-яё0-9]+(?:[.,][0-9]+)?", re.IGNORECASE)


def _normalize_market_search_text(value: object) -> str:
    return str(value or "").casefold().replace("ё", "е")


def _market_search_score(
    query: str,
    lot: dict[str, Any],
    preset_names: dict[str, str],
) -> tuple[int, float]:
    query_tokens = _MARKET_SEARCH_WORD_RE.findall(
        _normalize_market_search_text(query)
    )
    if not query_tokens:
        return 0, 0.0

    level = int(lot["level"])
    preset = str(lot.get("preset") or "")
    price_cents = int(lot["price_cents"])
    price_coins = f"{price_cents / 100:.2f}".rstrip("0").rstrip(".")
    fields = [
        str(lot.get("title") or ""),
        str(lot.get("seed") or ""),
        preset,
        preset_names.get(preset, ""),
        str(lot.get("lot_id") or ""),
        str(level),
        _MARKET_SEARCH_RARITIES.get(level, ())[0],
        " ".join(_MARKET_SEARCH_RARITIES.get(level, ())),
        price_coins,
        f"{price_cents / 100:.2f}",
        str(price_cents),
        "seed сиид сид название лот цена редкость",
    ]
    normalized_fields = [_normalize_market_search_text(field) for field in fields]
    corpus = " ".join(normalized_fields)
    corpus_words = _MARKET_SEARCH_WORD_RE.findall(corpus)
    matched = 0
    total_score = 0.0
    for token in query_tokens:
        best = 0.0
        for word in corpus_words:
            if token == word:
                best = 1.0
                break
            if len(token) >= 3 and (token in word or word in token):
                best = max(best, 0.92)
            else:
                best = max(best, difflib.SequenceMatcher(None, token, word).ratio())
        if best >= 0.68:
            matched += 1
            total_score += best
    return matched, total_score


async def search_market_lots(
    query: str,
    *,
    preset_names: dict[str, str] | None = None,
    level: int | None = None,
    sort_by: str = "relevance",
    limit: int = 8,
    offset: int = 0,
) -> list[dict[str, Any]]:
    normalized_query = query.strip()
    if not normalized_query:
        return await get_market_lots(
            level=level,
            sort_by="date" if sort_by == "relevance" else sort_by,
            limit=limit,
            offset=offset,
        )
    async with _connect() as db:
        cursor = await db.execute(
            """
            SELECT lot.lot_id, lot.item_id, lot.seller_id, lot.price_cents,
                   lot.title, lot.created_at, item.level, item.seed, item.preset,
                   item.file_id, item.market_file_id
            FROM market_lots lot
            JOIN banner_items item ON item.item_id = lot.item_id
            WHERE lot.status = 'open' AND (? IS NULL OR item.level = ?)
            """,
            (level, level),
        )
        candidates = [dict(row) for row in await cursor.fetchall()]
    names = preset_names or {}
    ranked: list[tuple[tuple[float | int, ...], dict[str, Any]]] = []
    for lot in candidates:
        matched, score = _market_search_score(normalized_query, lot, names)
        if not matched:
            continue
        created_at_value = datetime.fromisoformat(str(lot["created_at"]))
        if created_at_value.tzinfo is None:
            created_at_value = created_at_value.replace(tzinfo=MOSCOW_TIMEZONE)
        created_at = created_at_value.timestamp()
        lot_id = int(lot["lot_id"])
        if sort_by == "price":
            sort_key: tuple[float | int, ...] = (
                -matched,
                int(lot["price_cents"]),
                -score,
                -lot_id,
            )
        elif sort_by == "date":
            sort_key = (-matched, -created_at, -score, -lot_id)
        else:
            sort_key = (-matched, -score, -created_at, -lot_id)
        ranked.append((sort_key, lot))
    ranked.sort(key=lambda row: row[0])
    safe_offset = max(0, offset)
    safe_limit = max(1, min(limit, 50))
    return [
        row[1]
        for row in ranked[safe_offset:safe_offset + safe_limit]
    ]


async def count_market_search_results(
    query: str,
    *,
    preset_names: dict[str, str] | None = None,
    level: int | None = None,
) -> int:
    if not query.strip():
        return await count_market_lots(level=level)
    async with _connect() as db:
        cursor = await db.execute(
            """
            SELECT lot.lot_id, lot.price_cents, lot.title, lot.created_at,
                   item.level, item.seed, item.preset
            FROM market_lots lot
            JOIN banner_items item ON item.item_id = lot.item_id
            WHERE lot.status = 'open' AND (? IS NULL OR item.level = ?)
            """,
            (level, level),
        )
        candidates = [dict(row) for row in await cursor.fetchall()]
    names = preset_names or {}
    return sum(
        _market_search_score(query, lot, names)[0] > 0
        for lot in candidates
    )


async def get_user_market_lots(user_id: int) -> list[dict[str, Any]]:
    async with _connect() as db:
        cursor = await db.execute(
            """
            SELECT lot.lot_id, lot.item_id, lot.price_cents, lot.title,
                   lot.created_at, item.level
            FROM market_lots lot
            JOIN banner_items item ON item.item_id = lot.item_id
            WHERE lot.seller_id = ? AND lot.status = 'open'
            ORDER BY lot.created_at DESC
            """,
            (user_id,),
        )
        return [dict(row) for row in await cursor.fetchall()]


async def withdraw_market_lot(user_id: int, lot_id: int) -> bool:
    async with _write_lock:
        async with _connect() as db:
            cursor = await db.execute(
                "UPDATE market_lots SET status = 'withdrawn' "
                "WHERE lot_id = ? AND seller_id = ? AND status = 'open'",
                (lot_id, user_id),
            )
            await db.commit()
        if cursor.rowcount:
            _record_write()
    return cursor.rowcount == 1


async def buy_market_lot(
    buyer_id: int,
    buyer_username: str | None,
    lot_id: int,
) -> tuple[str, dict[str, Any] | None]:
    today_text = _moscow_date().isoformat()
    async with _write_lock:
        async with _connect() as db:
            await db.execute("BEGIN")
            cursor = await db.execute(
                """
                SELECT lot.item_id, lot.seller_id, lot.price_cents, item.level,
                       item.seed, item.preset, item.source, item.backup_msg_id,
                       item.market_file_id, item.market_backup_msg_id
                FROM market_lots lot
                JOIN banner_items item ON item.item_id = lot.item_id
                WHERE lot.lot_id = ? AND lot.status = 'open'
                """,
                (lot_id,),
            )
            lot = await cursor.fetchone()
            if lot is None:
                await db.rollback()
                return "sold", None
            seller_id = int(lot["seller_id"])
            item_id = int(lot["item_id"])
            price = int(lot["price_cents"])
            if seller_id == buyer_id:
                await db.rollback()
                return "own", None
            cursor = await db.execute(
                "INSERT OR IGNORE INTO duel_players("
                "user_id, username, balance_cents, last_mined_on, is_bot"
                ") VALUES (?, ?, 500, ?, 0)",
                (buyer_id, buyer_username, today_text),
            )
            cursor = await db.execute(
                "UPDATE duel_players SET username = ?, balance_cents = balance_cents - ? "
                "WHERE user_id = ? AND balance_cents >= ?",
                (buyer_username, price, buyer_id, price),
            )
            if cursor.rowcount != 1:
                await db.rollback()
                return "funds", None
            seller_income = price * 80 // 100
            fee = price - seller_income
            await db.execute(
                "INSERT OR IGNORE INTO duel_players(user_id, balance_cents, last_mined_on) "
                "VALUES (?, 500, ?)",
                (seller_id, today_text),
            )
            await db.execute(
                "UPDATE duel_players SET balance_cents = balance_cents + ? WHERE user_id = ?",
                (seller_income, seller_id),
            )
            fee_key = "market_fee_cents"
            fee_cursor = await db.execute("SELECT value FROM meta WHERE key = ?", (fee_key,))
            fee_row = await fee_cursor.fetchone()
            fee_total = int(fee_row["value"]) if fee_row else 0
            await db.execute(
                "INSERT INTO meta(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (fee_key, str(fee_total + fee)),
            )
            cursor = await db.execute(
                "UPDATE market_lots SET status = 'sold' WHERE lot_id = ? AND status = 'open'",
                (lot_id,),
            )
            if cursor.rowcount != 1:
                await db.rollback()
                return "sold", None
            await db.execute(
                "UPDATE banner_items SET user_id = ?, is_active = 0, file_id = NULL, "
                "backup_msg_id = NULL, content_hash = NULL, rendered_at = NULL WHERE item_id = ?",
                (buyer_id, item_id),
            )
            await db.commit()
        _record_write()
    return "bought", {
        "item_id": item_id,
        "user_id": buyer_id,
        "level": int(lot["level"]),
        "seed": str(lot["seed"]),
        "preset": lot["preset"],
        "source": str(lot["source"]),
        "backup_msg_id": lot["backup_msg_id"],
        "market_file_id": lot["market_file_id"],
        "market_backup_msg_id": lot["market_backup_msg_id"],
    }


async def get_market_fee_cents() -> int:
    value = await get_meta("market_fee_cents")
    try:
        return int(value or 0)
    except ValueError:
        logging.exception("Некорректное значение market_fee_cents в meta.")
        raise sqlite3.DatabaseError("Повреждён счётчик комиссии площадки.")


async def claim_subscription_bonus(
    user_id: int,
    bonus_cents: int,
) -> tuple[str, int | None]:
    async with _write_lock:
        async with _connect() as db:
            await db.execute("BEGIN")
            cursor = await db.execute(
                "SELECT status, penalized_at FROM bonus_claims "
                "WHERE user_id = ? ORDER BY claim_id DESC LIMIT 1",
                (user_id,),
            )
            previous = await cursor.fetchone()
            if previous is not None and previous["status"] == "subscribed":
                await db.rollback()
                return "already_claimed", None
            cursor = await db.execute(
                "SELECT balance_cents FROM duel_players WHERE user_id = ?",
                (user_id,),
            )
            player = await cursor.fetchone()
            if player is None:
                await db.rollback()
                raise sqlite3.DatabaseError(f"Игровой профиль user_id={user_id} отсутствует.")
            await db.execute(
                "UPDATE duel_players SET balance_cents = balance_cents + ? WHERE user_id = ?",
                (bonus_cents, user_id),
            )
            cursor = await db.execute(
                "INSERT INTO bonus_claims(user_id, status) VALUES (?, 'subscribed')",
                (user_id,),
            )
            claim_id = int(cursor.lastrowid)
            await db.commit()
        _record_write()
    return "claimed", claim_id


async def penalize_subscription_bonus(
    user_id: int,
    penalty_cents: int,
) -> int | None:
    async with _write_lock:
        async with _connect() as db:
            await db.execute("BEGIN")
            cursor = await db.execute(
                """
                SELECT claim_id FROM bonus_claims
                WHERE user_id = ? AND status = 'subscribed' AND penalized_at IS NULL
                ORDER BY claim_id DESC LIMIT 1
                """,
                (user_id,),
            )
            claim = await cursor.fetchone()
            if claim is None:
                await db.rollback()
                return None
            await db.execute(
                "UPDATE bonus_claims SET status = 'unsubscribed', "
                "penalized_at = datetime('now', '+3 hours') "
                "WHERE claim_id = ? AND status = 'subscribed' AND penalized_at IS NULL",
                (int(claim["claim_id"]),),
            )
            await db.execute(
                "UPDATE duel_players SET balance_cents = balance_cents - ? WHERE user_id = ?",
                (penalty_cents, user_id),
            )
            await db.execute(
                "UPDATE market_lots SET status = 'withdrawn' "
                "WHERE seller_id = ? AND status = 'open'",
                (user_id,),
            )
            cursor = await db.execute(
                "SELECT balance_cents FROM duel_players WHERE user_id = ?",
                (user_id,),
            )
            row = await cursor.fetchone()
            await db.commit()
        _record_write()
    return int(row["balance_cents"]) if row else None


async def get_users_with_active_subscription_bonus() -> list[int]:
    async with _connect() as db:
        cursor = await db.execute(
            """
            SELECT claim.user_id
            FROM bonus_claims claim
            WHERE claim.status = 'subscribed'
              AND claim.penalized_at IS NULL
              AND claim.claim_id = (
                  SELECT MAX(latest.claim_id)
                  FROM bonus_claims latest
                  WHERE latest.user_id = claim.user_id
              )
            ORDER BY claim.claim_id
            """
        )
        return [int(row["user_id"]) for row in await cursor.fetchall()]
