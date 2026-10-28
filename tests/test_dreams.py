import os
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

from cryptography.fernet import Fernet

os.environ.setdefault("TELEGRAM_TOKEN", "123456:test-token")
os.environ.setdefault("GROQ_API_KEY", "test-key")
os.environ.setdefault("HISTORY_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
os.environ.setdefault("ADMIN_ID", "1")

import database
from config import MOSCOW_TIMEZONE
from DEAD import (
    _generate_dead_story_audio,
    _generate_dead_story_audio_gtts,
    format_dead_countdown,
    is_dead_trigger,
)


class DeadTimerTests(unittest.IsolatedAsyncioTestCase):
    async def test_gtts_uses_russian_and_returns_mp3_bytes(self):
        with patch("gtts.gTTS") as gtts:
            gtts.return_value.write_to_fp.side_effect = (
                lambda audio_buffer: audio_buffer.write(b"mp3 audio")
            )
            result = await _generate_dead_story_audio_gtts("Дело было так.")

        self.assertEqual(result, b"mp3 audio")
        gtts.assert_called_once_with(
            text="Дело было так.",
            lang="ru",
            slow=False,
            timeout=(5, 15),
        )

    async def test_story_audio_prefers_online_tts(self):
        with (
            patch(
                "DEAD._generate_dead_story_audio_gtts",
                new=AsyncMock(return_value=b"MP3 audio"),
            ) as online_tts,
            patch(
                "DEAD._generate_dead_story_audio_local",
                new=AsyncMock(),
            ) as local_tts,
        ):
            result = await _generate_dead_story_audio("Дело было так.")

        self.assertEqual(result, b"MP3 audio")
        online_tts.assert_awaited_once_with("Дело было так.")
        local_tts.assert_not_awaited()

    async def test_story_audio_falls_back_when_online_tts_fails(self):
        with (
            patch(
                "DEAD._generate_dead_story_audio_gtts",
                new=AsyncMock(side_effect=RuntimeError("TTS unavailable")),
            ),
            patch(
                "DEAD._generate_dead_story_audio_local",
                new=AsyncMock(return_value=b"local audio"),
            ) as local_tts,
        ):
            result = await _generate_dead_story_audio("Дело было так.")

        self.assertEqual(result, b"local audio")
        local_tts.assert_awaited_once_with("Дело было так.")

    def test_countdown_uses_years_months_days_and_clock_units(self):
        start = datetime(2026, 1, 31, 12, 0, 0, tzinfo=MOSCOW_TIMEZONE)
        deadline = datetime(2028, 3, 31, 13, 2, 3, tzinfo=MOSCOW_TIMEZONE)

        result = format_dead_countdown(start, deadline)

        self.assertEqual(
            result,
            "2 года, 2 месяца, 0 дней, 1 час, 2 минуты, 3 секунды",
        )

    def test_text_trigger_matches_only_the_requested_phrases(self):
        self.assertTrue(is_dead_trigger("сколько мне осталось?"))
        self.assertTrue(is_dead_trigger("Сколько до конца моих дней"))
        self.assertFalse(is_dead_trigger("Сколько мне осталось сделать?"))

    async def test_timer_is_persistent_and_reward_is_awarded_only_once(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "dead.db"
            connection = sqlite3.connect(database_path)
            try:
                connection.executescript(
                    """
                    CREATE TABLE dead_timers (
                        user_id INTEGER PRIMARY KEY,
                        username TEXT,
                        chat_id INTEGER NOT NULL,
                        deadline REAL NOT NULL,
                        status TEXT NOT NULL DEFAULT 'counting',
                        prompt_message_id INTEGER,
                        response_deadline REAL
                    );
                    CREATE TABLE duel_players (
                        user_id INTEGER PRIMARY KEY,
                        username TEXT,
                        balance_cents INTEGER NOT NULL DEFAULT 500,
                        last_mined_on TEXT NOT NULL,
                        is_bot INTEGER NOT NULL DEFAULT 0
                    );
                    """
                )
                connection.commit()
            finally:
                connection.close()

            now = datetime.now(MOSCOW_TIMEZONE).timestamp()
            with patch.object(database, "DATABASE_PATH", database_path):
                first = await database.create_or_get_dead_timer(
                    77, "deadman", -100, now + 500
                )
                repeated = await database.create_or_get_dead_timer(
                    77, "deadman", -200, now + 900
                )
                await database.mark_dead_prompt_sent(
                    77,
                    message_id=15,
                    response_deadline=now + 1000,
                )
                result, balance = await database.fulfill_dead_timer(
                    77, "deadman", now + 800
                )
                repeated_result, repeated_balance = await database.fulfill_dead_timer(
                    77, "deadman", now + 800
                )

        self.assertEqual(first["deadline"], repeated["deadline"])
        self.assertEqual(first["chat_id"], repeated["chat_id"])
        self.assertEqual(result, "fulfilled")
        self.assertIsNone(balance)
        self.assertEqual(repeated_result, "fulfilled")
        self.assertIsNone(repeated_balance)


if __name__ == "__main__":
    unittest.main()
