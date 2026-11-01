"""Draws Pip, the AI apprentice who co-writes Harkenn with Master Magic, using the character toolkit in app/starter.py.

    python tools/draw_pip.py <output dir>      # pip.png (240 px) and pip-64.png
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.starter import SPARK, Canvas, blush, brows, eye, mouth, sparkle  # noqa: E402

BODY_DARK, BODY_LIGHT = (92, 150, 230), (190, 225, 255)
HAT, HAT_LIGHT, BAND = (58, 52, 140, 255), (96, 86, 196, 255), (255, 200, 90, 255)
WAND, WAND_TIP = (120, 78, 48, 255), (255, 236, 140, 255)


def draw(c: Canvas) -> None:
    cx, cy, r = 120, 158, 64
    # soft glow behind
    c.overlay(lambda o: o.circle(cx, cy - 6, 108, (122, 162, 255, 40)))
    # wand arm: a stubby hand at the side, wand tilted up and out
    hx, hy = cx + 62, cy + 18
    tip = (hx + 34, hy - 58)
    c.line([(hx, hy), tip], WAND, 6)
    c.circle(*tip, 6, WAND_TIP)
    sparkle(c, tip[0] + 2, tip[1] - 2, 20, SPARK)
    sparkle(c, tip[0] - 22, tip[1] - 18, 8, (255, 255, 255, 230))
    sparkle(c, tip[0] + 18, tip[1] + 20, 6, (255, 255, 255, 200))
    # body
    c.shaded_ellipse(cx, cy, r, r * 0.96, BODY_DARK, BODY_LIGHT)
    c.ellipse(hx - 2, hy + 2, 13, 11, fill=(150, 195, 245, 255))  # hand over the wand
    # face
    brows(c, cx, cy - 26, 23, 12, 5, 1, 0, (40, 70, 120, 255), 3.2)
    for side in (-1, 1):
        eye(c, cx + side * 23, cy - 4, 14, look=(0.25, -0.15))
    blush(c, cx - 42, cy + 18, 10, 6, 170)
    blush(c, cx + 42, cy + 18, 10, 6, 170)
    mouth(c, cx, cy + 26, 26, "grin")
    # wizard hat, slightly bent at the tip, sitting on the head
    base_y = cy - r * 0.78
    c.ellipse(cx, base_y + 6, 68, 13, fill=HAT)
    cone = [(cx - 46, base_y + 2), (cx - 8, base_y - 74), (cx + 6, base_y - 96), (cx + 30, base_y - 90),
            (cx + 14, base_y - 70), (cx + 46, base_y + 2)]
    c.polygon(cone, HAT)
    c.polygon([(cx - 30, base_y), (cx - 6, base_y - 62), (cx + 2, base_y - 60), (cx - 14, base_y)], HAT_LIGHT)
    c.rrect(cx - 46, base_y - 12, cx + 46, base_y + 2, 4, fill=BAND)
    for (sx, sy, sr) in ((cx + 18, base_y - 34, 7), (cx - 16, base_y - 26, 5), (cx + 4, base_y - 58, 4)):
        sparkle(c, sx, sy, sr, (255, 236, 140, 255))
    c.circle(cx + 30, base_y - 90, 7, BAND)  # pompom on the tip


def render(size: int):
    c = Canvas()
    draw(c)
    return c.finish().resize((size, size))


out = Path(sys.argv[1])
render(240).save(out / "pip.png")
render(64).save(out / "pip-64.png")
print("drew", out / "pip.png")
