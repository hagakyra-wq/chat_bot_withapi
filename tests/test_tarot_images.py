import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

import tarot_images
from tarot_deck import draw_spread


class TarotImageTests(unittest.TestCase):
    def test_dealt_cards_render_to_png_collage(self):
        collage = tarot_images.create_tarot_collage(draw_spread())

        with Image.open(collage) as image:
            self.assertEqual(image.format, "PNG")
            self.assertEqual(image.size, (804, 590))
        self.assertEqual(collage.name, "tarot-spread.png")

    def test_reversed_card_image_is_rotated(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            image_dir = Path(temporary_directory)
            source = Image.new("RGB", (2, 2))
            source.putpixel((0, 0), (255, 0, 0))
            source.putpixel((1, 0), (0, 255, 0))
            source.putpixel((0, 1), (0, 0, 255))
            source.putpixel((1, 1), (255, 255, 0))
            source.save(image_dir / "test.jpg", quality=100, subsampling=0)

            with patch.object(tarot_images, "TAROT_IMAGE_DIR", image_dir):
                normal = tarot_images._load_card_image(
                    {"image_file": "test.jpg", "reversed": False}
                )
                reversed_image = tarot_images._load_card_image(
                    {"image_file": "test.jpg", "reversed": True}
                )

        self.assertEqual(
            normal.getpixel((0, 0)),
            reversed_image.getpixel((219, 386)),
        )

    def test_collage_requires_exactly_three_cards(self):
        with self.assertRaises(ValueError):
            tarot_images.create_tarot_collage([])


if __name__ == "__main__":
    unittest.main()
