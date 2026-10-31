"""Draws the built-in 'default' character, so a fresh server has an animation for every state."""
from __future__ import annotations

import math
from typing import Callable

from PIL import Image, ImageDraw

from .assets import MASTER_SIZE, CharacterStore

SS = 2  # draw at 2x and downscale for antialiasing
BODY = (94, 200, 229, 255)
HIGHLIGHT = (170, 230, 245, 255)
INK = (16, 24, 48, 255)
WHITE = (255, 255, 255, 255)
CHEEK = (255, 140, 190, 255)
WAVE = (122, 162, 255, 255)

Frame = tuple[Image.Image, int]


def _box(cx: float, cy: float, rx: float, ry: float) -> list[float]:
    return [(cx - rx) * SS, (cy - ry) * SS, (cx + rx) * SS, (cy + ry) * SS]


def _face(*, dx: float = 0, dy: float = 0, r: float = 88, eye_open: float = 1.0, eye_scale: float = 1.0,
          look: tuple[float, float] = (0, 0), eyes: str = "open", mouth: float = 0.0, frown: bool = False,
          extra: Callable[[ImageDraw.ImageDraw], None] | None = None) -> Image.Image:
    img = Image.new("RGBA", (MASTER_SIZE * SS, MASTER_SIZE * SS), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    cx, cy = 120 + dx, 126 + dy
    d.ellipse(_box(cx, cy, r, r), fill=BODY)
    d.ellipse(_box(cx - r * 0.38, cy - r * 0.62, r * 0.22, r * 0.12), fill=HIGHLIGHT)
    for side in (-1, 1):
        d.ellipse(_box(cx + side * 52, cy + 22, 10, 6), fill=CHEEK)
        ex, ey = cx + side * 30, cy - 12
        if eyes == "x":
            for a, b in ((-1, -1), (-1, 1)):
                d.line([(ex + a * 10) * SS, (ey + b * 10) * SS, (ex - a * 10) * SS, (ey - b * 10) * SS],
                       fill=INK, width=5 * SS)
        elif eyes == "closed" or eye_open < 0.2:
            d.arc(_box(ex, ey - 4, 14, 10), 20, 160, fill=INK, width=4 * SS)
        else:
            rx, ry = 16 * eye_scale, 20 * eye_scale * eye_open
            d.ellipse(_box(ex, ey, rx, ry), fill=WHITE)
            px, py = ex + look[0] * 6, ey + look[1] * 8 * eye_open
            d.ellipse(_box(px, py, 9 * eye_scale, 9 * eye_scale * min(1.0, eye_open * 1.4)), fill=INK)
            d.ellipse(_box(px + 3, py - 3, 3, 3 * eye_open), fill=WHITE)
    mx, my = cx, cy + 28
    if mouth > 0.05:
        d.ellipse(_box(mx, my + 2, 13, 3 + mouth * 12), fill=INK)
    elif frown:
        d.arc(_box(mx, my + 12, 14, 10), 200, 340, fill=INK, width=4 * SS)
    else:
        d.arc(_box(mx, my - 4, 14, 10), 20, 160, fill=INK, width=4 * SS)
    if extra:
        extra(d)
    return img.resize((MASTER_SIZE, MASTER_SIZE), Image.Resampling.LANCZOS)


def idle() -> list[Frame]:
    blink = {24: 0.5, 25: 0.1, 26: 0.5}
    return [(_face(dy=math.sin(2 * math.pi * i / 30) * 4, eye_open=blink.get(i, 1.0),
                   look=(math.sin(2 * math.pi * i / 30) * 0.3, 0)), 80) for i in range(30)]


def listening() -> list[Frame]:
    frames = []
    for i in range(16):
        def waves(d: ImageDraw.ImageDraw, i: int = i) -> None:
            for k in range(3):
                phase = (i / 16 + k / 3) % 1.0
                if phase > 0.85:
                    continue
                rad = 96 + phase * 20
                for start, end in ((-35, 35), (145, 215)):
                    d.arc(_box(120, 126, rad, rad), start, end, fill=WAVE, width=4 * SS)
        frames.append((_face(r=88 + math.sin(2 * math.pi * i / 16) * 2, eye_scale=1.15, mouth=0.25,
                             extra=waves), 70))
    return frames


def thinking() -> list[Frame]:
    frames = []
    for i in range(24):
        def dots(d: ImageDraw.ImageDraw, i: int = i) -> None:
            for k in range(3):
                lift = 6 if (i // 4) % 3 == k else 0
                d.ellipse(_box(150 + k * 16, 40 - lift, 5, 5), fill=WHITE)
        frames.append((_face(look=(0.6, -0.8), dy=math.sin(2 * math.pi * i / 24) * 2, extra=dots), 90))
    return frames


def speaking() -> list[Frame]:
    return [(_face(mouth=max(0.0, min(1.0, 0.5 + 0.5 * math.sin(i * 1.3) * math.cos(i * 0.7))),
                   dy=math.sin(i * 0.8) * 1.5), 70) for i in range(16)]


def sleeping() -> list[Frame]:
    frames = []
    for i in range(32):
        def zs(d: ImageDraw.ImageDraw, i: int = i) -> None:
            for k in range(2):
                phase = (i / 32 + k / 2) % 1.0
                x, y, s = 168 + phase * 20, 76 - phase * 44, 7 + phase * 6
                d.line([(x - s) * SS, (y - s) * SS, (x + s) * SS, (y - s) * SS,
                        (x - s) * SS, (y + s) * SS, (x + s) * SS, (y + s) * SS], fill=WHITE, width=3 * SS)
        frames.append((_face(eyes="closed", r=88 + math.sin(2 * math.pi * i / 32) * 2, extra=zs), 100))
    return frames


def error() -> list[Frame]:
    shake = [0, -6, 6, -4, 4, -2, 2, 0, 0, 0, 0, 0, 0, 0]
    return [(_face(eyes="x", frown=True, dx=dx), 60) for dx in shake]


STATE_DRAWERS = {"idle": idle, "listening": listening, "thinking": thinking, "speaking": speaking,
                 "sleeping": sleeping, "error": error}


def ensure_default_character(store: CharacterStore) -> None:
    if store.get("default"):
        return
    store.create("default", "Kotodama")
    for state, draw in STATE_DRAWERS.items():
        frames = draw()
        store.save_state("default", state, [f for f, _ in frames], [ms for _, ms in frames])
