"""
banner_anim.py - легендарный (уровень 6): анимированный GIF-баннер, 28 пресетов-сцен.

Схема экономии CPU: карточка, рамка, текст и эмблема рендерятся ОДИН раз (banner.build_parts),
сцена-пресет считается на уменьшенной картинке (800 px) из готовых спрайтов.
Все движения периодические (целое число циклов за GIF), поэтому петля бесшовная.

    from banner_anim import make_animated_banner, PRESET_IDS
    buf = make_animated_banner(user_id, name, tag, preset=None)   # preset=None -> по seed
    # buf.name == "banner.gif"

Новый пресет = функция p_xxx(C) -> render(t), плюс строка в PRESETS. Больше ничего менять не надо.
"""
import io
import math
import random
from types import SimpleNamespace

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from . import banner
from .bn_common import H, W, font, lerp, make_digest

OUT_W = 800
TAU = 2 * math.pi


# =============================================================== спрайты (маски 'L')
def radial(size, power=2.0):
    size = max(2, int(size))
    y, x = np.mgrid[0:size, 0:size].astype(np.float32)
    c = (size - 1) / 2
    d = np.sqrt((x - c) ** 2 + (y - c) ** 2) / c
    return Image.fromarray((np.clip(1 - d, 0, 1) ** power * 255).astype(np.uint8), "L")


def ss_mask(w, h, fn, k=4, blur=0.0):
    im = Image.new("L", (w * k, h * k), 0)
    fn(ImageDraw.Draw(im), k)
    im = im.resize((w, h), Image.LANCZOS)
    return im.filter(ImageFilter.GaussianBlur(blur)) if blur else im


def scaled(m, k):
    k = max(0.0, min(1.0, k))
    return m.point([int(v * k) for v in range(256)])


def star4(size):
    def fn(d, k):
        S = size * k
        c, q = S / 2, S / 2 * .14
        d.polygon([(c, 0), (c + q, c - q), (S, c), (c + q, c + q), (c, S), (c - q, c + q), (0, c), (c - q, c - q)], fill=255)
    return ss_mask(size, size, fn, 4, .5)


def m_petal(r):
    w, h = int(r * 2) + 2, int(r * 1.15) + 2
    return ss_mask(w, h, lambda d, k: d.ellipse([k, k, (w - 1) * k, (h - 1) * k], fill=255), 4, .4)


def m_leaf(r):
    w, h = int(r * 2) + 2, int(r) + 2

    def fn(d, k):
        pts, L = [], (w / 2 - 1) * k
        for i in range(21):
            x = -L + 2 * L * i / 20
            pts.append((w * k / 2 + x, h * k / 2 - (h / 2 - 1) * k * (1 - (x / L) ** 2) ** .9))
        for i in range(20, -1, -1):
            x = -L + 2 * L * i / 20
            pts.append((w * k / 2 + x, h * k / 2 + (h / 2 - 1) * k * (1 - (x / L) ** 2) ** .9))
        d.polygon(pts, fill=255)
        d.line([(k, h * k / 2), ((w - 1) * k, h * k / 2)], fill=120, width=k)
    return ss_mask(w, h, fn, 4, .3)


def m_heart(r):
    s = int(r * 2) + 2

    def fn(d, k):
        pts = []
        for i in range(64):
            t = TAU * i / 64
            x = 16 * math.sin(t) ** 3
            y = 13 * math.cos(t) - 5 * math.cos(2 * t) - 2 * math.cos(3 * t) - math.cos(4 * t)
            pts.append((s * k / 2 + x / 17 * (s / 2 - 1) * k, s * k / 2 - y / 17 * (s / 2 - 1) * k))
        d.polygon(pts, fill=255)
    return ss_mask(s, s, fn, 4, .4)


def m_ring(r, w=2):
    s = int(r * 2) + 4
    return ss_mask(s, s, lambda d, k: d.ellipse([2 * k, 2 * k, (s - 2) * k, (s - 2) * k], outline=255, width=w * k), 4, .5)


def m_rect(w, h):
    return Image.new("L", (int(w), int(h)), 255)


def m_disc(r, p=1.6):
    return radial(int(r * 2), p)


def m_streak(L, w=3):
    """Капля дождя / метеор: вертикальный штрих с хвостом сверху."""
    y = (np.arange(L, dtype=np.float32) / L) ** 1.6
    x = 1 - np.abs(np.linspace(-1, 1, w))
    return Image.fromarray((np.outer(y, x) * 255).astype(np.uint8), "L")


# =============================================================== утилиты сцены
def vgrad(C, stops):
    ys = np.linspace(0, 1, C.OH)
    cols = np.stack([np.interp(ys, [p for p, _ in stops], [c[i] for _, c in stops]) for i in range(3)], 1)
    return np.repeat(cols[:, None, :], C.OW, 1).astype(np.float32)


def toimg(a):
    return Image.fromarray(np.clip(a, 0, 255).astype(np.uint8))


def blob(a, C, cx, cy, rx, ry, col, k):
    yy, xx = np.arange(C.OH)[:, None], np.arange(C.OW)[None, :]
    m = np.exp(-(((xx - cx) / rx) ** 2 + ((yy - cy) / ry) ** 2))[..., None]
    a += m * np.array(col, np.float32) * k


def blend(fr, ov):
    fr.paste(ov.convert("RGB"), (0, 0), ov.getchannel("A"))


def glowdot(fr, col, x, y, r, a=1.0, p=1.6):
    m = scaled(radial(r * 2, p), a)
    fr.paste(col, (int(x - m.width / 2), int(y - m.height / 2)), m)


def scene(base, *layers):
    def render(t):
        fr = base(t) if callable(base) else base.copy()
        for l in layers:
            l(fr, t)
        return fr
    return render


def silhouette_city(img, C, ybase, col, lit=(255, 214, 120)):
    d, rng, x = ImageDraw.Draw(img), C.rng, 0
    while x < C.OW:
        w, h = rng.randint(28, 70), rng.randint(40, 150)
        d.rectangle([x, ybase - h, x + w, C.OH], fill=col)
        for wy in range(int(ybase - h + 8), int(ybase), 12):
            for wx in range(x + 5, x + w - 6, 10):
                if rng.random() < .28:
                    d.rectangle([wx, wy, wx + 3, wy + 5], fill=lit)
        x += w + rng.randint(0, 6)


def ridge(C, base, g, n, amp, k, col, decay=.55, absval=False, step=6):
    """Горизонтально бегущий рельеф (дюны/волны/горы). Спектр g·j, сдвиг на период 1/g за цикл."""
    rng = C.rng
    js = list(range(1, n + 1))
    amps = [amp * decay ** (j - 1) for j in js]
    phs = [rng.random() for _ in js]

    def draw(fr, t):
        pts = []
        for x in range(0, C.OW + step, step):
            v = sum(a * math.sin(TAU * (g * j * x / C.OW - j * k * t + ph)) for j, a, ph in zip(js, amps, phs))
            pts.append((x, base - (abs(v) if absval else v)))
        pts += [(C.OW, C.OH), (0, C.OH)]
        ImageDraw.Draw(fr).polygon(pts, fill=col(t) if callable(col) else col)
    return draw


# =============================================================== слои-эффекты
def L_stars(C, n, zr=(4, 9), gate=None, col=(255, 255, 255), area=1.0):
    rng = C.rng
    ST = [(rng.uniform(0, C.OW), rng.uniform(0, C.OH * area), rng.randint(*zr), rng.random(), rng.choice([1, 2]))
          for _ in range(n)]
    masks = {z: star4(z) for z in range(zr[0], zr[1] + 1)}

    def draw(fr, t):
        g = 1 if gate is None else gate(t)
        if g < .03:
            return
        for x, y, z, ph, k in ST:
            a = max(0.0, math.sin(TAU * (k * t + ph))) ** 2 * g
            if a > .03:
                fr.paste(col, (int(x - z / 2), int(y - z / 2)), scaled(masks[z], a))
    return draw


