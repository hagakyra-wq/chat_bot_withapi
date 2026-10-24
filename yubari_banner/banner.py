"""
banner.py - движок баннеров профиля «Юбари», уровни 1-5 (PNG) + вход в уровень 6 (GIF).

    from banner import make_banner
    buf = make_banner(user_id, name, tag, level=3, item_seed=42)     # BytesIO, buf.name = banner.png
    buf = make_banner(user_id, name, tag, level=6, item_seed=7)      # BytesIO, buf.name = banner.gif

Уровень 5 и 6 ставят аватарку в рамку: avatar=bytes | PIL.Image | file-like (или None - тогда инициал).
Seed = sha256(salt:user_id[:item_seed]). Новый выигрыш = новый item_seed = новый баннер.
"""
import io
import math
import random

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageOps

from . import frames
from .bn_common import (H, LEVELS, SS, TITLES, TRAITS, W, clean_text, content_hash,  # noqa: F401
                        font, glow, lerp, make_digest, palette, put_text, spaced_len)

PAD, RADIUS = 44, 34
EX, EY, ER = W - 255, H // 2 - 8, 148          # положение и размер эмблемы


# ---------------------------------------------------------------- фон
def _background(rng, bg1, bg2, acc, cfg):
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    t = (yy / H * .7 + xx / W * .3)[..., None]
    c1, c2 = np.array(bg1, np.float32), np.array(bg2, np.float32)
    img = c1 + (c2 - c1) * t
    for _ in range(rng.randint(*cfg["blobs"])):                     # «аврора»-пятна
        cx, cy = rng.uniform(0, W), rng.uniform(0, H)
        rx, ry = rng.uniform(180, 520), rng.uniform(120, 340)
        ang = rng.uniform(0, math.pi)
        dx, dy = xx - cx, yy - cy
        u = dx * math.cos(ang) + dy * math.sin(ang)
        v = -dx * math.sin(ang) + dy * math.cos(ang)
        m = np.exp(-((u / rx) ** 2 + (v / ry) ** 2))[..., None]
        img += m * np.array(rng.choice(acc), np.float32) * rng.uniform(.18, .42)
    for _ in range(rng.randint(*cfg["ribbons"])):                   # волнистые ленты
        amp, fr = rng.uniform(30, 90), rng.uniform(.002, .006)
        ph, base = rng.uniform(0, 6.28), rng.uniform(.2, .85) * H
        wy = base + amp * np.sin(xx * fr + ph) + amp * .4 * np.sin(xx * fr * 2.3 + ph * 1.7)
        band = np.exp(-((yy - wy) / rng.uniform(14, 40)) ** 2)[..., None]
        img += band * np.array(rng.choice(acc), np.float32) * rng.uniform(.10, .22)
    vig = 1 - .45 * (((xx - W / 2) / (W / 2)) ** 2 + ((yy - H / 2) / (H / 2)) ** 2)[..., None] ** 1.2
    img *= np.clip(vig, .45, 1)
    img += np.random.default_rng(rng.randint(0, 2 ** 31)).normal(0, 3.2, (H, W, 1))
    return Image.fromarray(np.clip(img, 0, 255).astype(np.uint8), "RGB").convert("RGBA")


def _pattern(rng, acc):
    """Тонкий узор на фоне (уровень 3+): решётка, точки, дуги или соты."""
    lay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(lay)
    col = rng.choice(acc) + (30,)
    kind = rng.choice(["lattice", "dots", "arcs", "hex"])
    if kind == "lattice":
        st = rng.choice([48, 64, 80])
        for i in range(-H, W, st):
            d.line([(i, 0), (i + H, H)], fill=col, width=1)
            d.line([(i, H), (i + H, 0)], fill=col, width=1)
    elif kind == "dots":
        for x in range(0, W, 30):
            for y in range(0, H, 30):
                d.ellipse([x - 1.6, y - 1.6, x + 1.6, y + 1.6], fill=col)
    elif kind == "arcs":
        cx, cy = rng.choice([(0, 0), (W, 0), (0, H), (W, H)])
        for r in range(60, 1500, 55):
            d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=col, width=1)
    else:
        R = 34
        for j in range(-1, int(H / (R * 1.732)) + 2):
            for i in range(-1, int(W / (R * 1.5)) + 2):
                cx = i * R * 1.5
                cy = j * R * 1.732 + (R * .866 if i % 2 else 0)
                d.polygon([(cx + R * math.cos(k * math.pi / 3), cy + R * math.sin(k * math.pi / 3))
                           for k in range(6)], outline=col)
    return lay


