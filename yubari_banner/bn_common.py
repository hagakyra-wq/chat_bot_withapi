"""Common fonts, text helpers, seeds, palettes, and banner-level settings."""
import colorsys
import hashlib
import logging
import os
import time
import unicodedata
from functools import lru_cache

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

log = logging.getLogger("banner")

W, H = 1200, 630          # итоговый размер статичных баннеров
SS = 2                    # суперсэмплинг для гладких линий

HERE = os.path.dirname(os.path.abspath(__file__))
FONT_DIRS = [os.path.join(HERE, "fonts"), "/usr/share/fonts/truetype/dejavu",
             "/usr/share/fonts/dejavu", "/usr/share/fonts/TTF", "C:/Windows/Fonts"]
REG = ["DejaVuSans.ttf", "arial.ttf", "LiberationSans-Regular.ttf"]
BOLD = ["DejaVuSans-Bold.ttf", "arialbd.ttf", "LiberationSans-Bold.ttf"]
SYMBOLS = "NotoSansSymbols2-Regular.ttf"
EMOJI = "NotoColorEmoji.ttf"
EMOJI_FONT_SIZE = 109
_warned = False


@lru_cache(maxsize=64)
def font(kind, size):
    """Load a bundled font; Noto Color Emoji uses its single supported bitmap size."""
    global _warned
    if kind == "s":
        path = os.path.join(HERE, "fonts", SYMBOLS)
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    if kind == "e":
        path = os.path.join(HERE, "fonts", EMOJI)
        if os.path.exists(path):
            return ImageFont.truetype(path, EMOJI_FONT_SIZE)
    for d in FONT_DIRS:
        names = BOLD if kind == "b" else REG
        for n in names:
            p = os.path.join(d, n)
            if os.path.exists(p):
                return ImageFont.truetype(p, size)
    if not _warned:
        log.warning("banner: шрифты не найдены, положи fonts/DejaVuSans*.ttf рядом с модулем")
        _warned = True
    try:
        return ImageFont.load_default(size)
    except TypeError:
        return ImageFont.load_default()


# ---------------------------------------------------------------- уровни
# colors - сколько акцентных цветов в палитре; остальное - насыщенность декора
LEVELS = {
    1: dict(name="Обычный",     colors=(2, 2), blobs=(2, 3), ribbons=(0, 0), bokeh=(0, 0),   dots=(10, 18),  stars=(0, 0),   pattern=False),
    2: dict(name="Необычный",   colors=(3, 3), blobs=(3, 4), ribbons=(0, 1), bokeh=(5, 8),   dots=(25, 40),  stars=(2, 4),   pattern=False),
    3: dict(name="Редкий",      colors=(4, 4), blobs=(4, 5), ribbons=(1, 2), bokeh=(8, 12),  dots=(35, 55),  stars=(4, 6),   pattern=True),
    4: dict(name="Эпический",   colors=(5, 6), blobs=(4, 7), ribbons=(2, 4), bokeh=(14, 24), dots=(40, 80),  stars=(5, 10),  pattern=True),
    5: dict(name="Элитный",     colors=(7, 8), blobs=(5, 8), ribbons=(3, 5), bokeh=(18, 28), dots=(60, 100), stars=(8, 14),  pattern=True),
    6: dict(name="Легендарный", colors=(5, 6), blobs=(4, 6), ribbons=(2, 3), bokeh=(0, 0),   dots=(0, 0),    stars=(0, 0),   pattern=False),
}

TITLES = [
    "Особая королевская декорация", "Подданный с высочайшего одобрения",
    "Гость тронного зала", "Под личным покровительством Юбари",
    "Обладатель милости её величества", "Записан в королевские хроники",
    "Допущен к аудиенции", "Фаворит придворного круга",
    "Хранитель малой печати", "Почётный гость дворцовых садов",
]
TRAITS = ["ВЕРНОСТЬ", "ДЕРЗОСТЬ", "ХАРИЗМА", "ТЕРПЕНИЕ", "ХИТРОСТЬ", "ТАЛАНТ",
          "ЛОЯЛЬНОСТЬ", "ИНТУИЦИЯ", "ОТВАГА", "СТИЛЬ", "ДУША", "ВОЛЯ"]


