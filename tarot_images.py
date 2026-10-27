"""Tarot card image loading and three-card collage rendering."""

from __future__ import annotations

from io import BytesIO
from functools import lru_cache
from pathlib import Path
from textwrap import wrap
from typing import Any

from PIL import Image, ImageDraw, ImageFont

TAROT_IMAGE_DIR = Path(__file__).resolve().parent / "taro_jpg"
_FONT_PATH = (
    Path(__file__).resolve().parent
    / "yubari_banner"
    / "fonts"
    / "DejaVuSans.ttf"
)
_CARD_SIZE = (220, 387)
_CARD_TOP = 126
_CARD_LEFT = 42
_CARD_GAP = 30
_CANVAS_SIZE = (3 * _CARD_SIZE[0] + 2 * _CARD_GAP + 2 * _CARD_LEFT, 590)


@lru_cache(maxsize=2)
def _font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(_FONT_PATH, size)


def _load_card_image(card: dict[str, Any]) -> Image.Image:
    image_path = TAROT_IMAGE_DIR / card["image_file"]
    with Image.open(image_path) as source:
        image = source.convert("RGB")
    image = image.resize(_CARD_SIZE, Image.Resampling.LANCZOS)
    if card["reversed"]:
        image = image.rotate(180)
    return image


def create_tarot_collage(cards: list[dict[str, Any]]) -> BytesIO:
    if len(cards) != 3:
        raise ValueError("Для коллажа нужен расклад ровно из трёх карт.")

    image = Image.new("RGB", _CANVAS_SIZE, "#171729")
    draw = ImageDraw.Draw(image)
    draw.text(
        (40, 30),
        "РАСКЛАД ТАРО",
        fill="#f3d58a",
        font=_font(28),
    )
    draw.line((40, 78, _CANVAS_SIZE[0] - 40, 78), fill="#78633f", width=2)

    for index, card in enumerate(cards):
        x = _CARD_LEFT + index * (_CARD_SIZE[0] + _CARD_GAP)
        draw.text((x, 96), card["position"].upper(), fill="#d8c9a2", font=_font(12))
        card_image = _load_card_image(card)
        image.paste(card_image, (x, _CARD_TOP))
        name = f"{card['name']}{' (перевёрнутая)' if card['reversed'] else ''}"
        for line_index, line in enumerate(wrap(name, width=28)):
            draw.text(
                (x, _CARD_TOP + _CARD_SIZE[1] + 12 + line_index * 18),
                line,
                fill="#f3d58a",
                font=_font(12),
            )

    output = BytesIO()
    image.save(output, format="PNG")
    output.seek(0)
    output.name = "tarot-spread.png"
    return output