def _star(d, cx, cy, r, fill):
    k = r * .18
    d.polygon([(cx, cy - r), (cx + k, cy - k), (cx + r, cy), (cx + k, cy + k),
               (cx, cy + r), (cx - k, cy + k), (cx - r, cy), (cx - k, cy - k)], fill=fill)


def _bokeh(rng, acc, n):
    if n <= 0:
        return Image.new("RGBA", (W, H), (0, 0, 0, 0))
    S = (W * SS, H * SS)
    soft, ring = Image.new("RGBA", S, (0, 0, 0, 0)), Image.new("RGBA", S, (0, 0, 0, 0))
    ds, dr = ImageDraw.Draw(soft), ImageDraw.Draw(ring)
    for _ in range(n):
        r = rng.choice([rng.uniform(6, 20), rng.uniform(20, 55), rng.uniform(55, 110)]) * SS
        cx, cy = rng.uniform(0, W) * SS, rng.uniform(0, H) * SS
        col = rng.choice(acc)
        if rng.random() < .55:
            ds.ellipse([cx - r, cy - r, cx + r, cy + r], fill=col + (rng.randint(18, 55),))
        else:
            dr.ellipse([cx - r, cy - r, cx + r, cy + r], outline=col + (rng.randint(60, 150),),
                       width=max(1, int(rng.uniform(1, 2.6) * SS)))
    soft = soft.filter(ImageFilter.GaussianBlur(3 * SS))
    out = Image.alpha_composite(Image.alpha_composite(soft, glow(ring, 6 * SS, 1.2)), ring)
    return out.resize((W, H), Image.LANCZOS)


def _sparkles(rng, nd, ns):
    S = (W * SS, H * SS)
    lay = Image.new("RGBA", S, (0, 0, 0, 0))
    d = ImageDraw.Draw(lay)
    for _ in range(nd):
        x, y = rng.uniform(0, W) * SS, rng.uniform(0, H) * SS
        d.ellipse([x - SS, y - SS, x + SS, y + SS], fill=(255, 255, 255, rng.randint(70, 200)))
    for _ in range(ns):
        _star(d, rng.uniform(0, W) * SS, rng.uniform(0, H) * SS, rng.uniform(8, 20) * SS, (255, 255, 255, 230))
    return Image.alpha_composite(glow(lay, 5 * SS, 1.6), lay).resize((W, H), Image.LANCZOS)


# ---------------------------------------------------------------- центр: печать / эмблема / аватарка
def _rose(d, cx, cy, amp, n, m, rot, col, w):
    k = n / m
    pts = []
    for s in range(1401):
        th = m * 2 * math.pi * s / 1400
        r = amp * math.cos(k * th)
        pts.append((cx + r * math.cos(th + rot), cy + r * math.sin(th + rot)))
    d.line(pts, fill=col + (235,), width=w, joint="curve")


def _seal(rng, acc, cx, cy, R, level, name):
    """Печать для уровней 1-3: кольца + инициал (+ узор на 3)."""
    lay = Image.new("RGBA", (W * SS, H * SS), (0, 0, 0, 0))
    d = ImageDraw.Draw(lay)
    cx, cy, R = cx * SS, cy * SS, R * SS
    A = lambda i: acc[i % len(acc)]
    d.ellipse([cx - R, cy - R, cx + R, cy + R], outline=A(0) + (230,), width=3 * SS)
    d.ellipse([cx - R * .9, cy - R * .9, cx + R * .9, cy + R * .9], outline=A(1) + (120,), width=SS)
    if level >= 2:
        for i in range(36):
            a = 2 * math.pi * i / 36
            l = .06 if i % 3 else .1
            d.line([(cx + math.cos(a) * R, cy + math.sin(a) * R),
                    (cx + math.cos(a) * R * (1 + l), cy + math.sin(a) * R * (1 + l))], fill=A(0) + (170,), width=SS)
        for i in range(12):
            a = 2 * math.pi * i / 12
            r = 4 * SS
            x, y = cx + math.cos(a) * R * .78, cy + math.sin(a) * R * .78
            d.ellipse([x - r, y - r, x + r, y + r], fill=A(2) + (200,))
    if level == 3:
        n, m = rng.choice([(5, 2), (7, 2), (4, 1), (6, 1)])
        _rose(d, cx, cy, R * .66, n, m, rng.uniform(0, 6.28), A(1), int(1.8 * SS))
        _star(d, cx, cy, R * .18, (255, 255, 255, 240))
    lay = Image.alpha_composite(glow(lay, 9 * SS, 1.5), lay).resize((W, H), Image.LANCZOS)
    if level < 3:
        ch = (clean_text(name, 3) or "Ю")[0].upper()
        f = font("b", int(R / SS * .95))
        probe = ImageDraw.Draw(lay)
        tw = probe.textlength(ch, font=f)
        txt = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        put_text(txt, (cx / SS - tw / 2, cy / SS - R / SS * .6), ch, f, (255, 255, 255, 240))
        lay = Image.alpha_composite(lay, Image.alpha_composite(glow(txt, 8, 1.4), txt))
    return lay