def L_meteors(C, n, col, ang=(58, 74), length=(90, 170)):
    rng, M = C.rng, []
    for _ in range(n):
        a = math.radians(rng.uniform(*ang))
        L = int(rng.uniform(*length) * C.s * 1.2)
        w, h = int(L * math.cos(a)) + 16, int(L * math.sin(a)) + 16
        m = Image.new("L", (w, h), 0)
        d = ImageDraw.Draw(m)
        for i in range(L):
            al = int(255 * (i / L) ** 1.9)
            d.ellipse([8 + i * math.cos(a) - 1.3, 8 + i * math.sin(a) - 1.3, 8 + i * math.cos(a) + 1.3, 8 + i * math.sin(a) + 1.3], fill=al)
        hx, hy = 8 + L * math.cos(a), 8 + L * math.sin(a)
        d.ellipse([hx - 3, hy - 3, hx + 3, hy + 3], fill=255)
        M.append((m.filter(ImageFilter.GaussianBlur(.8)), a, hx, hy, rng.uniform(0, C.OW * 1.1), rng.random()))

    def draw(fr, t):
        for m, a, hx, hy, x0, ph in M:
            u = ((t + ph) % 1) * 1.7
            if u < 1:
                dist = u * (C.OH + 60) / math.sin(a)
                x, y = x0 + dist * math.cos(a) - C.OW * .2, -20 + dist * math.sin(a)
                fr.paste(col, (int(x - hx), int(y - hy)), m)
    return draw


def fall(C, n, mk, cols, sizes, k=(1,), sway=(0, 0), up=False, rot=0, alpha=(.6, 1.0), span=.6, wind=0.0, life=True):
    """Частицы. life=True: живут один цикл (плавно появляются и гаснут) - медленный дрейф без швов.
    life=False: пролетают экран насквозь (дождь, конфетти), k - целое число проходов за цикл."""
    rng, P = C.rng, []
    for _ in range(n):
        sz = rng.choice(list(sizes))
        P.append(dict(x=rng.uniform(0, C.OW), y0=rng.uniform(0, C.OH), p=rng.random(), k=rng.choice(k), m=mk(sz),
                      a=rng.uniform(*sway), f=rng.choice([1, 2]), ph=rng.random(), c=rng.choice(cols),
                      r=rot * rng.choice([-1, 1]) if rot else 0, ang0=rng.uniform(0, 360), al=rng.uniform(*alpha)))

    def draw(fr, t):
        for q in P:
            u = (q["p"] + q["k"] * t) % 1
            m = q["m"]
            if life:
                dy = span * C.OH * (u - .5) * (-1 if up else 1)
                y = q["y0"] + dy
                x = q["x"] + wind * span * C.OH * (u - .5)
                al = q["al"] * math.sin(math.pi * u) ** .7
            else:
                uu = 1 - u if up else u
                y = -m.height + uu * (C.OH + 2 * m.height)
                x = q["x"] + wind * C.OH * (uu - .5)
                al = q["al"]
            x += q["a"] * math.sin(TAU * (q["f"] * t + q["ph"]))
            if q["r"]:
                m = m.rotate(q["ang0"] + 360 * q["r"] * t, resample=Image.BICUBIC, expand=True)
            if al < .02:
                continue
            if al < 1:
                m = scaled(m, al)
            fr.paste(q["c"], (int(x - m.width / 2), int(y - m.height / 2)), m)
    return draw


def L_bokeh(C, n, cols=None, alpha=(.12, .3), span=.35):
    return fall(C, n, lambda z: m_disc(z, 1.0), cols or C.acc, range(10, 40), up=True, alpha=alpha, span=span)


def L_clouds(C, n, col, alpha=.5, gate=None, ymax=.5, drift=36):
    rng, cl = C.rng, []
    for _ in range(n):
        w = rng.randint(130, 240)
        h = int(w * .38)
        m = Image.new("L", (w * 2, h * 2), 0)
        d = ImageDraw.Draw(m)
        for _ in range(7):
            cx, cy, r = rng.uniform(.3, .7) * w * 2, rng.uniform(.4, .6) * h * 2, rng.uniform(.18, .32) * w
            d.ellipse([cx - r, cy - r * .6, cx + r, cy + r * .6], fill=255)
        cl.append((m.filter(ImageFilter.GaussianBlur(h * .16)), rng.uniform(0, C.OW), rng.uniform(.04, ymax) * C.OH, rng.random()))

    def draw(fr, t):
        g = 1 if gate is None else gate(t)
        if g < .03:
            return
        for m, x, y, ph in cl:
            fr.paste(col, (int(x - m.width / 2 + drift * math.sin(TAU * (t + ph))), int(y)), scaled(m, alpha * g))
    return draw


def plasma(C, pal, fx=(.05, .04, .03, .05), ks=(1, 1, 1, 1), weight=None, res=4):
    """Бесшовная «плазма» по палитре pal (список цветов) - туманность, лава, огонь."""
    RW, RH = C.OW // res, C.OH // res
    yy, xx = np.mgrid[0:RH, 0:RW].astype(np.float32)
    xx, yy = xx * res, yy * res
    stops = np.linspace(0, 1, len(pal))
    lut = np.stack([np.interp(np.linspace(0, 1, 256), stops, [c[i] for c in pal]) for i in range(3)], 1).astype(np.float32)
    ph = [C.rng.uniform(0, TAU) for _ in range(4)]
    cx, cy = C.rng.uniform(.3, .7) * C.OW, C.rng.uniform(.3, .7) * C.OH
    wgt = None if weight is None else np.interp(np.arange(RH) * res / C.OH, [p for p, _ in weight], [v for _, v in weight])[:, None]

    def base(t):
        v = (np.sin(xx * fx[0] + TAU * ks[0] * t + ph[0]) + np.sin(yy * fx[1] - TAU * ks[1] * t + ph[1]) +
             np.sin((xx + yy) * fx[2] + TAU * ks[2] * t + ph[2]) +
             np.sin(np.hypot(xx - cx, yy - cy) * fx[3] - TAU * ks[3] * t + ph[3])) / 4
        v = (v + 1) / 2
        if wgt is not None:
            v = v * wgt
        im = Image.fromarray(lut[(np.clip(v, 0, 1) * 255).astype(np.uint8)].astype(np.uint8))
        return im.resize((C.OW, C.OH), Image.BICUBIC)
    return base


def L_fireflies(C, n, col=(220, 255, 140), box=(0, .2, 1, .95)):
    rng = C.rng
    F = [(rng.uniform(box[0], box[2]) * C.OW, rng.uniform(box[1], box[3]) * C.OH, rng.uniform(20, 60), rng.uniform(10, 30),
          rng.choice([1, 2]), rng.choice([1, 2]), rng.random(), rng.random(), rng.choice([1, 2])) for _ in range(n)]

    def draw(fr, t):
        for x, y, ax, ay, m1, m2, p1, p2, k in F:
            a = .25 + .75 * max(0, math.sin(TAU * (k * t + p1))) ** 2
            glowdot(fr, col, x + ax * math.sin(TAU * (m1 * t + p1)), y + ay * math.sin(TAU * (m2 * t + p2)), 9, a, 1.3)
    return draw


