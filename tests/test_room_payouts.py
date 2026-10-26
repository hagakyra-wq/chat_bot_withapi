import os
import unittest

from cryptography.fernet import Fernet

os.environ.setdefault("TELEGRAM_TOKEN", "123456:test-token")
os.environ.setdefault("GROQ_API_KEY", "test-key")
os.environ.setdefault("HISTORY_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
os.environ.setdefault("ADMIN_ID", "1")

from mini_games import _room_payouts


class RoomPayoutTests(unittest.TestCase):
    def _room(self, player_count):
        players = [
            {"user_id": user_id, "display_name": f"Player {user_id}"}
            for user_id in range(1, player_count + 1)
        ]
        return {
            "state": {
                "stake_cents": 100,
                "players": players,
                "game": {
                    "placement_order": list(range(1, player_count + 1)),
                    "players": players,
                },
            }
        }

    def test_two_player_winner_receives_net_pool(self):
        payouts = _room_payouts(self._room(2))

        self.assertEqual(payouts, [
            {"user_id": 1, "delta_cents": 190},
            {"user_id": 2, "delta_cents": 0},
        ])

    def test_three_player_pool_is_split_seventy_thirty(self):
        payouts = _room_payouts(self._room(3))

        self.assertEqual([item["delta_cents"] for item in payouts], [199, 86, 0])
        self.assertEqual(sum(item["delta_cents"] for item in payouts), 285)

    def test_four_player_pool_is_split_two_to_one(self):
        payouts = _room_payouts(self._room(4))

        self.assertEqual([item["delta_cents"] for item in payouts], [253, 127, 0, 0])
        self.assertEqual(sum(item["delta_cents"] for item in payouts), 380)

    def test_timeout_returns_ninety_five_percent_of_each_stake(self):
        payouts = _room_payouts(self._room(3), timeout=True)

        self.assertEqual([item["delta_cents"] for item in payouts], [95, 95, 95])


if __name__ == "__main__":
    unittest.main()
