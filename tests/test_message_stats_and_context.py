import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiosqlite
from cryptography.fernet import Fernet

os.environ.setdefault("TELEGRAM_TOKEN", "123456:test-token")
os.environ.setdefault("GROQ_API_KEY", "test-key")
os.environ.setdefault("HISTORY_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
os.environ.setdefault("ADMIN_ID", "1")

import database
import llm
from config import DEFAULT_CONTEXT_SIZE
from handlers_group import _parse_message_top_request


class MessageStatsAndContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_time_stats_include_old_and_new_activity(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "stats.db"
            connection = sqlite3.connect(database_path)
            try:
                connection.executescript(
                    """
                    CREATE TABLE message_activity (
                        chat_id INTEGER NOT NULL,
                        user_id INTEGER NOT NULL,
                        activity_date TEXT NOT NULL,
                        message_count INTEGER NOT NULL,
                        PRIMARY KEY (chat_id, user_id, activity_date)
                    );
                    CREATE TABLE global_profiles (
                        user_id INTEGER PRIMARY KEY,
                        callsign TEXT,
                        display_name TEXT,
                        username TEXT
                    );
                    CREATE TABLE users (
                        user_id INTEGER NOT NULL,
                        group_id INTEGER NOT NULL,
                        username TEXT,
                        callsign TEXT
                    );
                    INSERT INTO message_activity VALUES (-100, 7, '2020-01-01', 10);
                    INSERT INTO message_activity VALUES (-100, 8, '2020-01-01', 6);
                    INSERT INTO global_profiles VALUES (7, NULL, 'Аня', 'anya');
                    INSERT INTO global_profiles VALUES (8, NULL, 'Борис', 'boris');
                    """
                )
                connection.commit()
            finally:
                connection.close()

            with patch.object(database, "DATABASE_PATH", database_path):
                await database.record_group_message_activity(-100, 7)
                activity = await database.get_group_message_activity(-100)
                user_activity = await database.get_user_message_activity(7)
                leaderboard = await database.get_group_message_leaderboard(-100, "a")

        self.assertEqual(activity[7]["a"], 11)
        self.assertEqual(user_activity["a"], 11)
        self.assertEqual(
            [(entry["user_id"], entry["message_count"]) for entry in leaderboard],
            [(7, 11), (8, 6)],
        )

    async def test_summary_request_is_recognized_without_bot_mention(self):
        self.assertEqual(_parse_message_top_request("стата вся"), (True, "a"))
        self.assertEqual(_parse_message_top_request("стата за всё время"), (True, "a"))
        self.assertTrue(llm.is_summary_request("саммари за 3 часа"))

    async def test_context_prompt_includes_the_last_15_messages(self):
        self.assertEqual(DEFAULT_CONTEXT_SIZE, 15)
        history = [
            (message_id, f"Участник {message_id}", message_id, f"Сообщение {message_id}")
            for message_id in range(1, DEFAULT_CONTEXT_SIZE + 2)
        ]

        prompt = llm.build_prompt(
            history=history,
            user_id=99,
            user_name="Аня",
            display_name="Аня",
            current_message="Что ты думаешь?",
            summary=False,
            current_message_id=DEFAULT_CONTEXT_SIZE + 1,
        )

        self.assertIn("История последних сообщений в чате (всего 15)", prompt)
        self.assertIn("Сообщение 1", prompt)
        self.assertNotIn("Сообщение 16", prompt)
        self.assertIn("Учитывай историю последних сообщений", prompt)

    async def test_summary_tries_next_groq_model_after_failure(self):
        success = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="Краткий итог."))]
        )
        create = AsyncMock(side_effect=[RuntimeError("недоступная модель"), success])
        client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))
        )

        with (
            patch.object(llm, "_client", client),
            patch.object(llm, "SUMMARY_GROQ_MODELS", ("broken-model", "working-model")),
        ):
            result = await llm.generate_summary(
                [],
                period_minutes=60,
                participant_genders={},
            )

        self.assertEqual(result, "Краткий итог.")
        self.assertEqual(
            [call.kwargs["model"] for call in create.await_args_list],
            ["broken-model", "working-model"],
        )

    async def test_archive_migration_preserves_already_encrypted_messages(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "archive.db"
            encrypted = database._encrypt_message_payload("Аня", "Старое сообщение")
            async with aiosqlite.connect(database_path) as connection:
                await connection.execute(
                    """
                    CREATE TABLE messages (
                        id INTEGER PRIMARY KEY,
                        sender_name TEXT NOT NULL,
                        text TEXT NOT NULL,
                        payload_version INTEGER NOT NULL DEFAULT 0
                    )
                    """
                )
                await connection.execute(
                    "INSERT INTO messages(id, sender_name, text) VALUES (1, ?, ?)",
                    ("[encrypted]", encrypted),
                )
                await connection.execute(
                    "INSERT INTO messages(id, sender_name, text) VALUES (2, ?, ?)",
                    ("Борис", "Новое старое сообщение"),
                )
                await connection.commit()

                migrated = await database._encrypt_legacy_messages(connection)
                cursor = await connection.execute(
                    "SELECT sender_name, text, payload_version FROM messages ORDER BY id"
                )
                rows = await cursor.fetchall()

        self.assertEqual(migrated, 2)
        self.assertEqual(database._decrypt_message_payload(rows[0][1])["text"], "Старое сообщение")
        self.assertEqual(database._decrypt_message_payload(rows[1][1])["text"], "Новое старое сообщение")
        self.assertEqual([row[2] for row in rows], [1, 1])


if __name__ == "__main__":
    unittest.main()