def _draw_crown(d, cx, cy, R, A):
    cw, ch = R * .46, R * .30                                       # корона
    by = cy + ch * .35
    pts = [(cx - cw, by), (cx - cw * .95, cy - ch * .55), (cx - cw * .5, cy - ch * .05), (cx, cy - ch * .85),
           (cx + cw * .5, cy - ch * .05), (cx + cw * .95, cy - ch * .55), (cx + cw, by)]
    gold = lerp(A(0), (255, 236, 170), .72)
    d.polygon(pts, fill=gold + (255,))
    d.rectangle([cx - cw, by, cx + cw, by + ch * .16], fill=lerp(gold, (255, 255, 255), .25) + (255,))
    for px, py in [pts[1], pts[3], pts[5]]:
        d.ellipse([px - ch * .1, py - ch * .1, px + ch * .1, py + ch * .1], fill=(255, 255, 255, 255))
    for gx in (-.5, 0, .5):
        gem = lerp(A(int(gx * 2) + 1), (255, 255, 255), .2)
        d.ellipse([cx + cw * gx - ch * .08, by + ch * .01, cx + cw * gx + ch * .08, by + ch * .15], fill=gem + (255,))


def _crown_layer(acc, cx, cy, R):
    """Корона отдельным слоем: в анимации эмблема вращается, а корона стоит на месте."""
    lay = Image.new("RGBA", (W * SS, H * SS), (0, 0, 0, 0))
    _draw_crown(ImageDraw.Draw(lay), cx * SS, cy * SS, R * SS, lambda i: acc[i % len(acc)])
    return Image.alpha_composite(glow(lay, 10 * SS, 1.7), lay).resize((W, H), Image.LANCZOS)


def _emblem(rng, acc, cx, cy, R, crown=True):
    """Сигила уровня 4 и 6: кольца, засечки, 2-3 розы Гранди, лучи, корона."""
    lay = Image.new("RGBA", (W * SS, H * SS), (0, 0, 0, 0))
    d = ImageDraw.Draw(lay)
    cx, cy, R = cx * SS, cy * SS, R * SS
    A = lambda i: acc[i % len(acc)]
    for i, (k, w) in enumerate([(1.0, 3), (.92, 1), (.62, 1)]):
        d.ellipse([cx - R * k, cy - R * k, cx + R * k, cy + R * k],
                  outline=A(i) + (210 if i == 0 else 120,), width=w * SS)
    ticks = rng.choice([24, 36, 48, 60])
    for i in range(ticks):
        a = 2 * math.pi * i / ticks
        l = .06 if i % 4 else .11
        d.line([(cx + math.cos(a) * R, cy + math.sin(a) * R),
                (cx + math.cos(a) * R * (1 + l), cy + math.sin(a) * R * (1 + l))], fill=A(0) + (170,), width=SS)
    for li in range(rng.randint(2, 3)):
        n, m = rng.choice([(3, 1), (5, 2), (4, 1), (7, 2), (5, 1), (8, 3), (6, 1), (9, 4)])
        _rose(d, cx, cy, R * (.88 - li * .17), n, m, rng.uniform(0, 6.28), A(li), int(1.8 * SS))
    n = rng.choice([8, 12, 16])
    for i in range(n):
        a = 2 * math.pi * i / n + .1
        d.line([(cx + math.cos(a) * R * .12, cy + math.sin(a) * R * .12),
                (cx + math.cos(a) * R * .34, cy + math.sin(a) * R * .34)], fill=(255, 255, 255, 190), width=SS)
    if crown:
        _draw_crown(d, cx, cy, R, A)
    return Image.alpha_composite(glow(lay, 10 * SS, 1.7), lay).resize((W, H), Image.LANCZOS)


