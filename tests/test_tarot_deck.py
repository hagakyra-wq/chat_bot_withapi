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


if __name__ == "__main__":
    unittest.main()