# ---------------------------------------------------------------- seed / текст
def make_digest(user_id, salt="yubari", fresh=False, item_seed=None):
    """sha256-seed. item_seed - у каждого предмета инвентаря свой (новый выигрыш = новый баннер)."""
    src = f"{salt}:{user_id}"
    if item_seed is not None:
        src += f":{item_seed}"
    if fresh:
        src += f":{time.time_ns()}:{os.urandom(4).hex()}"
    return hashlib.sha256(src.encode()).hexdigest()


def clean_text(s, limit=22):
    """Keep printable Unicode, including combining marks and emoji joiners."""
    s = unicodedata.normalize("NFC", s or "")
    out = [
        ch
        for ch in s
        if unicodedata.category(ch)[0] in "LNMPSZ"
        or ch in " _-.'\u200d\ufe0e\ufe0f"
        or 0xE0020 <= ord(ch) <= 0xE007F
    ]
    s = " ".join("".join(out).split())
    if len(s) <= limit:
        return s
    prefix = ""
    for unit in _text_units(s):
        if len(prefix) + len(unit) > max(0, limit - 1):
            break
        prefix += unit
    return prefix + "…"


def content_hash(name, tag=None, avatar_uid=None):
    """Хеш того, что зашито в картинку (ник + тег + аватарка). Изменился - баннер устарел."""
    raw = f"{clean_text(name)}|{clean_text(tag, 20).lower() if tag else ''}|{avatar_uid or ''}"
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


# ---------------------------------------------------------------- цвет
def hsv(h, s, v):
    r, g, b = colorsys.hsv_to_rgb(h % 1.0, max(0, min(1, s)), max(0, min(1, v)))
    return (int(r * 255), int(g * 255), int(b * 255))


def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


SCHEMES = {"analog": [0, .08, -.08], "triad": [0, .33, .66],
           "split": [0, .42, .58], "mono": [0, .03, -.03]}


def palette(rng, n=3, hue=None):
    """-> (bg1, bg2, [n акцентов]). n=2 для обычного, 7-8 для элитного."""
    h = rng.random() if hue is None else hue
    scheme = rng.choice(list(SCHEMES))
    base = SCHEMES[scheme]
    offs = list(base)
    spread = .05 if scheme == "mono" else .5
    while len(offs) < n:
        offs.append(rng.uniform(-spread, spread))
    offs = offs[:n]
    acc = [hsv(h + o, rng.uniform(.45, .8), rng.uniform(.85, 1)) for o in offs]
    bg1 = hsv(h, rng.uniform(.65, .9), rng.uniform(.07, .14))
    bg2 = hsv(h + base[1], rng.uniform(.65, .95), rng.uniform(.20, .34))
    return bg1, bg2, acc


# ---------------------------------------------------------------- рисование
def glow(layer, radius, strength=1.0):
    g = layer.filter(ImageFilter.GaussianBlur(radius))
    if strength != 1.0:
        g.putalpha(g.getchannel("A").point(lambda v: min(255, int(v * strength))))
    return g


def spaced_len(draw, text, f, spacing):
    units = _text_units(text)
    if not units:
        return 0
    width = sum(_unit_width(draw, unit, f) for unit in units)
    return width + spacing * (len(units) - 1)


def text_length(text, f):
    """Measure Unicode text using the same fallback fonts as put_text()."""
    return spaced_len(ImageDraw.Draw(Image.new("L", (1, 1))), text, f, 0)


def _is_emoji_base(char):
    codepoint = ord(char)
    return (
        0x1F000 <= codepoint <= 0x1FAFF
        or 0x2600 <= codepoint <= 0x27BF
        or codepoint in {0x00A9, 0x00AE, 0x203C, 0x2049, 0x2122, 0x2139, 0x3030, 0x303D, 0x3297, 0x3299}
    )