def _load_avatar(avatar):
    if avatar is None:
        return None
    try:
        if isinstance(avatar, Image.Image):
            return avatar.convert("RGB")
        if isinstance(avatar, (bytes, bytearray)):
            avatar = io.BytesIO(avatar)
        return Image.open(avatar).convert("RGB")
    except Exception:
        return None


def _medallion(rng, acc, avatar, name, cx, cy, R, level):
    """Уровни 5-6: аватарка (или инициал) в круглой рамке с камнями. Аватарка нигде не хранится."""
    A = lambda i: acc[i % len(acc)]
    lay = Image.new("RGBA", (W * SS, H * SS), (0, 0, 0, 0))
    d = ImageDraw.Draw(lay)
    sx, sy, sR = cx * SS, cy * SS, R * SS
    for k, w, c, a in ((1.0, 5, 0, 240), (.93, 2, 1, 170), (.64 + .18, 3, 2, 230)):
        d.ellipse([sx - sR * k, sy - sR * k, sx + sR * k, sy + sR * k], outline=A(c) + (a,), width=w * SS)
    for i in range(72):
        a = 2 * math.pi * i / 72
        l = .05 if i % 3 else .1
        d.line([(sx + math.cos(a) * sR * 1.0, sy + math.sin(a) * sR * 1.0),
                (sx + math.cos(a) * sR * (1 + l), sy + math.sin(a) * sR * (1 + l))], fill=A(0) + (180,), width=SS)
    from .frames import _gem
    for i in range(8):
        a = 2 * math.pi * i / 8 + math.pi / 8
        _gem(d, sx + math.cos(a) * sR * 1.0, sy + math.sin(a) * sR * 1.0, (11 if i % 2 else 8) * SS, A(i + 2))
    lay = Image.alpha_composite(glow(lay, 9 * SS, 1.5), lay).resize((W, H), Image.LANCZOS)

    ar = int(R * .8)
    av = _load_avatar(avatar)
    if av is not None:
        disc = ImageOps.fit(av, (2 * ar, 2 * ar), Image.LANCZOS)
    else:                                                           # нет аватарки - инициал
        yy, xx = np.mgrid[0:2 * ar, 0:2 * ar].astype(np.float32)
        t = (np.sqrt((xx - ar) ** 2 + (yy - ar) ** 2) / ar)[..., None]
        c0, c1 = np.array(lerp(A(0), (0, 0, 0), .35), np.float32), np.array(lerp(A(1), (0, 0, 0), .7), np.float32)
        disc = Image.fromarray((c0 + (c1 - c0) * np.clip(t, 0, 1)).astype(np.uint8), "RGB")
        ch = (clean_text(name, 3) or "Ю")[0].upper()
        f = font("b", int(ar * 1.15))
        dd = ImageDraw.Draw(disc)
        try:
            dd.text((ar, ar), ch, font=f, fill=(255, 255, 255), anchor="mm")
        except Exception:
            dd.text((ar * .6, ar * .4), ch, font=f, fill=(255, 255, 255))
    k = 4
    mask = Image.new("L", (2 * ar * k, 2 * ar * k), 0)
    ImageDraw.Draw(mask).ellipse([0, 0, 2 * ar * k - 1, 2 * ar * k - 1], fill=255)
    mask = mask.resize((2 * ar, 2 * ar), Image.LANCZOS)
    lay.paste(disc, (int(cx - ar), int(cy - ar)), mask)
    return lay


# ---------------------------------------------------------------- карточка
def _glass(base, box, radius, tint):
    """Стеклянная карточка: размытие фона под ней + затемнение + тинт (без рамки - рамку рисует frames)."""
    x0, y0, x1, y1 = box
    crop = base.crop(box).filter(ImageFilter.GaussianBlur(18)).convert("RGBA")
    crop = Image.alpha_composite(crop, Image.new("RGBA", crop.size, (8, 8, 18, 120)))
    crop = Image.alpha_composite(crop, Image.new("RGBA", crop.size, tint + (26,)))
    mask = Image.new("L", (crop.width * SS, crop.height * SS), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, mask.width - 1, mask.height - 1], radius * SS, fill=255)
    base.paste(crop, (x0, y0), mask.resize(crop.size, Image.LANCZOS))
    return base


