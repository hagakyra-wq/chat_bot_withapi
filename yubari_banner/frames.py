"""
frames.py - генератор рамок. Рамка = функция (rng от seed, уровень).

1 одинарная линия
2 двойная линия
3 линия + угловые вставки
4 орнамент (зубцы / засечки / волны) + тонкая внутренняя линия
5 двойная линия + орнамент + камни по углам и серединам сторон
6 как 5 + цепочка огоньков (живое свечение добавляет banner_anim)

draw_frame() -> (RGBA-слой W×H, маска линии рамки L W×H для анимации свечения)
"""
import math

from PIL import Image, ImageDraw

from .bn_common import SS, W, H, glow, lerp


def perimeter(box, r, step):
    """Точки вдоль скруглённого прямоугольника по часовой: (x, y, nx, ny), n - внешняя нормаль."""
    x0, y0, x1, y1 = box
    pts = []

    def straight(ax, ay, bx, by, nx, ny):
        n = max(1, int(math.hypot(bx - ax, by - ay) / step))
        for i in range(n):
            u = i / n
            pts.append((ax + (bx - ax) * u, ay + (by - ay) * u, nx, ny))

    def arc(cx, cy, a0):
        n = max(2, int(r * math.pi / 2 / step))
        for i in range(n):
            a = a0 + (math.pi / 2) * i / n
            pts.append((cx + r * math.cos(a), cy + r * math.sin(a), math.cos(a), math.sin(a)))

    straight(x0 + r, y0, x1 - r, y0, 0, -1)
    arc(x1 - r, y0 + r, -math.pi / 2)
    straight(x1, y0 + r, x1, y1 - r, 1, 0)
    arc(x1 - r, y1 - r, 0)
    straight(x1 - r, y1, x0 + r, y1, 0, 1)
    arc(x0 + r, y1 - r, math.pi / 2)
    straight(x0, y1 - r, x0, y0 + r, -1, 0)
    arc(x0 + r, y0 + r, math.pi)
    return pts


def _gem(d, x, y, r, col):
    d.polygon([(x, y - r * 1.25), (x + r, y), (x, y + r * 1.25), (x - r, y)], fill=col + (255,),
              outline=(255, 255, 255, 230))
    lc = lerp(col, (255, 255, 255), .55)
    d.polygon([(x, y - r * .7), (x + r * .5, y), (x, y + r * .7), (x - r * .5, y)], fill=lc + (255,))
    d.ellipse([x - r * .18, y - r * .55, x + r * .18, y - r * .2], fill=(255, 255, 255, 255))


def _ornament(rng, d, box, radius, acc, level):
    S = SS
    kind = rng.choice(["teeth", "ticks", "waves"])
    c1 = acc[1 % len(acc)]
    c2 = acc[2 % len(acc)]
    inset = 7
    ib = (box[0] + inset, box[1] + inset, box[2] - inset, box[3] - inset)
    step = {"teeth": 15, "ticks": 9, "waves": 20}[kind]
    for i, (x, y, nx, ny) in enumerate(perimeter(ib, radius - inset, step)):
        x, y = x * S, y * S
        tx, ty = -ny, nx
        if kind == "ticks":
            l = (6 if i % 3 else 12) * S
            d.line([(x, y), (x - nx * l, y - ny * l)], fill=c1 + (200,), width=S)
        elif kind == "teeth":
            w, h = step * .38 * S, 10 * S
            d.polygon([(x + tx * w, y + ty * w), (x - tx * w, y - ty * w), (x - nx * h, y - ny * h)],
                      fill=(c1 if i % 2 else c2) + (170,))
        else:
            r = step * .5 * S
            cx, cy = x - nx * r * .9, y - ny * r * .9
            d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=(c1 if i % 2 else c2) + (200,), width=int(1.4 * S))


def _brackets(d, box, acc, w=5, ln=62, off=6):
    S = SS
    x0, y0, x1, y1 = [v for v in box]
    c = acc[1 % len(acc)]
    for (cx, cy, sx, sy) in ((x0, y0, 1, 1), (x1, y0, -1, 1), (x1, y1, -1, -1), (x0, y1, 1, -1)):
        px, py = (cx - sx * off) * S, (cy - sy * off) * S
        d.line([(px + sx * ln * S, py), (px, py), (px, py + sy * ln * S)], fill=c + (255,),
               width=w * S, joint="curve")
        r = 5 * S
        d.ellipse([px + sx * ln * S - r, py - r, px + sx * ln * S + r, py + r], fill=(255, 255, 255, 255))
        d.ellipse([px - r, py + sy * ln * S - r, px + r, py + sy * ln * S + r], fill=(255, 255, 255, 255))


def draw_frame(rng, level, box, radius, acc):
    S = SS
    lay = Image.new("RGBA", (W * S, H * S), (0, 0, 0, 0))
    d = ImageDraw.Draw(lay)
    c0, c1 = acc[0], acc[1 % len(acc)]
    x0, y0, x1, y1 = box

    def rr(b, rad, col, w):
        d.rounded_rectangle([v * S for v in b], rad * S, outline=col, width=max(1, int(w * S)))

    rr(box, radius, c0 + (240,), 2.5)                                   # основная линия
    if level in (2, 5, 6):
        rr((x0 + 9, y0 + 9, x1 - 9, y1 - 9), radius - 9, c1 + (170,), 1.3)  # двойная
    if level == 3:
        _brackets(d, box, acc)
    if level >= 4:
        _ornament(rng, d, box, radius, acc, level)
        if level == 4:
            rr((x0 + 22, y0 + 22, x1 - 22, y1 - 22), radius - 22, c1 + (110,), 1)
    if level >= 5:
        _brackets(d, box, acc, w=4, ln=48, off=2)
        mx, my = (x0 + x1) / 2, (y0 + y1) / 2
        gems = [(x0 + radius * .55, y0 + radius * .55), (x1 - radius * .55, y0 + radius * .55),
                (x1 - radius * .55, y1 - radius * .55), (x0 + radius * .55, y1 - radius * .55)]
        for i, (gx, gy) in enumerate(gems):
            _gem(d, gx * S, gy * S, 11 * S, acc[(i + 2) % len(acc)])
        for i, (gx, gy) in enumerate([(mx, y0), (mx, y1), (x0, my), (x1, my)]):
            _gem(d, gx * S, gy * S, 7 * S, acc[(i + 3) % len(acc)])
    if level == 6:
        for x, y, nx, ny in perimeter((x0 - 6, y0 - 6, x1 + 6, y1 + 6), radius + 6, 26):
            r = 2 * S
            d.ellipse([x * S - r, y * S - r, x * S + r, y * S + r], fill=(255, 255, 255, 190))

    g = glow(lay, 5 * S, 1.3)
    out = Image.alpha_composite(g, lay).resize((W, H), Image.LANCZOS)

    # маска центральной линии - по ней бежит живое свечение
    m = Image.new("L", (W * S, H * S), 0)
    ImageDraw.Draw(m).rounded_rectangle([v * S for v in box], radius * S, outline=255, width=4 * S)
    from PIL import ImageFilter
    m = m.resize((W, H), Image.LANCZOS).filter(ImageFilter.GaussianBlur(1.2))
    return out, m
