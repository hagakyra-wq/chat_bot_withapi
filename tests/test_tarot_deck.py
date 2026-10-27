import unittest

from tarot_deck import draw_spread


class TarotDeckTests(unittest.TestCase):
    def test_spread_has_three_distinct_cards_and_orientations(self):
        spread = draw_spread()

        self.assertEqual(len(spread), 3)
        self.assertEqual(len({card["name"] for card in spread}), 3)
        self.assertEqual(
            [card["position"] for card in spread],
            ["Прошлое", "Настоящее", "Возможное направление"],
        )
        self.assertTrue(all(type(card["reversed"]) is bool for card in spread))
        self.assertTrue(all(card["image_file"].endswith(".jpg") for card in spread))

    def test_deck_has_a_matching_image_for_every_card(self):
        spread = draw_spread()
        self.assertEqual(len({card["image_file"] for card in spread}), 3)


if __name__ == "__main__":
    unittest.main()