def card_flat(box, radius, tint, left=170, mid=150, right=85):
    """Полупрозрачная тёмная карточка для анимации: слева плотнее (читаемый текст), справа видна сцена."""
    m = Image.new("L", (W * SS, H * SS), 0)
    ImageDraw.Draw(m).rounded_rectangle([v * SS for v in box], radius * SS, fill=255)
    m = np.asarray(m.resize((W, H), Image.LANCZOS), np.float32) / 255
    gx = np.interp(np.arange(W), [0, W * .5, W], [left, mid, right]).astype(np.float32)
    a = (m * gx[None, :]).astype(np.uint8)
    rgb = np.empty((H, W, 3), np.uint8)
    rgb[:] = lerp((6, 6, 16), tint, .12)
    lay = Image.fromarray(rgb, "RGB").convert("RGBA")
    lay.putalpha(Image.fromarray(a, "L"))
    return lay


# ---------------------------------------------------------------- текст
def _text_layer(rng, acc, name, tag, level, digest, show_name=True):
    A = lambda i: acc[i % len(acc)]
    if not show_name:
        return Image.new("RGBA", (W, H), (0, 0, 0, 0))
    name = clean_text(name) or "Безымянный"
    tag = clean_text(tag, 20).lower().replace(" ", "_") if tag else None
    lx = PAD + 56
    max_w = EX - ER - 40 - lx
    out = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    probe = ImageDraw.Draw(out)

    size = 92
    f_name = font("b", size)
    while probe.textlength(name, font=f_name) > max_w and size > 36:
        size -= 3
        f_name = font("b", size)
    ny = 180 + (92 - size) // 2
    name_w = probe.textlength(name, font=f_name)

    gl = Image.new("RGBA", (W, H), (0, 0, 0, 0))                     # свечение имени
    ImageDraw.Draw(gl).text((lx, ny), name, font=f_name, fill=A(0) + (255,))
    out = Image.alpha_composite(out, glow(gl, 14, 1.3))

    mask = Image.new("L", (W, H), 0)                                 # градиентная заливка имени
    ImageDraw.Draw(mask).text((lx, ny), name, font=f_name, fill=255)
    y0, y1, x1 = int(ny), min(H, int(ny + size * 1.4)), min(W, int(lx + name_w) + 2)
    t = np.clip((np.arange(y0, y1) - ny) / (size * 1.2), 0, 1)[:, None, None]
    top, bot = np.array((255, 255, 255), np.float32), np.array(lerp(A(0), (255, 255, 255), .45), np.float32)
    rgb = ((top + (bot - top) * t) * np.ones((1, x1 - lx, 1), np.float32)).astype(np.uint8)
    gim = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gim.paste(Image.fromarray(rgb, "RGB"), (lx, y0))
    gim.putalpha(mask)
    out = Image.alpha_composite(out, gim)

    d = ImageDraw.Draw(out)
    f_lab = font("b", 21)
    label = "ПРОФИЛЬ ЮБАРЫ"
    lw = put_text(out, (lx, 128), label, f_lab, A(0) + (255,), 6)
    d.line([(lx + lw + 20, 140), (lx + lw + 110, 140)], fill=A(1) + (200,), width=2)

    cur_y = ny + int(size * 1.22)
    if tag:
        put_text(out, (lx + 2, cur_y), "@" + tag, font("r", 30), lerp(A(1), (255, 255, 255), .35) + (255,))
        cur_y += 52
    else:
        cur_y += 8
    for x in range(int(max_w)):                                      # разделитель
        u = x / max_w
        d.line([(lx + x, cur_y + 10), (lx + x, cur_y + 12)], fill=lerp(A(0), A(2), u) + (int(255 * (1 - u) ** .8),))
    d.ellipse([lx - 5, cur_y + 5, lx + 5, cur_y + 17], fill=(255, 255, 255, 255))

    put_text(out, (lx, cur_y + 34), rng.choice(TITLES), font("r", 27), (232, 230, 250, 235))

    f_chip = font("b", 15)                                           # чипы-качества
    cx_, cy_ = lx, cur_y + 86
    for i, tr in enumerate(rng.sample(TRAITS, 3)):
        tw = spaced_len(probe, tr, f_chip, 2)
        chip = (int(cx_), cy_, int(cx_ + tw + 30), cy_ + 32)
        col = A(i)
        d.rounded_rectangle(chip, 16, fill=col + (38,), outline=col + (200,), width=1)
        put_text(out, (chip[0] + 15, chip[1] + 7), tr, f_chip, (255, 255, 255, 235), 2)
        cx_ = chip[2] + 12

    f_id = font("r", 15)                                             # уровень и номер
    idtxt = f"{LEVELS[level]['name'].upper()}  ·  № {int(digest[:8], 16) % 10 ** 6:06d}"
    wid = spaced_len(probe, idtxt, f_id, 1)
    put_text(out, (W - PAD - 28 - wid, PAD + 22), idtxt, f_id, (255, 255, 255, 170), 1)

    code = "SIGIL " + digest[:4].upper() + "·" + digest[4:8].upper()   # код под эмблемой
    wc = spaced_len(probe, code, font("r", 15), 3)
    put_text(out, (EX - wc / 2, EY + ER + 26), code, font("r", 15), (255, 255, 255, 150), 3)
    return out


