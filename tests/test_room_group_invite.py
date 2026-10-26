import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from cryptography.fernet import Fernet

os.environ.setdefault("TELEGRAM_TOKEN", "123456:test-token")
os.environ.setdefault("GROQ_API_KEY", "test-key")
os.environ.setdefault("HISTORY_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
os.environ.setdefault("ADMIN_ID", "1")

import mini_games


class RoomGroupInviteTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.room = {
            "room_code": "ABC234",
            "owner_id": 10,
            "max_players": 2,
            "status": "lobby",
            "state": {
                "players": [{"user_id": 10},],
            },
        }
        self.bot = SimpleNamespace(
            username="YubaraBot",
            send_message=AsyncMock(),
        )

    async def test_owner_can_invite_connected_group(self):
        with (
            patch(
                "mini_games.database.get_minigame_room",
                new=AsyncMock(side_effect=[self.room, self.room]),
            ),
            patch("mini_games._expire_room_if_needed", new=AsyncMock()),
            patch(
                "mini_games.database.list_user_group_memberships",
                new=AsyncMock(return_value=[-100123]),
            ),
            patch(
                "mini_games.database.is_group_allowed",
                new=AsyncMock(return_value=True),
            ),
        ):
            status, response = await mini_games.invite_room_to_group(
                "ABC234",
                10,
                -100123,
                self.bot,
            )

        self.assertEqual(status, 200)
        self.assertEqual(response["message"], "Приглашение отправлено в выбранную группу.")
        self.bot.send_message.assert_awaited_once()
        self.assertEqual(self.bot.send_message.await_args.kwargs["chat_id"], -100123)
        self.assertIn("Свободно мест: 1", self.bot.send_message.await_args.kwargs["text"])

    async def test_unconnected_group_is_rejected(self):
        with (
            patch(
                "mini_games.database.get_minigame_room",
                new=AsyncMock(side_effect=[self.room, self.room]),
            ),
            patch("mini_games._expire_room_if_needed", new=AsyncMock()),
            patch(
                "mini_games.database.list_user_group_memberships",
                new=AsyncMock(return_value=[]),
            ),
        ):
            status, response = await mini_games.invite_room_to_group(
                "ABC234",
                10,
                -100123,
                self.bot,
            )

        self.assertEqual(status, 403)
        self.assertIn("не подключена", response["error"])
        self.bot.send_message.assert_not_awaited()

    async def test_boolean_group_identifier_is_not_accepted_as_an_integer(self):
        status, response = await mini_games.invite_room_to_group(
            "ABC234",
            10,
            True,
            self.bot,
        )

        self.assertEqual(status, 400)
        self.assertIn("подключённую группу", response["error"])
        self.bot.send_message.assert_not_awaited()

    def test_photo_urls_are_limited_to_telegram_hosts(self):
        self.assertEqual(
            mini_games._valid_photo_url("https://t.me/i/userpic/photo.jpg"),
            "https://t.me/i/userpic/photo.jpg",
        )
        for url in (
            "http://t.me/i/userpic/photo.jpg",
            "https://t.me.example/i/userpic/photo.jpg",
            "https://user@t.me/i/userpic/photo.jpg",
        ):
            with self.subTest(url=url):
                self.assertIsNone(mini_games._valid_photo_url(url))


if __name__ == "__main__":
    unittest.main()