# =============================================================== шестерёнки (из прототипа)
def _gear(r, teeth, col, alpha, blur):
    S = 3
    R = r * S
    size = int(2 * R + 12 * S)
    im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    c, ri, p, pts = size / 2, R * .83, TAU / teeth, []
    for i in range(teeth):
        for da, rr in ((-.30, ri), (-.17, R), (.17, R), (.30, ri)):
            pts.append((c + rr * math.cos(i * p + da * p), c + rr * math.sin(i * p + da * p)))
    light = lerp(col, (255, 255, 255), .5)
    d.polygon(pts, fill=col + (alpha,))
    d.line(pts + [pts[0]], fill=light + (min(255, alpha + 80),), width=3)
    hole = ri * .55
    d.ellipse([c - hole, c - hole, c + hole, c + hole], fill=(0, 0, 0, 0))
    for i in range(teeth):                                       # n отверстий: поворот на 1 зуб = та же картинка
        a = i * TAU / teeth
        px, py, pr = c + hole * .72 * math.cos(a), c + hole * .72 * math.sin(a), max(2, R * .035)
        d.ellipse([px - pr, py - pr, px + pr, py + pr], fill=col + (alpha,))
    d.ellipse([c - hole * .45, c - hole * .45, c + hole * .45, c + hole * .45], outline=col + (alpha,), width=int(R * .06))
    d.ellipse([c - R * .17, c - R * .17, c + R * .17, c + R * .17], fill=light + (alpha,))
    return im.resize((size // S, size // S), Image.LANCZOS).filter(ImageFilter.GaussianBlur(blur))


def _layout_gears(rng, n, box, accs, scale):
    """Шестерёни, зацепленные по-настоящему (фазы зубьев совпадают)."""
    x0, y0, x1, y1 = box
    cw, ch = x1 - x0, y1 - y0
    r1, n1 = rng.uniform(.30, .40) * ch, rng.choice([14, 16, 18, 20])
    g = [dict(c=(x0 + rng.uniform(.05, .5) * cw, y1 - rng.uniform(-.05, .2) * ch), r=r1, n=n1, phi=0.0, dir=1)]
    tries = 0
    while len(g) < n and tries < 300:
        tries += 1
        b = rng.choice(g)
        th = rng.uniform(0, TAU)
        nt = max(8, round(b["n"] * rng.uniform(.4, .8)))
        r = b["r"] * nt / b["n"]
        dist = (b["r"] + r) * .915
        c = (b["c"][0] + dist * math.cos(th), b["c"][1] + dist * math.sin(th))
        if not (x0 - r * .5 < c[0] < x1 + r * .5 and y0 - r * .5 < c[1] < y1 + r * .5):
            continue
        if any(math.dist(c, o["c"]) < (r + o["r"]) * .9 for o in g):
            continue
        p1, p2 = TAU / b["n"], TAU / nt
        u1 = ((th - b["phi"]) % p1) / p1
        u2 = (.5 - u1) % 1
        g.append(dict(c=c, r=r, n=nt, phi=(th + math.pi - u2 * p2) % TAU, dir=-b["dir"]))
    for i, o in enumerate(g):
        o["sprite"] = _gear(o["r"], o["n"], accs[i % len(accs)], 125, 2.0 * scale + .8)
    return g


# =============================================================== ПРЕСЕТЫ (28)
def p_gears(C):
    base = toimg(vgrad(C, [(0, C.bg1), (1, lerp(C.bg2, C.acc[0], .15))]))
    gs = _layout_gears(C.rng, 6, (0, 0, C.OW, C.OH), C.acc, C.s)

    def gears(fr, t):
        for g in gs:
            spr = g["sprite"].rotate(-math.degrees(g["phi"] + g["dir"] * (TAU / g["n"]) * t), resample=Image.BICUBIC)
            fr.paste(spr.convert("RGB"), (int(g["c"][0] - spr.width / 2), int(g["c"][1] - spr.height / 2)), spr.getchannel("A"))
    return scene(base, gears, L_bokeh(C, 8), L_stars(C, 36))


def p_starry(C):
    a = vgrad(C, [(0, (4, 6, 24)), (.6, (14, 16, 50)), (1, (46, 28, 84))])
    for _ in range(3):
        blob(a, C, C.rng.uniform(0, C.OW), C.rng.uniform(0, C.OH * .8), C.rng.uniform(120, 260), C.rng.uniform(60, 140),
             C.rng.choice(C.acc), C.rng.uniform(.25, .5))
    return scene(toimg(a), L_stars(C, 70, (3, 6)), L_stars(C, 18, (8, 12)), L_meteors(C, 8, (255, 255, 255)))


def p_sakura(C):
    rng = C.rng
    img = toimg(vgrad(C, [(0, (36, 18, 66)), (.55, (140, 64, 118)), (1, (250, 168, 170))]))
    d = ImageDraw.Draw(img)
    mx, my, mr = C.OW * .62, C.OH * .3, 50
    glowdot(img, (255, 225, 235), mx, my, mr * 3, .55, 2.2)
    d.ellipse([mx - mr, my - mr, mx + mr, my + mr], fill=(255, 240, 244))
    pinks = [(255, 183, 197), (255, 210, 222), (250, 150, 180), (255, 232, 238)]

    def blossom(x, y):
        r = rng.uniform(5, 8)
        c = rng.choice(pinks)
        for i in range(5):
            a = TAU * i / 5 + rng.uniform(-.2, .2)
            d.ellipse([x + math.cos(a) * r - r * .7, y + math.sin(a) * r - r * .7, x + math.cos(a) * r + r * .7, y + math.sin(a) * r + r * .7], fill=c)
        d.ellipse([x - 2, y - 2, x + 2, y + 2], fill=(255, 220, 120))

    def br(x, y, ang, L, w, dep):
        x2, y2 = x + L * math.cos(ang), y + L * math.sin(ang)
        d.line([(x, y), (x2, y2)], fill=(52, 26, 38), width=max(1, int(w)))
        if dep == 0 or rng.random() < .25:
            blossom(x2, y2)
        if dep:
            for da in (-.5, .45):
                if rng.random() < .92:
                    br(x2, y2, ang + da + rng.uniform(-.25, .25), L * rng.uniform(.62, .78), w * .7, dep - 1)
    br(-10, C.OH * .16, .38, 130, 9, 5)
    br(C.OW + 10, C.OH * .08, math.pi - .5, 90, 6, 4)
    petals = fall(C, 42, m_petal, pinks, range(7, 15), sway=(8, 26), rot=1, span=.8, wind=.55, alpha=(.7, 1))
    return scene(img, L_stars(C, 20, (4, 7), col=(255, 235, 240), area=.5), petals)


def p_daynight(C):
    OW, OH = C.OW, C.OH
    hz = OH * .74
    NT, NB, DT, DB, WARM = (6, 10, 36), (26, 28, 70), (70, 150, 245), (190, 225, 255), (255, 140, 70)

    def dayf(t):
        x = max(0, min(1, .5 + .85 * math.sin(TAU * t)))
        return x * x * (3 - 2 * x)

    def base(t):
        d = dayf(t)
        bot = lerp(lerp(NB, DB, d), WARM, .55 * 4 * d * (1 - d))
        return toimg(vgrad(C, [(0, lerp(NT, DT, d)), (hz / OH, bot), (1, bot)]))

    def orb(fr, t):
        th = TAU * t
        for off, col, gcol, r, ga in ((0, (255, 236, 180), (255, 200, 120), 26, .6), (math.pi, (236, 240, 255), (170, 190, 255), 20, .35)):
            s = math.sin(th + off)
            x, y = OW * .5 - OW * .42 * math.cos(th + off), hz - hz * .78 * s
            if s > -.25:
                glowdot(fr, gcol, x, y, r * 4, ga)
                ImageDraw.Draw(fr).ellipse([x - r, y - r, x + r, y + r], fill=col)

    h1 = ridge(C, hz + 6, 2, 2, 12, 0, lambda t: lerp((14, 24, 34), (70, 140, 80), dayf(t)))
    h2 = ridge(C, hz + 30, 3, 3, 14, 0, lambda t: lerp((10, 18, 28), (46, 110, 64), dayf(t)))

    def hills(fr, t):
        h1(fr, t)
        h2(fr, t)
    clouds = L_clouds(C, 5, (255, 255, 255), .55, gate=lambda t: dayf(t))
    return scene(base, L_stars(C, 60, (3, 7), gate=lambda t: 1 - dayf(t), area=.7), clouds, orb, hills)


def p_aurora(C):
    OW, OH = C.OW, C.OH
    rng = C.rng
    sky = vgrad(C, [(0, (3, 6, 22)), (.7, (8, 18, 44)), (1, (14, 30, 52))])
    RW, RH = OW // 2, OH // 2
    yy, xx = np.mgrid[0:RH, 0:RW].astype(np.float32)
    ribs = [(rng.uniform(.2, .45) * RH, rng.uniform(.08, .18) * RH, rng.choice([1, 2, 3]), rng.choice([1, 2]), rng.random(), rng.random(),
             rng.choice([(40, 255, 150), (60, 200, 255), (170, 90, 255), (255, 80, 200)]), rng.choice([-1, 1])) for _ in range(4)]

    def base(t):
        a = sky.copy()
        lay = np.zeros((RH, RW, 3), np.float32)
        for y0, amp, f, k, p1, p2, col, dr in ribs:
            yc = y0 + amp * np.sin(TAU * (f * xx / RW + dr * k * t + p1))
            w = RH * .1
            inten = np.exp(-((yy - yc) / w) ** 2) * (.55 + .45 * np.sin(TAU * (2 * xx / RW - k * t + p2)))
            inten += .35 * np.exp(-np.clip(yy - yc, 0, None) / (w * 2.2)) * (yy > yc) * (.5 + .5 * np.sin(TAU * (3 * xx / RW + p2)))
            lay += inten[..., None] * np.array(col, np.float32) * .55
        a += np.asarray(Image.fromarray(np.clip(lay, 0, 255).astype(np.uint8)).resize((OW, OH), Image.BICUBIC), np.float32)
        return toimg(a)
    mount = ridge(C, OH * .86, 3, 3, 26, 0, (6, 10, 20), absval=True)
    return scene(base, L_stars(C, 50, (3, 6), area=.6), mount)


def p_synthwave(C):
    OW, OH = C.OW, C.OH
    hy = OH * .56
    img = toimg(vgrad(C, [(0, (14, 4, 40)), (hy / OH * .85, (120, 20, 120)), (hy / OH, (255, 90, 120)), (hy / OH + .001, (18, 4, 40)), (1, (30, 6, 56))]))
    sr = OH * .26
    sun = np.zeros((int(sr * 2), int(sr * 2), 3), np.float32)
    ys = np.linspace(0, 1, sun.shape[0])
    for i, y in enumerate(ys):
        sun[i] = np.array(lerp((255, 235, 90), (255, 40, 150), y), np.float32)
    sm = ss_mask(int(sr * 2), int(sr * 2), lambda d, k: d.ellipse([0, 0, sr * 2 * k - 1, sr * 2 * k - 1], fill=255), 3)
    cut = ImageDraw.Draw(sm)
    for i in range(7):
        y = sr * (1.0 + i * .15)
        cut.rectangle([0, y, sr * 2, y + 1 + i * 1.6], fill=0)
    sx, sy = OW * .5 - sr, hy - sr * 1.55
    glowdot(img, (255, 60, 150), OW * .5, sy + sr, sr * 2.2, .5, 2)
    img.paste(toimg(sun), (int(sx), int(sy)), sm)
    ImageDraw.Draw(img).rectangle([0, hy, OW, OH], fill=(18, 4, 40))

    def grid(fr, t):
        d = ImageDraw.Draw(fr)
        N = 11
        for j in range(N):
            z = (j + t) / N
            y = hy + (OH - hy) * z ** 2.2
            d.line([(0, y), (OW, y)], fill=lerp((60, 10, 90), (255, 60, 200), z), width=1 + (z > .5))
        for i in range(-14, 15):
            d.line([(OW / 2 + i * 6, hy), (OW / 2 + i * 120, OH)], fill=(200, 40, 190), width=1)
    return scene(img, L_stars(C, 40, (3, 6), area=.45, col=(255, 230, 250)), grid)


def p_matrix(C):
    rng = C.rng
    img = toimg(vgrad(C, [(0, (0, 8, 4)), (1, (0, 22, 10))]))
    f = font("r", 13)
    cw = 14
    cols = [(i * cw + 4, rng.random(), rng.choice([1, 1, 2]), rng.randint(7, 15)) for i in range(C.OW // cw + 1)]
    chars = "0123456789ABCDEF<>+=*#"
    G = {}

    def draw(fr, t):
        d = ImageDraw.Draw(fr)
        for x, p, k, L in cols:
            head = ((p + k * t) % 1) * (C.OH + L * cw) / cw
            for i in range(L):
                row = int(head) - i
                if 0 <= row * cw < C.OH:
                    g = G.setdefault((x, row), rng.choice(chars))
                    a = (1 - i / L) ** 1.5
                    col = (235, 255, 235) if i == 0 else (int(20 * a), int(255 * a), int(70 * a))
                    d.text((x, row * cw), g, font=f, fill=col)
    return scene(img, draw)


def p_ocean(C):
    OW, OH = C.OW, C.OH
    hz = OH * .52
    img = toimg(vgrad(C, [(0, (30, 20, 80)), (hz / OH * .8, (240, 110, 90)), (hz / OH, (255, 190, 110)), (hz / OH + .001, (16, 40, 80)), (1, (6, 18, 44))]))
    sx, sr = OW * .5, 44
    glowdot(img, (255, 190, 110), sx, hz - 6, sr * 4, .7)
    ImageDraw.Draw(img).ellipse([sx - sr, hz - sr * .9, sx + sr, hz + sr * .9], fill=(255, 238, 190))
    ImageDraw.Draw(img).rectangle([0, hz, OW, OH], fill=(16, 40, 80))

    def glints(fr, t):
        d = ImageDraw.Draw(fr)
        for i in range(16):
            y = hz + 6 + i * (OH - hz) / 17
            w = 8 + i * 5
            a = max(0, math.sin(TAU * (t + i * .13 + (i % 3) * .3))) ** 2
            d.line([(sx - w, y), (sx + w, y)], fill=lerp((16, 40, 80), (255, 200, 130), a * (1 - i / 20)), width=2)
    layers = [ridge(C, hz + 18 + i * 32, 3 + i, 2, 6 + i * 2, (1 if i % 2 else -1),
                    lerp((14, 50, 90), (4, 14, 36), i / 3), decay=.5) for i in range(4)]

    def waves(fr, t):
        glints(fr, t)
        for l in layers:
            l(fr, t)
    return scene(img, L_stars(C, 24, (3, 5), area=.3), waves)


def p_deepsea(C):
    OW, OH = C.OW, C.OH
    rng = C.rng
    img = toimg(vgrad(C, [(0, (0, 40, 80)), (.6, (0, 16, 46)), (1, (0, 4, 20))]))
    rays = []
    for _ in range(6):
        x = rng.uniform(.05, 1) * OW
        w = rng.uniform(30, 80)
        m = Image.new("L", (OW, OH), 0)
        ImageDraw.Draw(m).polygon([(x - 6, 0), (x + 6, 0), (x + w * 1.5, OH), (x - w, OH)], fill=255)
        rays.append((m.filter(ImageFilter.GaussianBlur(10)), rng.random()))

    def ray(fr, t):
        for m, ph in rays:
            fr.paste((120, 200, 255), (0, 0), scaled(m, .16 + .12 * math.sin(TAU * (t + ph))))
    jel = [(rng.uniform(.1, .9) * OW, rng.uniform(.25, .7) * OH, rng.uniform(26, 44), rng.random(), rng.choice(C.acc)) for _ in range(3)]

    def jelly(fr, t):
        for x, y0, w, ph, col in jel:
            p = math.sin(TAU * (t + ph))
            cy, ww, hh = y0 - 16 * p, w * (1 + .1 * p), w * (.75 - .1 * p)
            m = ss_mask(int(ww * 2), int(hh * 2), lambda d, k: d.pieslice([0, 0, ww * 2 * k - 1, hh * 4 * k - 1], 180, 360, fill=255), 3, 1.5)
            fr.paste(lerp(col, (255, 255, 255), .35), (int(x - ww), int(cy - hh)), scaled(m, .55))
            tm = Image.new("L", (int(ww * 2) + 20, int(hh * 3.4)), 0)
            td = ImageDraw.Draw(tm)
            for i in range(5):
                pts = [(10 + ww * 2 * (i + .5) / 5 + 5 * math.sin(TAU * (t + j / 7 + i * .2)), j * hh * .45) for j in range(7)]
                td.line(pts, fill=255, width=2)
            fr.paste(col, (int(x - ww - 10), int(cy - 2)), scaled(tm, .45))
    bubbles = fall(C, 26, lambda z: m_ring(z, 1), [(180, 230, 255)], range(3, 8), sway=(4, 10), up=True, span=.7, alpha=(.5, .9))
    return scene(img, ray, jelly, bubbles, L_bokeh(C, 8, [(80, 200, 255)], (.05, .12)))


def p_snowfall(C):
    OW, OH = C.OW, C.OH
    img = toimg(vgrad(C, [(0, (10, 18, 46)), (.7, (40, 62, 104)), (1, (96, 120, 160))]))
    d = ImageDraw.Draw(img)
    for i in range(14):
        x, h = C.rng.uniform(-10, OW), C.rng.uniform(70, 150)
        for j in range(4):
            w = h * (.22 + j * .08)
            y = OH * .88 - h + j * h * .22
            d.polygon([(x, y - h * .3), (x - w, y + h * .24), (x + w, y + h * .24)], fill=(10, 24, 36) if i % 2 else (16, 34, 50))
    ImageDraw.Draw(img).polygon([(0, OH), (0, OH * .9)] + [(x, OH * .9 - 8 * math.sin(x / 70)) for x in range(0, OW + 20, 20)] + [(OW, OH)], fill=(220, 232, 248))
    snow = lambda n, sz, sp, al: fall(C, n, lambda z: m_disc(z, .8), [(255, 255, 255)], sz, sway=(6, 20), span=sp, alpha=al, wind=.35)
    return scene(img, L_stars(C, 30, (3, 5), area=.5), snow(40, range(2, 4), 1.0, (.5, .9)), snow(24, range(4, 7), 1.2, (.6, 1)),
                 snow(8, range(8, 12), 1.5, (.3, .55)))


def p_rain_city(C):
    OW, OH = C.OW, C.OH
    img = toimg(vgrad(C, [(0, (6, 8, 26)), (.7, (22, 14, 54)), (1, (50, 20, 74))]))
    silhouette_city(img, C, OH * .8, (8, 8, 20), lit=lerp(C.acc[0], (255, 255, 255), .5))
    neon = fall(C, 10, lambda z: m_disc(z, .7), C.acc, range(24, 60), span=.06, alpha=(.12, .26))
    drops = fall(C, 70, lambda z: m_streak(z, 3), [(170, 200, 255)], range(18, 34), k=(1, 2), life=False, alpha=(.35, .8), wind=.25)
    return scene(img, neon, L_bokeh(C, 8), drops)


def p_storm(C):
    OW, OH = C.OW, C.OH
    rng = C.rng
    img = toimg(vgrad(C, [(0, (12, 16, 28)), (1, (40, 50, 70))]))
    bolts = []
    for t0 in (.12, .58):
        x, pts = rng.uniform(.2, .8) * OW, []
        y = 0
        while y < OH * .8:
            pts.append((x, y))
            x += rng.uniform(-26, 26)
            y += rng.uniform(22, 44)
        bolts.append((t0, pts))

    def lightning(fr, t):
        for t0, pts in bolts:
            u = (t - t0) % 1
            if u < .12:
                k = 1 - u / .12
                fr.paste((200, 210, 255), (0, 0), Image.new("L", fr.size, int(90 * k)))
                ov = Image.new("L", fr.size, 0)
                ImageDraw.Draw(ov).line(pts, fill=255, width=4)
                fr.paste((230, 235, 255), (0, 0), scaled(ov.filter(ImageFilter.GaussianBlur(5)), k * 1.2))
                ov2 = Image.new("L", fr.size, 0)
                ImageDraw.Draw(ov2).line(pts, fill=255, width=2)
                fr.paste((255, 255, 255), (0, 0), scaled(ov2, k))
    drops = fall(C, 90, lambda z: m_streak(z, 3), [(160, 190, 240)], range(20, 38), k=(1, 2), life=False, alpha=(.4, .8), wind=.45)
    return scene(img, L_clouds(C, 7, (8, 10, 18), .85, ymax=.35, drift=30), drops, lightning)


def p_fire(C):
    base = plasma(C, [(8, 0, 0), (90, 6, 0), (200, 40, 0), (255, 140, 10), (255, 230, 120)], fx=(.035, .06, .03, .04),
                  ks=(1, 2, 1, 1), weight=[(0, .15), (.55, .45), (1, 1.0)])
    emb = fall(C, 44, lambda z: m_disc(z, 1.2), [(255, 200, 80), (255, 140, 40), (255, 240, 160)], range(3, 8), sway=(6, 18), up=True, span=.8, alpha=(.6, 1))
    return scene(base, emb)


def p_lava(C):
    base = plasma(C, [(10, 0, 0), (60, 4, 0), (160, 20, 0), (255, 90, 0), (255, 200, 40), (60, 4, 0)], fx=(.02, .025, .018, .03), ks=(1, 1, 2, 1))
    return scene(base, fall(C, 26, lambda z: m_disc(z, 1.3), [(255, 190, 70), (255, 120, 30)], range(3, 7), up=True, span=.5, alpha=(.5, 1)))


def p_nebula(C):
    base = plasma(C, [(4, 2, 20), (40, 10, 90), (140, 30, 150), (230, 80, 170), (80, 160, 255), (20, 10, 60)], fx=(.018, .022, .015, .025), ks=(1, 1, 1, 2))
    return scene(base, L_stars(C, 70, (3, 7)), L_stars(C, 14, (9, 13)))


def p_galaxy(C):
    rng = C.rng
    img = toimg(vgrad(C, [(0, (2, 2, 12)), (1, (6, 4, 22))]))
    cx, cy, R = C.OW * .5, C.OH * .5, C.OW * .42
    glowdot(img, (255, 210, 150), cx, cy, R * .5, .75, 2)
    arms = 3
    pts = []
    for _ in range(170):
        r = rng.random() ** .7
        pts.append((r, r * 7 + rng.gauss(0, .16), rng.choice([1, 1, 2])))
    cols = (lerp((255, 240, 200), C.acc[0], .0), C.acc[0], lerp(C.acc[1 % len(C.acc)], (255, 255, 255), .3))

    def spiral(fr, t):
        d = ImageDraw.Draw(fr)
        for arm in range(arms):
            for r, a, sz in pts:
                th = a + TAU * arm / arms + TAU * t / arms
                x, y = cx + R * r * math.cos(th), cy + R * r * .5 * math.sin(th)
                c = lerp((255, 235, 190), cols[int(r * 2.99)], min(1, r * 1.4))
                d.ellipse([x - sz * .8, y - sz * .8, x + sz * .8, y + sz * .8], fill=c)
    return scene(img, L_stars(C, 60, (3, 6)), spiral)


def p_warp(C):
    rng = C.rng
    img = toimg(vgrad(C, [(0, (2, 4, 16)), (1, (4, 4, 20))]))
    cx, cy = C.OW * .5, C.OH * .5
    glowdot(img, C.acc[0], cx, cy, 120, .35, 2)
    S = [(rng.uniform(0, TAU), rng.random(), rng.choice([1, 2]), rng.choice(C.acc)) for _ in range(120)]
    Rm = math.hypot(C.OW, C.OH) * .6

    def draw(fr, t):
        d = ImageDraw.Draw(fr)
        for a, p, k, col in S:
            u = (p + k * t) % 1
            r1, r0 = Rm * u ** 2.2, Rm * max(0, u - .07) ** 2.2
            b = min(1, u * 2.2)
            c = lerp((0, 0, 0), lerp((255, 255, 255), col, .35), b)
            d.line([(cx + r0 * math.cos(a), cy + r0 * math.sin(a)), (cx + r1 * math.cos(a), cy + r1 * math.sin(a))], fill=c, width=1 + int(u * 2.5))
    return scene(img, draw)


def p_fireflies(C):
    rng = C.rng
    OW, OH = C.OW, C.OH
    img = toimg(vgrad(C, [(0, (4, 10, 24)), (.7, (10, 28, 36)), (1, (6, 18, 22))]))
    glowdot(img, (210, 230, 255), OW * .2, OH * .2, 120, .3, 2)
    d = ImageDraw.Draw(img)
    d.ellipse([OW * .2 - 28, OH * .2 - 28, OW * .2 + 28, OH * .2 + 28], fill=(236, 242, 255))
    for i in range(9):
        x, h = (i + rng.uniform(-.2, .2)) * OW / 8, rng.uniform(.4, .75) * OH
        d.rectangle([x - 6, OH - h, x + 6, OH], fill=(4, 10, 12))
        for _ in range(6):
            cx, cy, r = x + rng.uniform(-50, 50), OH - h + rng.uniform(-20, 70), rng.uniform(30, 55)
            d.ellipse([cx - r, cy - r * .8, cx + r, cy + r * .8], fill=(5, 16, 16))
    d.rectangle([0, OH * .93, OW, OH], fill=(3, 8, 8))
    return scene(img, L_fireflies(C, 30), L_stars(C, 20, (3, 5), area=.4))


def p_autumn(C):
    OW, OH = C.OW, C.OH
    rng = C.rng
    img = toimg(vgrad(C, [(0, (60, 24, 20)), (.55, (200, 100, 40)), (1, (70, 34, 24))]))
    glowdot(img, (255, 200, 110), OW * .75, OH * .1, 190, .6, 1.6)
    rays = []
    for _ in range(5):
        x = rng.uniform(.4, 1.1) * OW
        m = Image.new("L", (OW, OH), 0)
        ImageDraw.Draw(m).polygon([(OW * .75, 0), (OW * .75 + 20, 0), (x + 90, OH), (x - 70, OH)], fill=255)
        rays.append((m.filter(ImageFilter.GaussianBlur(14)), rng.random()))

    def ray(fr, t):
        for m, ph in rays:
            fr.paste((255, 220, 150), (0, 0), scaled(m, .12 + .08 * math.sin(TAU * (t + ph))))
    leaves = fall(C, 34, m_leaf, [(220, 80, 30), (240, 160, 40), (170, 50, 30), (250, 200, 70), (130, 70, 30)], range(9, 18),
                  sway=(10, 30), rot=1, span=.85, wind=.5, alpha=(.75, 1))
    return scene(img, ray, ridge(C, OH * .93, 2, 2, 8, 0, (40, 18, 14)), leaves)


def p_fireworks(C):
    OW, OH = C.OW, C.OH
    rng = C.rng
    img = toimg(vgrad(C, [(0, (4, 4, 20)), (1, (30, 16, 50))]))
    silhouette_city(img, C, OH * .92, (6, 6, 14))
    B = []
    for i in range(5):
        cols = [rng.choice(C.acc), lerp(rng.choice(C.acc), (255, 255, 255), .5)]
        B.append((i / 5 + rng.uniform(-.04, .04), rng.uniform(.15, .85) * OW, rng.uniform(.12, .5) * OH, rng.uniform(70, 130), cols,
                  [rng.uniform(.6, 1) for _ in range(34)]))

    def draw(fr, t):
        d = ImageDraw.Draw(fr)
        for t0, cx, cy, R, cols, vs in B:
            u = (t - t0) % 1
            if u < .55:
                fade = (1 - u / .55) ** 1.3
                for i, v in enumerate(vs):
                    a = TAU * i / len(vs)
                    for tr in range(3):
                        uu = max(0, u - tr * .018) / .55
                        r = R * v * (1 - (1 - uu) ** 2)
                        x, y = cx + r * math.cos(a), cy + r * math.sin(a) + 40 * uu ** 2
                        c = lerp((0, 0, 0), cols[i % 2], fade * (1 - tr * .3))
                        d.ellipse([x - 2 + tr * .6, y - 2 + tr * .6, x + 2 - tr * .6, y + 2 - tr * .6], fill=c)
                glowdot(fr, cols[0], cx, cy, R * 1.2, .22 * fade, 2)
    return scene(img, L_stars(C, 30, (3, 5), area=.5), draw)


def p_cyber(C):
    OW, OH = C.OW, C.OH
    rng = C.rng
    img = toimg(vgrad(C, [(0, (2, 12, 22)), (1, (4, 24, 36))]))
    d = ImageDraw.Draw(img)
    paths = []
    for _ in range(16):
        x, y, pts, dx, dy = rng.randrange(0, OW, 20), rng.randrange(0, OH, 20), [], rng.choice([-1, 1]), 0
        pts.append((x, y))
        for _ in range(rng.randint(5, 10)):
            L = rng.randrange(40, 160, 20)
            if dx:
                x += dx * L
                dx, dy = 0, rng.choice([-1, 1])
            else:
                y += dy * L
                dx, dy = rng.choice([-1, 1]), 0
            pts.append((x, y))
        d.line(pts, fill=(14, 70, 88), width=2)
        for p in pts[::2]:
            d.ellipse([p[0] - 4, p[1] - 4, p[0] + 4, p[1] + 4], outline=(24, 120, 140))
        cum = [0]
        for a, b in zip(pts, pts[1:]):
            cum.append(cum[-1] + math.dist(a, b))
        paths.append((pts, cum, rng.random(), rng.choice(C.acc)))

    def at(pts, cum, s):
        s = max(0, min(cum[-1] - 1e-6, s))
        for i in range(len(cum) - 1):
            if cum[i] <= s < cum[i + 1]:
                u = (s - cum[i]) / (cum[i + 1] - cum[i])
                return pts[i][0] + (pts[i + 1][0] - pts[i][0]) * u, pts[i][1] + (pts[i + 1][1] - pts[i][1]) * u
        return pts[-1]

    def pulses(fr, t):
        for pts, cum, p, col in paths:
            for tr in range(7):
                x, y = at(pts, cum, ((p + t) % 1) * cum[-1] - tr * 9)
                glowdot(fr, lerp(col, (255, 255, 255), .5) if tr == 0 else col, x, y, 9 - tr, 1 - tr * .13, 1.2)
    return scene(img, pulses, L_bokeh(C, 6))


def p_radar(C):
    OW, OH = C.OW, C.OH
    rng = C.rng
    img = toimg(vgrad(C, [(0, (0, 14, 8)), (1, (0, 26, 14))]))
    cx, cy, R = C.ex, C.ey, OH * .75
    d = ImageDraw.Draw(img)
    for i in range(1, 6):
        d.ellipse([cx - R * i / 5, cy - R * i / 5, cx + R * i / 5, cy + R * i / 5], outline=(10, 80, 40))
    for i in range(12):
        a = TAU * i / 12
        d.line([(cx, cy), (cx + R * math.cos(a), cy + R * math.sin(a))], fill=(8, 60, 30))
    for x in range(0, OW, 40):
        d.line([(x, 0), (x, OH)], fill=(4, 36, 20))
    blips = [(rng.uniform(0, TAU), rng.uniform(.2, .95) * R) for _ in range(7)]

    def draw(fr, t):
        ang = TAU * t
        m = Image.new("L", fr.size, 0)
        md = ImageDraw.Draw(m)
        for i in range(24, -1, -1):
            a0 = math.degrees(ang) - i * 2.5
            md.pieslice([cx - R, cy - R, cx + R, cy + R], a0 - 2.6, a0 + .1, fill=int(210 * (1 - i / 25) ** 1.7))
        fr.paste((40, 255, 120), (0, 0), m)
        md2 = ImageDraw.Draw(fr)
        md2.line([(cx, cy), (cx + R * math.cos(ang), cy + R * math.sin(ang))], fill=(160, 255, 190), width=2)
        for a, r in blips:
            k = ((ang - a) % TAU) / TAU
            glowdot(fr, (120, 255, 160), cx + r * math.cos(a), cy + r * math.sin(a), 9, (1 - k) ** 2 * .95)
    return scene(img, draw)


def p_equalizer(C):
    OW, OH = C.OW, C.OH
    rng = C.rng
    img = toimg(vgrad(C, [(0, (16, 4, 40)), (1, (46, 10, 70))]))
    nb = 44
    bars = [(rng.choice([1, 2, 3]), rng.random(), rng.uniform(.35, 1)) for _ in range(nb)]
    bw = OW / nb

    def draw(fr, t):
        d = ImageDraw.Draw(fr)
        for i, (k, ph, a) in enumerate(bars):
            h = OH * .62 * a * (.2 + .8 * (.5 + .5 * math.sin(TAU * (k * t + ph + i * .05))))
            x = i * bw + 2
            col = lerp(C.acc[0], C.acc[1 % len(C.acc)], i / nb)
            d.rectangle([x, OH - h, x + bw - 4, OH], fill=lerp((0, 0, 0), col, .75))
            d.rectangle([x, OH - h - 4, x + bw - 4, OH - h], fill=lerp(col, (255, 255, 255), .6))
            d.rectangle([x, OH, x + bw - 4, OH], fill=col)
    return scene(img, L_bokeh(C, 10), draw, L_stars(C, 24, (3, 6), area=.5))


def p_dna(C):
    OW, OH = C.OW, C.OH
    img = toimg(vgrad(C, [(0, (4, 12, 30)), (1, (4, 30, 44))]))
    cy, A, lam = OH * .5, OH * .26, OW / 4
    ca, cb = C.acc[0], C.acc[1 % len(C.acc)]

    def draw(fr, t):
        items = []
        for x in range(-10, OW + 10, 10):
            ph = TAU * (x / lam - t)
            for strand, col in ((0, ca), (math.pi, cb)):
                s, z = math.sin(ph + strand), math.cos(ph + strand)
                items.append((z, x, cy + A * s, col))
            if int(x / 10) % 2 == 0:
                items.append((-2, x, cy + A * math.sin(ph), (x, cy + A * math.sin(ph + math.pi))))
        d = ImageDraw.Draw(fr)
        for z, x, y, col in sorted(items, key=lambda i: i[0]):
            if z == -2:
                d.line([(x, y), (col[0], col[1])], fill=(70, 110, 140), width=2)
            else:
                r = 4 + 2.5 * z
                glowdot(fr, col, x, y, r * 2.4, .55 + .35 * z, 1.1)
                d.ellipse([x - r, y - r, x + r, y + r], fill=lerp(col, (255, 255, 255), .25 + .25 * z))
    return scene(img, L_bokeh(C, 10, alpha=(.08, .18)), draw)


def p_mandala(C):
    OW, OH = C.OW, C.OH
    img = toimg(vgrad(C, [(0, lerp(C.bg1, (0, 0, 0), .3)), (1, lerp(C.bg2, (0, 0, 0), .2))]))
    cx, cy, R = C.ex, C.ey, OH * .62
    rings = [(n, R * (.2 + .13 * i), 1 if i % 2 else -1, C.acc[i % len(C.acc)]) for i, n in enumerate([3, 4, 5, 6, 8, 3, 5, 12])]

    def draw(fr, t):
        ov = Image.new("L", fr.size, 0)
        parts = []
        for n, r, dr, col in rings:
            a0 = dr * TAU * t / n
            parts.append(([(cx + r * math.cos(a0 + TAU * i / n), cy + r * math.sin(a0 + TAU * i / n)) for i in range(n)], col))
        lay = Image.new("RGB", fr.size, (0, 0, 0))
        d = ImageDraw.Draw(lay)
        for pts, col in parts:
            d.line(pts + [pts[0]], fill=col, width=2)
        for n, r, dr, col in rings[::2]:
            d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=lerp((0, 0, 0), col, .4), width=1)
        gl = lay.filter(ImageFilter.GaussianBlur(4))
        arr = np.asarray(fr, np.float32) + np.asarray(gl, np.float32) * 1.4 + np.asarray(lay, np.float32) * .9
        fr.paste(toimg(arr))
    return scene(img, L_bokeh(C, 8), draw, L_stars(C, 30, (3, 6)))


def p_dunes(C):
    OW, OH = C.OW, C.OH
    img = toimg(vgrad(C, [(0, (60, 70, 140)), (.45, (250, 170, 110)), (1, (255, 214, 150))]))
    glowdot(img, (255, 230, 170), OW * .3, OH * .42, 150, .7)
    ImageDraw.Draw(img).ellipse([OW * .3 - 38, OH * .42 - 38, OW * .3 + 38, OH * .42 + 38], fill=(255, 244, 210))
    dunes = [ridge(C, OH * (.58 + i * .12), 3, 2, 16 - i * 2, 1 if i % 2 else -1, lerp((226, 150, 90), (120, 64, 44), i / 3), decay=.4) for i in range(4)]
    sand = fall(C, 40, lambda z: m_disc(z, 1.0), [(255, 226, 170)], range(2, 4), span=.5, wind=2.4, alpha=(.3, .7), sway=(0, 6))

    def all_(fr, t):
        for d in dunes:
            d(fr, t)
    return scene(img, L_clouds(C, 3, (255, 210, 170), .25, ymax=.3), all_, sand)


def p_confetti(C):
    OW, OH = C.OW, C.OH
    img = toimg(vgrad(C, [(0, lerp(C.bg1, (30, 10, 60), .5)), (1, lerp(C.bg2, (50, 12, 80), .4))]))
    cols = C.acc + [(255, 220, 80), (255, 255, 255), (90, 220, 255)]
    beams = [(OW * (.2 + .3 * i), rng_ph) for i, rng_ph in enumerate([0, .33, .66])]

    def beam(fr, t):
        for x, ph in beams:
            a = math.radians(-15 * (1 if x < OW / 2 else -1) * 0 + 22 * math.sin(TAU * (t + ph)))
            m = Image.new("L", fr.size, 0)
            ImageDraw.Draw(m).polygon([(x - 6, OH), (x + 6, OH), (x + 6 + 300 * math.sin(a + .15), 0), (x - 6 + 300 * math.sin(a - .15), 0)], fill=255)
            fr.paste((255, 255, 255), (0, 0), scaled(m.filter(ImageFilter.GaussianBlur(6)), .13))
    conf = fall(C, 64, lambda z: m_rect(z, z * .55), cols, range(6, 12), k=(1, 2), sway=(10, 26), rot=2, life=False, alpha=(.85, 1))
    return scene(img, beam, L_bokeh(C, 8), conf)


def p_hearts(C):
    OW, OH = C.OW, C.OH
    img = toimg(vgrad(C, [(0, (60, 12, 70)), (.6, (170, 40, 110)), (1, (255, 120, 150))]))
    pinks = [(255, 110, 150), (255, 170, 190), (255, 70, 120), (255, 210, 225), (230, 90, 200)]
    return scene(img, L_bokeh(C, 10, [(255, 190, 220)], (.1, .22)),
                 fall(C, 30, m_heart, pinks, range(8, 24), sway=(8, 22), up=True, rot=0, span=.85, alpha=(.6, 1)),
                 L_stars(C, 24, (4, 8), col=(255, 235, 245)))


# id, название, оттенок интерфейса (hue) под сцену, построитель
PRESETS = [
    ("gears", "Шестерёнки", None, p_gears), ("starry", "Звёздное небо", .65, p_starry),
    ("sakura", "Цветение сакуры", .92, p_sakura), ("daynight", "День и ночь", .6, p_daynight),
    ("aurora", "Северное сияние", .42, p_aurora), ("synthwave", "Синтвейв", .86, p_synthwave),
    ("matrix", "Цифровой дождь", .36, p_matrix), ("ocean", "Закат над океаном", .03, p_ocean),
    ("deepsea", "Глубоководье", .55, p_deepsea), ("snowfall", "Снегопад", .58, p_snowfall),
    ("rain_city", "Неоновый дождь", .78, p_rain_city), ("storm", "Гроза", .62, p_storm),
    ("fire", "Огонь", .05, p_fire), ("lava", "Лава", .02, p_lava),
    ("nebula", "Туманность", .78, p_nebula), ("galaxy", "Галактика", .68, p_galaxy),
    ("warp", "Гипердрайв", .58, p_warp), ("fireflies", "Светлячки", .22, p_fireflies),
    ("autumn", "Осенние листья", .07, p_autumn), ("fireworks", "Салют", .95, p_fireworks),
    ("cyber", "Киберсхемы", .5, p_cyber), ("radar", "Радар", .36, p_radar),
    ("equalizer", "Эквалайзер", .83, p_equalizer), ("dna", "Спираль ДНК", .47, p_dna),
    ("mandala", "Мандала", None, p_mandala), ("dunes", "Барханы", .08, p_dunes),
    ("confetti", "Праздник", None, p_confetti), ("hearts", "Сердечки", .94, p_hearts),
]
PRESET_BY_ID = {p[0]: p for p in PRESETS}
PRESET_IDS = [p[0] for p in PRESETS]
PRESET_NAMES = {p[0]: p[1] for p in PRESETS}


def pick_preset(digest):
    return PRESET_IDS[int(digest[8:16], 16) % len(PRESET_IDS)]


# =============================================================== главная функция
def scene_for(user_id, preset, salt="yubari", item_seed=None):
    """Только фоновая сцена пресета: render(t) -> PIL RGB 800x420. Для превью и тестов петли."""
    digest = make_digest(user_id, salt, False, item_seed)
    _, _, hue, builder = PRESET_BY_ID[preset]
    P = banner.build_parts(digest, "x", None, 6, None, hue=hue)
    s = OUT_W / W
    C = SimpleNamespace(OW=OUT_W, OH=round(H * s), s=s, rng=random.Random(int(digest, 16) ^ 0xA11CE), acc=P["acc"],
                        bg1=P["bg1"], bg2=P["bg2"], ex=P["geom"][0] * s, ey=P["geom"][1] * s, er=P["geom"][2] * s)
    return builder(C)


def make_animated_banner(user_id, name, tag=None, preset=None, salt="yubari", fresh=False, item_seed=None,
                         avatar=None, frames=30, fps=20, return_info=False):
    digest = make_digest(user_id, salt, fresh, item_seed)
    pid = preset if preset in PRESET_BY_ID else pick_preset(digest)
    _, pname, hue, builder = PRESET_BY_ID[pid]
    P = banner.build_parts(digest, name, tag, 6, avatar, hue=hue)
    acc = P["acc"]
    A = lambda i: acc[i % len(acc)]

    s = OUT_W / W
    OW, OH = OUT_W, round(H * s)
    sz = lambda im: im.resize((OW, OH), Image.LANCZOS)
    ex, ey, er = [v * s for v in P["geom"]]

    # статика: карточка + рамка + текст (+ медальон с аватаркой) - один раз
    fg = banner.card_flat(P["box"], P["radius"], A(0))
    for layer in (P["frame"], P["text"]) + ((P["crown"],) if P["rotating"] else ()):
        fg = Image.alpha_composite(fg, layer)
    fg = sz(fg)
    fg_rgb, fg_a = fg.convert("RGB"), fg.getchannel("A")

    C = SimpleNamespace(OW=OW, OH=OH, s=s, rng=random.Random(int(digest, 16) ^ 0xA11CE), acc=acc,
                        bg1=P["bg1"], bg2=P["bg2"], ex=ex, ey=ey, er=er)
    render = builder(C)

    # живая рамка: бегущие огни по контуру
    fm = np.asarray(sz(P["fmask"]), np.float32) / 255
    ys, xs = np.where(fm > .02)
    y0, y1, x0, x1 = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
    Mc = fm[y0:y1, x0:x1, None]
    yy, xx = np.mgrid[y0:y1, x0:x1].astype(np.float32)
    U = (np.arctan2(yy - OH / 2, xx - OW / 2) / TAU) % 1
    h1c, h2c = np.array(lerp(A(0), (255, 255, 255), .55), np.float32), np.array(lerp(A(1), (255, 255, 255), .55), np.float32)
    base_c = np.array(A(0), np.float32)

    # центр: эмблема вращается / медальон статичен
    ebox = int(er * 1.55)
    emb = sz(P["center"]).crop((int(ex - ebox), int(ey - ebox), int(ex + ebox), int(ey + ebox)))   # эмблема или медальон
    crown = sz(P["crown"]).crop((int(ex - ebox), int(ey - ebox), int(ex + ebox), int(ey + ebox))) if P["rotating"] else None
    ring = Image.new("L", (int(er * 2.9),) * 2, 0)
    rd = ImageDraw.Draw(ring)
    cc, rr = ring.width / 2, er * 1.12
    for i in range(48):
        a0 = i * TAU / 48
        rd.arc([cc - rr, cc - rr, cc + rr, cc + rr], math.degrees(a0), math.degrees(a0 + .09), fill=210, width=2)
    eglow = radial(int(er * 3.2), 1.6)
    dots = [radial(z, 1.4) for z in (15, 12, 9, 7, 5)]
    gold = (255, 226, 140)
    n1 = 12
    sp = [(C.rng.uniform(0, TAU), C.rng.uniform(.55, 1.35) * er, C.rng.random(), C.rng.choice([1, 2])) for _ in range(22)]
    star10 = star4(10)
    sweep = (np.mgrid[0:OH, 0:OW][1] + np.mgrid[0:OH, 0:OW][0] * .6).astype(np.float32)
    span = OW + OH * .6

    seq = []
    for i in range(frames):
        t = i / frames
        fr = render(t)
        fr.paste(fg_rgb, (0, 0), fg_a)

        k = .30 + .12 * math.sin(TAU * t)                              # пульс свечения
        gm = scaled(eglow, k)
        fr.paste(A(0), (int(ex - gm.width / 2), int(ey - gm.height / 2)), gm)
        rot = emb.rotate(-360 * t, resample=Image.BICUBIC) if P["rotating"] else emb     # эмблема крутится, медальон стоит
        fr.paste(rot.convert("RGB"), (int(ex - rot.width / 2), int(ey - rot.height / 2)), rot.getchannel("A"))
        if crown is not None:
            fr.paste(crown.convert("RGB"), (int(ex - crown.width / 2), int(ey - crown.height / 2)), crown.getchannel("A"))
        rg = ring.rotate(360 * t, resample=Image.BICUBIC)
        fr.paste(A(1), (int(ex - rg.width / 2), int(ey - rg.height / 2)), rg)
        for rad, cnt, spd, col in ((er * 1.20, n1, 1, A(0)), (er * 1.38, 8, -1, A(2))):
            for j in range(cnt):
                for tr, dm in enumerate(dots):
                    a = TAU * (j / cnt + spd * t) - spd * tr * .075
                    fr.paste(col if tr else (255, 255, 255), (int(ex + rad * math.cos(a) - dm.width / 2), int(ey + rad * math.sin(a) - dm.height / 2)),
                             scaled(dm, 1 - tr * .17))
        for a0, rad, ph, k2 in sp:                                     # золотые искры
            al = max(0.0, math.sin(TAU * (k2 * t + ph))) ** 2
            if al > .05:
                a = a0 + TAU * t * .5
                fr.paste(gold, (int(ex + rad * math.cos(a) - 5), int(ey + rad * math.sin(a) - 5)), scaled(star10, al))

        arr = np.asarray(fr, np.float32).copy()                        # бегущий свет по рамке + блик
        i1 = np.exp(-((t - U) % 1) * 9)[..., None]
        i2 = np.exp(-((U - (.5 - t)) % 1) * 9)[..., None]
        pulse = .22 + .08 * math.sin(TAU * t)
        arr[y0:y1, x0:x1] += Mc * (i1 * h1c * .95 + i2 * h2c * .8 + base_c * pulse)
        pos = -.15 * span + t * 1.3 * span
        arr += (np.exp(-((sweep - pos) / (span * .05)) ** 2) * 50)[..., None] * np.array(gold, np.float32) / 255.0
        seq.append(Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8)))

    # общая палитра по нескольким кадрам - цвета не «прыгают»
    pick = [seq[int(j * frames / 6)] for j in range(6)]
    mosaic = Image.new("RGB", (OW, OH * 6))
    for j, im in enumerate(pick):
        mosaic.paste(im, (0, j * OH))
    pal = mosaic.quantize(256, method=Image.Quantize.MEDIANCUT)
    imgs = [f.quantize(palette=pal, dither=Image.Dither.FLOYDSTEINBERG) for f in seq]
    buf = io.BytesIO()
    imgs[0].save(buf, "GIF", save_all=True, append_images=imgs[1:], duration=int(1000 / fps), loop=0, disposal=1)
    buf.seek(0)
    buf.name = "banner.gif"
    if return_info:
        return buf, dict(preset=pid, preset_name=pname, digest=digest)
    return buf


if __name__ == "__main__":
    import sys
    pid = sys.argv[1] if len(sys.argv) > 1 else None
    b = make_animated_banner(1, "Деменция", "dementia", preset=pid)
    open("banner_test.gif", "wb").write(b.getvalue())
    print("ok", len(b.getvalue()) // 1024, "KB")