# ---------------------------------------------------------------- сборка частей
def build_parts(digest, name, tag, level, avatar=None, hue=None, show_name=True):
    """Всё, что общее для PNG и GIF: палитра, рамка, текст, центральный элемент."""
    rng = random.Random(int(digest, 16))
    cfg = LEVELS[level]
    bg1, bg2, acc = palette(rng, rng.randint(*cfg["colors"]), hue)
    box = (PAD, PAD, W - PAD, H - PAD)
    frame, fmask = frames.draw_frame(rng, level, box, RADIUS, acc)
    text = _text_layer(rng, acc, name, tag, level, digest, show_name=show_name)
    rotating, crown = False, None
    if level == 5 or (level == 6 and avatar is not None):
        center = _medallion(rng, acc, avatar, name, EX, EY, ER, level)
    elif level in (4, 6):
        center, rotating = _emblem(rng, acc, EX, EY, ER, crown=(level == 4)), level == 6
        if level == 6:
            crown = _crown_layer(acc, EX, EY, ER)
    else:
        center = _seal(rng, acc, EX, EY, ER, level, name)
    return dict(rng=rng, digest=digest, level=level, cfg=cfg, bg1=bg1, bg2=bg2, acc=acc, box=box,
                frame=frame, fmask=fmask, text=text, center=center, rotating=rotating, crown=crown,
                geom=(EX, EY, ER), radius=RADIUS, pad=PAD)


def make_banner(user_id, name, tag=None, level=1, salt="yubari", fresh=False, as_bytes=True,
                avatar=None, item_seed=None, preset=None, frames_n=30, fps=20,
                show_name=True):
    """Единая точка входа. level 1-5 -> PNG, level 6 -> анимированный GIF (BytesIO).
    as_bytes=False возвращает PIL.Image (только для уровней 1-5)."""
    level = max(1, min(6, int(level)))
    if level == 6:
        from .banner_anim import make_animated_banner
        return make_animated_banner(user_id, name, tag, preset=preset, salt=salt, fresh=fresh,
                                    item_seed=item_seed, avatar=avatar, frames=frames_n, fps=fps,
                                    show_name=show_name)
    digest = make_digest(user_id, salt, fresh, item_seed)
    P = build_parts(digest, name, tag, level, avatar, show_name=show_name)
    rng, cfg, acc = P["rng"], P["cfg"], P["acc"]
    img = _background(rng, P["bg1"], P["bg2"], acc, cfg)
    if cfg["pattern"]:
        img = Image.alpha_composite(img, _pattern(rng, acc))
    img = Image.alpha_composite(img, _bokeh(rng, acc, rng.randint(*cfg["bokeh"])))
    img = Image.alpha_composite(img, _sparkles(rng, rng.randint(*cfg["dots"]), rng.randint(*cfg["stars"])))
    img = _glass(img, P["box"], RADIUS, acc[0])
    for layer in (P["frame"], P["center"], P["text"]):
        img = Image.alpha_composite(img, layer)
    out = img.convert("RGB")
    if not as_bytes:
        return out
    buf = io.BytesIO()
    out.save(buf, "PNG", optimize=True)
    buf.seek(0)
    buf.name = "banner.png"
    return buf


if __name__ == "__main__":
    for lv in range(1, 6):
        make_banner(1, "Деменция", "dementia", level=lv, as_bytes=False).save(f"banner_lvl{lv}.png")