def _text_units(text):
    units = []
    for char in text:
        codepoint = ord(char)
        category = unicodedata.category(char)
        if (
            units
            and (
                category.startswith("M")
                or char == "\u200d"
                or units[-1].endswith("\u200d")
                or 0x1F3FB <= codepoint <= 0x1F3FF
                or (
                    0x1F1E6 <= codepoint <= 0x1F1FF
                    and len(units[-1]) == 1
                    and 0x1F1E6 <= ord(units[-1][0]) <= 0x1F1FF
                )
            )
        ):
            units[-1] += char
        else:
            units.append(char)
    return units


def _unit_font(unit, base_font):
    if any(_is_emoji_base(char) for char in unit):
        emoji_font = font("e", EMOJI_FONT_SIZE)
        if _emoji_glyph_exists(unit, emoji_font):
            return emoji_font, True
    if any(
        unicodedata.category(char).startswith("S")
        or 0x1D000 <= ord(char) <= 0x1D7FF
        for char in unit
    ):
        return font("s", int(getattr(base_font, "size", 20))), False
    return base_font, False


@lru_cache(maxsize=512)
def _emoji_glyph_exists(unit, emoji_font):
    bounds = emoji_font.getbbox(unit)
    width = max(1, bounds[2] - bounds[0])
    height = max(1, bounds[3] - bounds[1])
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    ImageDraw.Draw(image).text(
        (-bounds[0], -bounds[1]),
        unit,
        font=emoji_font,
        fill=(255, 255, 255, 255),
        embedded_color=True,
    )
    return image.getchannel("A").getbbox() is not None


def _unit_width(draw, unit, base_font):
    selected_font, is_emoji = _unit_font(unit, base_font)
    if is_emoji:
        size = int(getattr(base_font, "size", 20))
        return selected_font.getlength(unit) * size / EMOJI_FONT_SIZE
    return draw.textlength(unit, font=selected_font)


def put_text(out, xy, text, f, col, spacing=0):
    """Текст с правильным сглаживанием на прозрачном RGBA-слое (без тёмной каймы).
    col = (r, g, b, a)."""
    x, y = int(xy[0]), int(xy[1])
    probe = ImageDraw.Draw(Image.new("L", (4, 4)))
    units = _text_units(text)
    w = int(spaced_len(probe, text, f, spacing)) + 12
    h = int(getattr(f, "size", 20) * 1.5) + 4
    m = Image.new("L", (w, h), 0)
    md = ImageDraw.Draw(m)
    cx = 4.0
    for unit in units:
        selected_font, is_emoji = _unit_font(unit, f)
        if is_emoji:
            bounds = selected_font.getbbox(unit)
            emoji_image = Image.new(
                "RGBA",
                (max(1, bounds[2] - bounds[0]), max(1, bounds[3] - bounds[1])),
                (0, 0, 0, 0),
            )
            ImageDraw.Draw(emoji_image).text(
                (-bounds[0], -bounds[1]),
                unit,
                font=selected_font,
                fill=(255, 255, 255, 255),
                embedded_color=True,
            )
            emoji = emoji_image.getchannel("A")
            scale = int(getattr(f, "size", 20)) / EMOJI_FONT_SIZE
            emoji = emoji.resize(
                (
                    max(1, round(emoji.width * scale)),
                    max(1, round(emoji.height * scale)),
                ),
                Image.Resampling.LANCZOS,
            )
            m.paste(emoji, (round(cx), 2))
            cx += emoji.width + spacing
        else:
            md.text((cx, 2), unit, font=selected_font, fill=255)
            cx += md.textlength(unit, font=selected_font) + spacing
    a = col[3] if len(col) > 3 else 255
    if a < 255:
        m = m.point(lambda v: v * a // 255)
    lay = Image.new("RGBA", (w, h), tuple(col[:3]) + (0,))
    lay.putalpha(m)
    out.alpha_composite(lay, (x - 4, y - 2)) if x - 4 >= 0 and y - 2 >= 0 else \
        out.paste(lay, (x - 4, y - 2), lay)
    return cx - 4 - spacing
