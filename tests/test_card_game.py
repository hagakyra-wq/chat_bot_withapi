import unittest

from card_game import apply_action, create_game, forfeit_player


def _game(players):
    return {
        "players": players,
        "deck": [],
        "trump": {"rank": 9, "suit": "spades"},
        "trump_suit": "spades",
        "discard": [],
        "attacker_index": 0,
        "defender_index": 1,
        "stage": "attack",
        "table": [],
        "defender_limit": len(players[1]["hand"]),
        "status": "playing",
        "rounds_completed": 0,
        "winners": [],
        "losers": [],
        "loser_id": None,
        "finish_reason": None,
    }


class CardGameTests(unittest.TestCase):
    def test_new_game_deals_six_cards_and_uses_a_36_card_deck(self):
        for player_count in (2, 3, 4):
            with self.subTest(player_count=player_count):
                players = [
                    {"user_id": user_id, "display_name": f"Player {user_id}"}
                    for user_id in range(1, player_count + 1)
                ]

                game = create_game(players)

                self.assertEqual(
                    [len(player["hand"]) for player in game["players"]],
                    [6] * player_count,
                )
                self.assertEqual(
                    len(game["deck"]) + 1,
                    36 - 6 * player_count,
                )
                self.assertEqual(game["trump_suit"], game["trump"]["suit"])

    def test_player_can_defend_and_finish_a_round(self):
        game = _game(
            [
                {"user_id": 1, "hand": [{"rank": 6, "suit": "clubs"}]},
                {"user_id": 2, "hand": [{"rank": 8, "suit": "clubs"}]},
            ]
        )

        apply_action(game, 1, "play", card={"rank": 6, "suit": "clubs"})
        apply_action(game, 2, "defend", card={"rank": 8, "suit": "clubs"}, target=0)
        apply_action(game, 2, "finish")

        self.assertEqual(game["status"], "finished")
        self.assertEqual(game["winners"], [2])

    def test_player_can_take_cards(self):
        game = _game(
            [
                {"user_id": 1, "hand": [{"rank": 6, "suit": "clubs"}]},
                {"user_id": 2, "hand": [{"rank": 7, "suit": "hearts"}]},
            ]
        )

        apply_action(game, 1, "play", card={"rank": 6, "suit": "clubs"})
        apply_action(game, 2, "take")

        self.assertEqual(game["table"], [])
        self.assertEqual(len(game["players"][1]["hand"]), 2)
        self.assertEqual(game["attacker_index"], 0)

    def test_other_players_can_add_to_a_round(self):
        game = _game(
            [
                {"user_id": 1, "hand": [{"rank": 6, "suit": "clubs"}]},
                {"user_id": 2, "hand": [{"rank": 8, "suit": "clubs"}]},
                {"user_id": 3, "hand": [{"rank": 6, "suit": "hearts"}]},
            ]
        )
        game["defender_limit"] = 6

        apply_action(game, 1, "play", card={"rank": 6, "suit": "clubs"})
        apply_action(game, 2, "defend", card={"rank": 8, "suit": "clubs"}, target=0)
        apply_action(game, 3, "play", card={"rank": 6, "suit": "hearts"})

        self.assertEqual(len(game["table"]), 2)

    def test_invalid_defense_does_not_consume_the_card(self):
        game = _game(
            [
                {"user_id": 1, "hand": [{"rank": 6, "suit": "clubs"}]},
                {"user_id": 2, "hand": [{"rank": 7, "suit": "hearts"}]},
            ]
        )
        apply_action(game, 1, "play", card={"rank": 6, "suit": "clubs"})

        with self.assertRaises(ValueError):
            apply_action(game, 2, "defend", card={"rank": 7, "suit": "hearts"}, target=0)

        self.assertEqual(len(game["players"][1]["hand"]), 1)

    def test_forfeiting_players_are_ranked_after_remaining_players(self):
        players = [
            {"user_id": user_id, "display_name": f"Player {user_id}"}
            for user_id in (1, 2, 3)
        ]
        game = create_game(players)

        forfeit_player(game, 1)
        self.assertEqual(game["status"], "playing")
        forfeit_player(game, 2)

        self.assertEqual(game["status"], "finished")
        self.assertEqual(game["placement_order"], [3, 2, 1])

    def test_forfeited_player_cannot_take_another_turn(self):
        players = [
            {"user_id": user_id, "display_name": f"Player {user_id}"}
            for user_id in (1, 2, 3)
        ]
        game = create_game(players)
        forfeit_player(game, 1)

        with self.assertRaisesRegex(ValueError, "покинул"):
            apply_action(game, 1, "play", card={"rank": 6, "suit": "clubs"})

    def test_full_game_finishes_for_two_to_four_players(self):
        for player_count in (2, 3, 4):
            with self.subTest(player_count=player_count):
                players = [
                    {"user_id": user_id, "display_name": f"Player {user_id}"}
                    for user_id in range(1, player_count + 1)
                ]
                game = create_game(players)

                for _ in range(500):
                    if game["status"] == "finished":
                        break
                    if game["stage"] == "attack":
                        player = game["players"][game["attacker_index"]]
                        apply_action(
                            game,
                            player["user_id"],
                            "play",
                            card=player["hand"][0],
                        )
                    elif game["stage"] == "defend":
                        player = game["players"][game["defender_index"]]
                        apply_action(game, player["user_id"], "take")
                    else:
                        player = game["players"][game["defender_index"]]
                        apply_action(game, player["user_id"], "finish")

                self.assertEqual(game["status"], "finished")


if __name__ == "__main__":
    unittest.main()
