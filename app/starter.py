"""Built-in characters, drawn in code.

Four characters (Kotodama, Obake, Neko, Robo), each with an animation for every state. Frames are drawn at 2x and
downscaled for anti-aliasing, and stored as full-alpha APNG masters.

Bump BUILTIN_VERSION whenever a drawing changes: on startup, built-in characters that weren't customized in the admin
UI are redrawn (or copied from the prebuilt set baked into the container image).

    python -m app.starter <dir>      # prebuild into <dir> (used by the Dockerfile)
"""
from __future__ import annotations

import json
import logging
import math
import shutil
import sys
from pathlib import Path
from typing import Callable

from PIL import Image, ImageDraw

from .assets import MASTER_SIZE, CharacterStore

log = logging.getLogger("harkenn.starter")

BUILTIN_VERSION = 3
SS = 2  # supersampling
W = MASTER_SIZE
TAU = 2 * math.pi

# frames, milliseconds per frame
TIMING = {"idle": (72, 60), "listening": (40, 50), "thinking": (48, 60), "speaking": (32, 60),
          "sleeping": (64, 80), "error": (36, 50), "muted": (48, 80)}

INK = (28, 30, 52, 255)
WHITE = (255, 255, 255, 255)
TONGUE = (240, 110, 130, 255)
PINK = (255, 120, 170)
SPARK = (255, 236, 140, 255)
WAVE = (130, 170, 255, 255)

Color = tuple[int, int, int, int]


# helpers

def mix(a: tuple, b: tuple, t: float) -> Color:
    t = max(0.0, min(1.0, t))
    out = tuple(round(x + (y - x) * t) for x, y in zip(a, b))
    return out if len(out) == 4 else out + (255,)


def ease(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return 0.5 - 0.5 * math.cos(math.pi * t)


def osc(i: int, n: int, cycles: float = 1, phase: float = 0) -> float:
    return math.sin(TAU * (cycles * i / n + phase))


def pulse(i: int, start: int, length: int) -> float:
    """0 -> 1 -> 0 over [start, start+length)."""
    if not start <= i < start + length:
        return 0.0
    return math.sin(math.pi * (i - start + 0.5) / length)


def window(i: int, start: int, end: int, fade: int = 4) -> float:
    """1 inside [start, end) with eased edges, 0 outside."""
    if i < start - fade or i >= end + fade:
        return 0.0
    if i < start:
        return ease((i - start + fade) / fade)
    if i >= end:
        return ease((end + fade - i) / fade)
    return 1.0


def blink(i: int, *starts: int) -> float:
    shape = (0.55, 0.08, 0.08, 0.55)
    for s in starts:
        if 0 <= i - s < len(shape):
            return shape[i - s]
    return 1.0


class Canvas:
    def __init__(self) -> None:
        self.img = Image.new("RGBA", (W * SS, W * SS), (0, 0, 0, 0))
        self.d = ImageDraw.Draw(self.img)

    @staticmethod
    def _b(cx: float, cy: float, rx: float, ry: float) -> list[float]:
        return [(cx - rx) * SS, (cy - ry) * SS, (cx + rx) * SS, (cy + ry) * SS]

    @staticmethod
    def _w(width: float) -> int:
        return max(1, round(width * SS))

    def ellipse(self, cx, cy, rx, ry, fill=None, outline=None, width=0.0):
        if rx > 0 and ry > 0:
            self.d.ellipse(self._b(cx, cy, rx, ry), fill=fill, outline=outline, width=self._w(width) if outline else 0)

    def circle(self, cx, cy, r, fill):
        self.ellipse(cx, cy, r, r, fill)

    def arc(self, cx, cy, rx, ry, start, end, fill, width):
        if rx > 0 and ry > 0:
            self.d.arc(self._b(cx, cy, rx, ry), start, end, fill=fill, width=self._w(width))

    def chord(self, cx, cy, rx, ry, start, end, fill):
        if rx > 0 and ry > 0:
            self.d.chord(self._b(cx, cy, rx, ry), start, end, fill=fill)

    def line(self, pts, fill, width, caps=True):
        pts = [(x * SS, y * SS) for x, y in pts]
        self.d.line(pts, fill=fill, width=self._w(width), joint="curve")
        if caps:
            r = width * SS / 2
            for x, y in (pts[0], pts[-1]):
                self.d.ellipse([x - r, y - r, x + r, y + r], fill=fill)

    def polygon(self, pts, fill):
        self.d.polygon([(x * SS, y * SS) for x, y in pts], fill=fill)

    def rrect(self, x0, y0, x1, y1, r, fill=None, outline=None, width=0.0):
        self.d.rounded_rectangle([x0 * SS, y0 * SS, x1 * SS, y1 * SS], radius=r * SS, fill=fill, outline=outline,
                                 width=self._w(width) if outline else 0)

    def overlay(self, draw: Callable[["Canvas"], None]) -> None:
        """Draw on a separate layer and blend it in, for semi-transparent shapes."""
        layer = Canvas()
        draw(layer)
        self.img.alpha_composite(layer.img)

    def shaded_ellipse(self, cx, cy, rx, ry, dark, light, steps=16, hx=-0.34, hy=-0.40):
        """A soft 3D-looking ball: concentric ellipses drifting toward the highlight."""
        for k in range(steps):
            t = k / (steps - 1)
            s = 1 - 0.6 * t
            self.ellipse(cx + hx * rx * t * 0.8, cy + hy * ry * t * 0.8, rx * s, ry * s, fill=mix(dark, light, t ** 0.9))

    def finish(self) -> Image.Image:
        return self.img.resize((W, W), Image.Resampling.LANCZOS)


# face parts

def eye(c: Canvas, cx, cy, s, *, open_=1.0, look=(0.0, 0.0), style="normal", iris=INK, pupil=1.0, slit=False):
    """An eye of size s. Styles: normal, happy (^), closed (sleeping), x, spiral (with look[0] as phase)."""
    if style == "happy":
        c.arc(cx, cy + s * 0.45, s * 0.85, s * 0.75, 205, 335, INK, s * 0.3)
        return
    if style == "closed":
        c.arc(cx, cy - s * 0.25, s * 0.8, s * 0.6, 25, 155, INK, s * 0.26)
        return
    if style == "x":
        for sign in (-1, 1):
            c.line([(cx - s * 0.62, cy - s * 0.62 * sign), (cx + s * 0.62, cy + s * 0.62 * sign)], INK, s * 0.3)
        return
    if style == "spiral":
        pts = []
        for k in range(40):
            a = k * 0.42 + look[0] * TAU
            r = s * 0.9 * k / 40
            pts.append((cx + math.cos(a) * r, cy + math.sin(a) * r))
        c.line(pts, INK, s * 0.17)
        return
    if open_ < 0.2:
        c.line([(cx - s * 0.75, cy + s * 0.1), (cx + s * 0.75, cy + s * 0.1)], INK, s * 0.26)
        return
    ry = s * open_
    c.ellipse(cx, cy, s * 0.82, ry, fill=WHITE)
    ir = s * 0.58 * pupil
    px = cx + look[0] * s * 0.26
    py = cy + look[1] * s * 0.32 * open_
    c.ellipse(px, py, ir, min(ir, ry * 0.95), fill=iris)
    if slit:
        c.ellipse(px, py, ir * 0.22 * pupil, min(ir * 0.9, ry * 0.9), fill=INK)
    hr = ir * 0.34
    c.ellipse(px + ir * 0.35, py - ir * 0.38, hr, min(hr, ry * 0.3), fill=WHITE)
    c.ellipse(px - ir * 0.32, py + ir * 0.34, hr * 0.45, min(hr * 0.45, ry * 0.15), fill=WHITE)


def brow(c: Canvas, cx, cy, w, angle, color=INK, width=4.0):
    """A brow centred at (cx, cy); positive angle raises the inner end for the right brow (mirror for left)."""
    a = math.radians(angle)
    dx, dy = math.cos(a) * w / 2, math.sin(a) * w / 2
    c.line([(cx - dx, cy + dy), (cx + dx, cy - dy)], color, width)


def brows(c: Canvas, cx, cy, gap, w, angle, lift=0.0, asym=0.0, color=INK, width=4.0):
    brow(c, cx - gap, cy - lift - asym, w, -angle, color, width)
    brow(c, cx + gap, cy - lift + asym, w, angle, color, width)


def mouth(c: Canvas, cx, cy, w, kind="smile", amount=0.0):
    if kind == "smile":
        c.arc(cx, cy - w * 0.32, w * 0.5, w * 0.48, 28, 152, INK, w * 0.13)
    elif kind == "grin":
        c.chord(cx, cy - w * 0.15, w * 0.5, w * 0.45, 0, 180, INK)
        c.chord(cx, cy + w * 0.12, w * 0.28, w * 0.16, 180, 360, TONGUE)
    elif kind == "open":
        ry = w * (0.1 + 0.36 * amount)
        c.ellipse(cx, cy, w * (0.34 - 0.06 * amount), ry, fill=INK)
        if amount > 0.3:
            c.ellipse(cx, cy + ry * 0.45, w * 0.2, ry * 0.42, fill=TONGUE)
    elif kind == "o":
        c.ellipse(cx, cy, w * (0.16 + 0.06 * amount), w * (0.18 + 0.1 * amount), fill=INK)
    elif kind == "flat":
        c.line([(cx - w * 0.3, cy), (cx + w * 0.3, cy)], INK, w * 0.12)
    elif kind == "smirk":
        c.line([(cx - w * 0.3, cy + 2), (cx + w * 0.1, cy + 1), (cx + w * 0.32, cy - w * 0.12)], INK, w * 0.12)
    elif kind == "wavy":
        pts = [(cx - w * 0.4 + w * 0.8 * k / 16, cy + math.sin(k / 16 * TAU * 2 + amount * TAU) * w * 0.07)
               for k in range(17)]
        c.line(pts, INK, w * 0.11)
    elif kind == "frown":
        c.arc(cx, cy + w * 0.36, w * 0.42, w * 0.38, 212, 328, INK, w * 0.13)
    elif kind == "zip":  # zipped shut, for muted
        c.line([(cx - w * 0.38, cy), (cx + w * 0.38, cy)], INK, w * 0.1)
        for k in range(7):
            x = cx - w * 0.33 + k * w * 0.11
            c.line([(x, cy - w * 0.09), (x, cy + w * 0.09)], INK, w * 0.05, caps=False)
        c.rrect(cx + w * 0.36, cy - w * 0.03, cx + w * 0.5, cy + w * 0.2, w * 0.05, fill=(205, 210, 225, 255),
                outline=INK, width=w * 0.04)
    elif kind == "cat":
        r = w * 0.22
        c.arc(cx - r, cy - r * 0.2, r, r * 0.9, 20, 160, INK, w * 0.1)
        c.arc(cx + r, cy - r * 0.2, r, r * 0.9, 20, 160, INK, w * 0.1)
        if amount > 0.05:
            c.chord(cx, cy + r * 0.7, w * 0.2, w * (0.06 + 0.24 * amount), 0, 180, INK)


def blush(c: Canvas, cx, cy, rx, ry, alpha=150, color=PINK):
    c.overlay(lambda o: o.ellipse(cx, cy, rx, ry, fill=color + (alpha,)))


def sparkle(c: Canvas, x, y, r, color=SPARK):
    if r <= 0.5:
        return
    k = 0.28
    c.polygon([(x, y - r), (x + r * k, y - r * k), (x + r, y), (x + r * k, y + r * k), (x, y + r),
               (x - r * k, y + r * k), (x - r, y), (x - r * k, y - r * k)], color)


def zzz(c: Canvas, i, n, x, y, color=WHITE):
    for k in range(3):
        t = (i / n + k / 3) % 1.0
        s = 5 + 8 * t
        zx, zy = x + 22 * t + 4 * math.sin(t * TAU), y - 46 * t
        if t < 0.9:
            c.line([(zx - s, zy - s), (zx + s, zy - s), (zx - s, zy + s), (zx + s, zy + s)], color, 2.6 + 1.2 * t,
                   caps=False)


def sweat(c: Canvas, x, y, s, t=0.0):
    y += 6 * t
    c.polygon([(x, y - s * 1.6), (x + s * 0.8, y), (x - s * 0.8, y)], (150, 210, 255, 255))
    c.circle(x, y + s * 0.15, s * 0.85, (150, 210, 255, 255))
    c.circle(x - s * 0.3, y - s * 0.1, s * 0.25, WHITE)


def sound_waves(c: Canvas, i, n, cx, cy, r, color=WAVE):
    for k in range(3):
        t = (i / n * 2 + k / 3) % 1.0
        if t > 0.85:
            continue
        rad = r + 4 + t * 22
        w = 4.5 * (1 - t) + 1.5
        for start, end in ((-32, 32), (148, 212)):
            c.arc(cx, cy, rad, rad, start, end, color, w)


def think_dots(c: Canvas, i, x, y, color=WHITE):
    for k in range(3):
        lift = 7 * pulse(i % 24, k * 6, 10)
        c.circle(x + k * 15, y - lift, 4.5 + k * 0.6, color)


def muted_badge(c: Canvas, x, y, s, slash=(255, 90, 90, 255)):
    """A crossed-out microphone in a dark bubble."""
    c.overlay(lambda o: o.circle(x, y, s, (20, 24, 40, 175)))
    c.rrect(x - s * 0.2, y - s * 0.56, x + s * 0.2, y + s * 0.1, s * 0.2, fill=WHITE)
    c.arc(x, y - s * 0.14, s * 0.36, s * 0.36, 20, 160, WHITE, s * 0.09)
    c.line([(x, y + s * 0.24), (x, y + s * 0.42)], WHITE, s * 0.09)
    c.line([(x - s * 0.52, y - s * 0.52), (x + s * 0.52, y + s * 0.52)], slash, s * 0.13)


def speech_pattern(i: int) -> float:
    return max(0.0, min(1.0, 0.5 + 0.5 * math.sin(i * 1.27) * math.cos(i * 0.61) + 0.25 * math.sin(i * 2.9)))


# characters

class Character:
    id = ""
    name = ""
    order = 0

    def frame(self, state: str, i: int, n: int) -> Image.Image:
        c = Canvas()
        getattr(self, state)(c, i, n)
        return c.finish()


class Kotodama(Character):
    """A round word-spirit with a sprout on its head."""
    id, name, order = "default", "Kotodama", 0
    DARK, LIGHT = (36, 140, 185), (160, 236, 250)
    LEAF = (110, 205, 120, 255)

    def body(self, c, cx, cy, r, squash=0.0, sway=0.0, droop=0.0):
        rx, ry = r * (1 + squash), r * (1 - squash)
        top = cy - ry
        # sprout
        a = math.radians(-90 + sway * 22 + droop * 60)
        sx, sy = cx + math.cos(a) * 20, top + math.sin(a) * 20 + 4
        c.line([(cx, top + 6), ((cx + sx) / 2 + sway * 3, (top + sy) / 2), (sx, sy)], (80, 160, 90, 255), 3.4)
        for side in (-1, 1):
            la = a + side * 0.9 + sway * 0.2
            pts = []
            for k in range(13):
                t = k / 12
                along, across = 16 * t, 6 * math.sin(math.pi * t) * side
                pts.append((sx + math.cos(la) * along - math.sin(la) * across,
                            sy + math.sin(la) * along + math.cos(la) * across))
            c.polygon(pts, self.LEAF)
        c.shaded_ellipse(cx, cy, rx, ry, self.DARK, self.LIGHT)
        return rx, ry

    def face(self, c, cx, cy, *, eyes="normal", open_=1.0, look=(0, 0), pupil=1.0, brow_angle=0.0, brow_lift=0.0,
             brow_asym=0.0, show_brows=True, mouth_kind="smile", amount=0.0, blush_alpha=150, eye_size=17):
        if show_brows:
            brows(c, cx, cy - 36, 29, 15, brow_angle, brow_lift, brow_asym, (30, 90, 120, 255), 3.6)
        for side in (-1, 1):
            eye(c, cx + side * 29, cy - 10, eye_size, open_=open_, look=look, style=eyes, pupil=pupil)
        blush(c, cx - 50, cy + 16, 12, 7, blush_alpha)
        blush(c, cx + 50, cy + 16, 12, 7, blush_alpha)
        mouth(c, cx, cy + 28, 30, mouth_kind, amount)

    def idle(self, c, i, n):
        b = osc(i, n)
        cx, cy = 120, 132 + b * 4
        look_x = -0.9 * window(i, 28, 36) + 0.9 * window(i, 44, 52)
        self.body(c, cx, cy, 82, squash=0.025 * b, sway=osc(i, n, 2))
        self.face(c, cx, cy, open_=blink(i, 14, 60), look=(look_x, 0), brow_lift=2 * window(i, 44, 52),
                  mouth_kind="grin" if 62 <= i < 70 else "smile")
        sparkle(c, 162, 52, 7 * pulse(i, 60, 12))
        sparkle(c, 70, 62, 5 * pulse(i, 64, 8))

    def listening(self, c, i, n):
        b = osc(i, n, 2)
        cx, cy = 120, 130
        sound_waves(c, i, n, cx, cy, 86)
        self.body(c, cx, cy, 82 + b * 2, squash=-0.02, sway=0.4 * osc(i, n))
        self.face(c, cx, cy, eye_size=19, pupil=1.15, look=(0, -0.15), brow_angle=6, brow_lift=6,
                  mouth_kind="o", amount=0.3 + 0.2 * b)

    def thinking(self, c, i, n):
        cx, cy = 120 + 3 * osc(i, n), 134
        self.body(c, cx, cy, 80, squash=0.02 * osc(i, n, 2), sway=0.6)
        self.face(c, cx, cy, look=(0.75, -0.9), brow_angle=-4, brow_asym=5, brow_lift=2,
                  mouth_kind="smirk" if (i // 12) % 2 else "flat")
        think_dots(c, i, 156, 44)

    def speaking(self, c, i, n):
        m = speech_pattern(i)
        cx, cy = 120, 132 - 3 * m
        self.body(c, cx, cy, 82, squash=0.03 * (m - 0.5), sway=osc(i, n, 2) * 0.5)
        self.face(c, cx, cy, open_=0.9 if i % 16 < 14 else blink(i % 16, 14), brow_lift=3 * m, brow_angle=3 * m,
                  mouth_kind="open", amount=m, eyes="happy" if 20 <= i < 26 else "normal")

    def sleeping(self, c, i, n):
        b = osc(i, n)
        cx, cy = 120, 138 + b * 3
        self.body(c, cx, cy, 80, squash=0.04 * b, sway=-0.5, droop=0.4)
        self.face(c, cx, cy, eyes="closed", show_brows=False, mouth_kind="o", amount=0.2 + 0.2 * b, blush_alpha=90)
        bubble = (i % 32) / 32
        if bubble < 0.85:
            c.overlay(lambda o: o.circle(cx + 22, cy + 34, 3 + 11 * bubble, (200, 235, 255, 140)))
        zzz(c, i, n, 166, 70)

    def error(self, c, i, n):
        shake = 5 * math.sin(i * 2.1) * (1 - i / n) if i < n // 2 else 0
        cx, cy = 120 + shake, 134
        self.body(c, cx, cy, 80, squash=0.03 * osc(i, n, 3), droop=1.0)
        self.face(c, cx, cy, eyes="spiral", look=(i / n * 2, 0), brow_angle=-10, brow_lift=-2,
                  mouth_kind="wavy", amount=i / n * 2, blush_alpha=60)
        sweat(c, cx + 66, cy - 42, 7, (i % 18) / 18)

    def muted(self, c, i, n):
        b = osc(i, n)
        cx, cy = 120, 134 + b * 3
        peek = window(i, 18, 30)  # glances up at the badge
        self.body(c, cx, cy, 81, squash=0.02 * b, sway=0.3 * osc(i, n), droop=0.15)
        self.face(c, cx, cy, open_=blink(i, 8, 40), look=(0.6 * peek, -0.8 * peek), brow_angle=5, brow_lift=2,
                  brow_asym=2, mouth_kind="zip", blush_alpha=180)
        muted_badge(c, 182, 50 + 3 * osc(i, n, 1, 0.25), 20)


class Obake(Character):
    """A friendly ghost that floats."""
    id, name, order = "obake", "Obake", 1
    BODY, SHADE = (246, 246, 255, 238), (206, 210, 244, 238)

    def ghost(self, c, cx, cy, phase, arm_l=0.0, arm_r=0.0, alpha=238):
        r, h = 70, 92
        pts = [(cx + math.cos(a) * r, cy + math.sin(a) * r)
               for a in (math.pi + math.pi * k / 30 for k in range(31))]
        pts.append((cx + r, cy + h - 10))
        for k in range(41):
            x = cx + r - 2 * r * k / 40
            pts.append((x, cy + h + 9 * math.sin(k / 40 * TAU * 2 + phase * TAU)))
        pts.append((cx - r, cy + h - 10))
        body = self.BODY[:3] + (alpha,)
        shade = self.SHADE[:3] + (alpha,)
        # shadow on the floor
        c.overlay(lambda o: o.ellipse(cx, 226, 46 - (cy - 104) * 0.4, 6, fill=(0, 0, 0, 60)))
        # arms
        for side, lift in ((-1, arm_l), (1, arm_r)):
            ax, ay = cx + side * (r + 4), cy + 52 - lift * 26
            c.overlay(lambda o, ax=ax, ay=ay: o.ellipse(ax, ay, 14, 10, fill=shade))
        c.overlay(lambda o: o.polygon(pts, shade))
        inner = [(cx + (x - cx) * 0.9 - 6, cy + (y - cy) * 0.93 - 6) for x, y in pts]
        c.overlay(lambda o: o.polygon(inner, body))

    def face(self, c, cx, cy, **kw):
        style = kw.pop("eyes", "normal")
        open_ = kw.pop("open_", 1.0)
        look = kw.pop("look", (0, 0))
        mouth_kind = kw.pop("mouth_kind", "smile")
        amount = kw.pop("amount", 0.0)
        for side in (-1, 1):
            eye(c, cx + side * 24, cy + 4, 15, open_=open_, look=look, style=style, iris=(40, 40, 80, 255),
                pupil=kw.get("pupil", 1.1))
        blush(c, cx - 42, cy + 24, 11, 6, 140)
        blush(c, cx + 42, cy + 24, 11, 6, 140)
        mouth(c, cx, cy + 32, 24, mouth_kind, amount)

    def idle(self, c, i, n):
        cy = 104 + 7 * osc(i, n)
        self.ghost(c, 120, cy, i / n * 2, arm_r=0.5 * pulse(i, 40, 20))
        look = (-0.8 * window(i, 20, 30) + 0.8 * window(i, 50, 58), 0)
        self.face(c, 120, cy, open_=blink(i, 12, 62), look=look, mouth_kind="grin" if 40 <= i < 58 else "smile")

    def listening(self, c, i, n):
        cy = 100 + 3 * osc(i, n, 2)
        sound_waves(c, i, n, 120, cy + 30, 78)
        self.ghost(c, 120, cy, i / n * 2, arm_l=0.9, arm_r=0.9)
        self.face(c, 120, cy, pupil=1.3, look=(0, -0.2), mouth_kind="o", amount=0.4)

    def thinking(self, c, i, n):
        cy = 106 + 4 * osc(i, n)
        self.ghost(c, 120 + 4 * osc(i, n, 0.5), cy, i / n, arm_r=0.7)
        self.face(c, 120, cy, look=(-0.8, -0.9), mouth_kind="flat")
        think_dots(c, i, 156, 34, (200, 205, 255, 255))

    def speaking(self, c, i, n):
        m = speech_pattern(i)
        cy = 104 - 4 * m
        self.ghost(c, 120, cy, i / n * 2, arm_l=0.6 * m, arm_r=0.6 * (1 - m))
        self.face(c, 120, cy, mouth_kind="open", amount=m, eyes="happy" if 8 <= i < 13 else "normal")

    def sleeping(self, c, i, n):
        cy = 116 + 4 * osc(i, n)
        self.ghost(c, 120, cy, i / n, alpha=200)
        self.face(c, 120, cy, eyes="closed", mouth_kind="o", amount=0.1 + 0.2 * osc(i, n))
        zzz(c, i, n, 168, 66, (220, 225, 255, 255))

    def error(self, c, i, n):
        flicker = 120 if (i % 6) < 2 else 238
        cy = 106
        self.ghost(c, 120 + 4 * math.sin(i * 2.3) * (i < n // 2), cy, i / n * 3, alpha=flicker, arm_l=1, arm_r=1)
        self.face(c, 120, cy, eyes="x", mouth_kind="wavy", amount=i / n * 2)
        sweat(c, 186, 82, 7, (i % 18) / 18)

    def muted(self, c, i, n):
        cy = 106 + 5 * osc(i, n)
        peek = window(i, 20, 32)
        self.ghost(c, 120, cy, i / n, arm_l=0.55, arm_r=0.55)  # paws over its mouth
        self.face(c, 120, cy, open_=blink(i, 10, 42), look=(0.6 * peek, -0.7 * peek), mouth_kind="zip")
        muted_badge(c, 186, 46 + 3 * osc(i, n, 1, 0.25), 20)


class Neko(Character):
    """An orange cat."""
    id, name, order = "neko", "Neko", 2
    DARK, LIGHT = (222, 128, 52), (255, 200, 130)
    STRIPE = (200, 104, 40, 255)
    MUZZLE = (255, 238, 220, 255)
    EAR_IN = (255, 170, 185, 255)
    IRIS = (110, 190, 90, 255)

    def ear(self, c, cx, cy, side, angle, length=46):
        base = math.radians(-90 + side * 38 + side * angle)
        tip = (cx + math.cos(base) * length, cy + math.sin(base) * length)
        perp = base + math.pi / 2
        b1 = (cx + math.cos(perp) * 24, cy + math.sin(perp) * 24)
        b2 = (cx - math.cos(perp) * 24, cy - math.sin(perp) * 24)
        c.polygon([b1, tip, b2], mix(self.DARK, self.LIGHT, 0.2))
        inner_tip = (cx + math.cos(base) * length * 0.72, cy + math.sin(base) * length * 0.72)
        c.polygon([(cx + (b1[0] - cx) * 0.55, cy + (b1[1] - cy) * 0.55), inner_tip,
                   (cx + (b2[0] - cx) * 0.55, cy + (b2[1] - cy) * 0.55)], self.EAR_IN)

    def head(self, c, cx, cy, ear_l=0.0, ear_r=0.0, whisker=0.0):
        self.ear(c, cx - 50, cy - 42, -1, ear_l)
        self.ear(c, cx + 50, cy - 42, 1, ear_r)
        c.shaded_ellipse(cx, cy, 88, 74, self.DARK, self.LIGHT)
        for k in (-1, 0, 1):
            c.line([(cx + k * 13, cy - 70), (cx + k * 11, cy - 54)], self.STRIPE, 4.5)
        c.ellipse(cx, cy + 30, 36, 24, fill=self.MUZZLE)
        for side in (-1, 1):
            for k in (-1, 0, 1):
                y = cy + 26 + k * 7
                c.line([(cx + side * 34, y), (cx + side * 76, y + k * 7 + whisker * 3 * side)], (90, 60, 50, 255),
                       1.6, caps=False)

    def face(self, c, cx, cy, *, eyes="normal", open_=1.0, look=(0, 0), pupil=1.0, mouth_amount=0.0,
             mouth_kind="cat"):
        for side in (-1, 1):
            eye(c, cx + side * 32, cy - 8, 18, open_=open_, look=look, style=eyes, iris=self.IRIS, pupil=pupil,
                slit=pupil < 1.2)
        c.polygon([(cx - 6, cy + 16), (cx + 6, cy + 16), (cx, cy + 22)], (235, 110, 140, 255))
        mouth(c, cx, cy + 28, 26, mouth_kind, mouth_amount)
        blush(c, cx - 54, cy + 14, 11, 6, 120)
        blush(c, cx + 54, cy + 14, 11, 6, 120)

    def idle(self, c, i, n):
        cy = 134 + 3 * osc(i, n)
        twitch = 14 * pulse(i, 34, 6)
        self.head(c, 120, cy, ear_r=twitch, whisker=osc(i, n, 2))
        slow_blink = 0.15 if 56 <= i < 64 else 1.0
        look = (0.8 * window(i, 16, 26) - 0.6 * window(i, 40, 46), 0)
        self.face(c, 120, cy, eyes="happy" if slow_blink < 1 else "normal", look=look)

    def listening(self, c, i, n):
        cy = 132
        sound_waves(c, i, n, 120, cy, 92, (255, 200, 120, 255))
        perk = -16 + 4 * osc(i, n, 2)
        self.head(c, 120, cy, ear_l=perk, ear_r=perk, whisker=osc(i, n, 4))
        self.face(c, 120, cy, pupil=1.45, look=(0, -0.1), mouth_amount=0.2)

    def thinking(self, c, i, n):
        cy = 136
        self.head(c, 120 + 2 * osc(i, n), cy, ear_l=18, ear_r=-6, whisker=0.5)
        self.face(c, 120, cy, look=(-0.8, -0.9))
        think_dots(c, i, 150, 40)

    def speaking(self, c, i, n):
        m = speech_pattern(i)
        cy = 134 - 2 * m
        self.head(c, 120, cy, ear_l=-6 * m, ear_r=-6 * m, whisker=m)
        self.face(c, 120, cy, mouth_amount=m, eyes="happy" if 18 <= i < 23 else "normal")

    def sleeping(self, c, i, n):
        cy = 142 + 3 * osc(i, n)
        self.head(c, 120, cy, ear_l=14, ear_r=14)
        self.face(c, 120, cy, eyes="closed")
        zzz(c, i, n, 170, 72)

    def error(self, c, i, n):
        shake = 5 * math.sin(i * 2.4) if i < n // 2 else 0
        cy = 136
        self.head(c, 120 + shake, cy, ear_l=48, ear_r=48, whisker=-1)
        self.face(c, 120 + shake, cy, eyes="x", mouth_kind="wavy", mouth_amount=i / n * 2)
        sweat(c, 188, 84, 7, (i % 18) / 18)

    def muted(self, c, i, n):
        cy = 136 + 2 * osc(i, n)
        flick = 10 * pulse(i, 26, 5)  # one ear flicks
        self.head(c, 120, cy, ear_l=10, ear_r=10 + flick, whisker=0.3 * osc(i, n))
        look = (0.6 * window(i, 12, 22), -0.6 * window(i, 12, 22))
        self.face(c, 120, cy, open_=blink(i, 34) * 0.85, look=look, mouth_kind="zip")
        muted_badge(c, 186, 52 + 3 * osc(i, n, 1, 0.25), 20)


class Robo(Character):
    """A little robot with an LED face."""
    id, name, order = "robo", "Robo", 3
    METAL_DARK, METAL_LIGHT = (130, 142, 160), (214, 222, 234)
    SCREEN = (18, 24, 40, 255)
    LED = {"idle": (90, 220, 255), "listening": (110, 255, 160), "thinking": (255, 210, 90),
           "speaking": (255, 120, 210), "sleeping": (80, 110, 200), "error": (255, 80, 80),
           "muted": (255, 150, 80)}

    def head(self, c, cx, cy, led, glow, antenna_tilt=0.0):
        # antenna
        tip = (cx + antenna_tilt * 10, cy - 98)
        c.line([(cx, cy - 72), tip], (150, 160, 175, 255), 4)
        c.overlay(lambda o: o.circle(*tip, 9 + 9 * glow, led + (int(70 * glow),)))
        c.circle(*tip, 7, mix(led, (255, 255, 255), 0.35 * glow))
        # ears
        for side in (-1, 1):
            c.rrect(cx + side * 88 - 8, cy - 22, cx + side * 88 + 8, cy + 22, 6, fill=(120, 130, 148, 255))
            c.circle(cx + side * 88, cy, 4, led + (255,))
        # head with shading
        for k in range(10):
            t = k / 9
            inset = 7 * t
            c.rrect(cx - 84 + inset - 3 * t, cy - 74 + inset - 4 * t, cx + 84 - inset - 3 * t, cy + 70 - inset - 4 * t,
                    26 - 2 * t, fill=mix(self.METAL_DARK, self.METAL_LIGHT, t))
        c.rrect(cx - 66, cy - 52, cx + 66, cy + 50, 18, fill=self.SCREEN, outline=(70, 80, 100, 255), width=2)
        # screen reflection
        c.overlay(lambda o: o.polygon([(cx - 60, cy - 46), (cx - 30, cy - 46), (cx - 52, cy + 10),
                                       (cx - 60, cy + 10)], (255, 255, 255, 18)))

    def eyes(self, c, cx, cy, led, *, kind="rect", open_=1.0, look=(0, 0), size=1.0):
        col = led + (255,)
        for side in (-1, 1):
            ex, ey = cx + side * 30 + look[0] * 8, cy - 12 + look[1] * 8
            if kind == "happy":
                c.arc(ex, ey + 8, 14, 12, 200, 340, col, 6)
            elif kind == "line":
                c.line([(ex - 13, ey + 2), (ex + 13, ey + 2)], col, 5)
            elif kind == "x":
                for s in (-1, 1):
                    c.line([(ex - 10, ey - 10 * s), (ex + 10, ey + 10 * s)], col, 5)
            elif kind == "round":
                c.circle(ex, ey, 14 * size, col)
                c.circle(ex + 4, ey - 4, 4 * size, (255, 255, 255, 200))
            else:
                h = max(2.5, 16 * open_ * size)
                c.rrect(ex - 11 * size, ey - h, ex + 11 * size, ey + h, min(8, h), fill=col)
            # soft glow under each eye
        c.overlay(lambda o: [o.ellipse(cx + s * 30 + look[0] * 8, cy - 12 + look[1] * 8, 24, 22, fill=led + (30,))
                             for s in (-1, 1)])

    def smile(self, c, cx, cy, led, kind="smile", amount=0.0, i=0):
        col = led + (255,)
        if kind == "smile":
            c.arc(cx, cy + 6, 22, 14, 20, 160, col, 4.5)
        elif kind == "eq":
            for k in range(7):
                h = 3 + 15 * amount * (0.4 + 0.6 * abs(math.sin(i * 0.9 + k * 1.3)))
                x = cx - 27 + k * 9
                c.rrect(x - 3, cy + 22 - h, x + 3, cy + 22 + h, 3, fill=col)
        elif kind == "dots":
            for k in range(3):
                on = (i // 6) % 4 > k
                c.circle(cx - 16 + k * 16, cy + 24, 4, col if on else led + (60,))
        elif kind == "flat":
            c.line([(cx - 18, cy + 24), (cx + 18, cy + 24)], col, 4)
        elif kind == "zig":
            c.line([(cx - 22 + k * 11, cy + 24 + (5 if k % 2 else -5)) for k in range(5)], col, 4)
        elif kind == "mute":  # a crossed-out microphone on the mouth display
            c.rrect(cx - 6, cy + 10, cx + 6, cy + 28, 6, fill=col)
            c.arc(cx, cy + 22, 11, 10, 20, 160, col, 3)
            c.line([(cx - 14, cy + 8), (cx + 14, cy + 36)], (255, 80, 80, 255), 3.5)

    def idle(self, c, i, n):
        led = self.LED["idle"]
        cy = 134 + 3 * osc(i, n)
        self.head(c, 120, cy, led, 0.5 + 0.5 * osc(i, n, 2), antenna_tilt=0.3 * osc(i, n))
        look = (-1 * window(i, 20, 30) + 1 * window(i, 46, 54), 0)
        if 62 <= i < 70:
            self.eyes(c, 120, cy, led, kind="happy")
        else:
            self.eyes(c, 120, cy, led, open_=blink(i, 10, 40), look=look)
        self.smile(c, 120, cy, led)

    def listening(self, c, i, n):
        led = self.LED["listening"]
        cy = 132
        sound_waves(c, i, n, 120, cy, 92, led + (255,))
        self.head(c, 120, cy, led, 0.5 + 0.5 * osc(i, n, 4))
        self.eyes(c, 120, cy, led, kind="round", size=1 + 0.08 * osc(i, n, 2))
        self.smile(c, 120, cy, led, "flat")

    def thinking(self, c, i, n):
        led = self.LED["thinking"]
        cy = 134
        self.head(c, 120, cy, led, 1.0 if (i // 4) % 2 else 0.2, antenna_tilt=osc(i, n, 2))
        self.eyes(c, 120, cy, led, look=(0.8, -0.8), open_=0.7)
        self.smile(c, 120, cy, led, "dots", i=i)

    def speaking(self, c, i, n):
        led = self.LED["speaking"]
        m = speech_pattern(i)
        cy = 134 - 2 * m
        self.head(c, 120, cy, led, m)
        self.eyes(c, 120, cy, led, kind="happy" if 16 <= i < 22 else "rect")
        self.smile(c, 120, cy, led, "eq", amount=m, i=i)

    def sleeping(self, c, i, n):
        led = self.LED["sleeping"]
        cy = 138 + 2 * osc(i, n)
        self.head(c, 120, cy, led, 0.2 + 0.2 * osc(i, n))
        self.eyes(c, 120, cy, led, kind="line")
        self.smile(c, 120, cy, led, "flat")
        zzz(c, i, n, 160, 60, led + (255,))

    def error(self, c, i, n):
        led = self.LED["error"]
        glitch = 6 * math.sin(i * 3.1) if i % 7 < 2 else 0
        cy = 134
        self.head(c, 120 + glitch, cy, led, 1.0 if i % 4 < 2 else 0.0, antenna_tilt=-0.8)
        self.eyes(c, 120 + glitch, cy, led, kind="x")
        self.smile(c, 120 + glitch, cy, led, "zig")
        for k in range(3):
            sparkle(c, 60 + k * 50, 46 + 10 * k, 8 * pulse((i + k * 5) % 18, 0, 6), (255, 180, 80, 255))

    def muted(self, c, i, n):
        led = self.LED["muted"]
        cy = 136 + 2 * osc(i, n)
        self.head(c, 120, cy, led, 0.3 + 0.3 * osc(i, n, 2), antenna_tilt=-0.4)
        side = window(i, 20, 30) - window(i, 34, 42)  # looks one way, then the other
        self.eyes(c, 120, cy, led, open_=blink(i, 8) * 0.75, look=(side, 0.2))
        self.smile(c, 120, cy, led, "mute")


CHARACTERS: list[Character] = [Kotodama(), Obake(), Neko(), Robo()]


# install

def render(character: Character, store: CharacterStore) -> None:
    for state, (n, ms) in TIMING.items():
        store.save_state(character.id, state, [character.frame(state, i, n) for i in range(n)], [ms] * n)


def build(root: Path) -> None:
    """Draw every built-in character into a fresh character directory."""
    store = CharacterStore(root, root / ".cache")
    for ch in CHARACTERS:
        if store.get(ch.id):
            store.delete(ch.id)
        store.create(ch.id, ch.name, order=ch.order)
        render(ch, store)
        store.update_meta(ch.id, builtin=BUILTIN_VERSION)
    shutil.rmtree(root / ".cache", ignore_errors=True)


def ensure_builtin_characters(store: CharacterStore, prebuilt: Path | None = None) -> None:
    """Install or upgrade the built-in characters, unless they were customized in the admin UI."""
    for ch in CHARACTERS:
        meta = store.get(ch.id)
        if meta and (meta.get("customized") or meta.get("builtin") == BUILTIN_VERSION):
            continue
        old_version = meta["version"] if meta else 0
        source = prebuilt / ch.id if prebuilt else None
        meta_file = source / "character.json" if source else None
        # read-only: the prebuilt set lives in the image and is not writable at runtime
        src_meta = json.loads(meta_file.read_text()) if meta_file and meta_file.is_file() else None
        target = store.root / ch.id
        if src_meta and src_meta.get("builtin") == BUILTIN_VERSION:
            log.info("installing built-in character %s from %s", ch.id, prebuilt)
            shutil.rmtree(target, ignore_errors=True)
            shutil.copytree(source, target)
        else:
            log.info("drawing built-in character %s", ch.id)
            if meta:
                store.delete(ch.id)
            store.create(ch.id, ch.name, order=ch.order)
            render(ch, store)
        # keep version numbers increasing so speakers and caches never reuse an old URL
        new_version = max(old_version, store.get(ch.id)["version"]) + 1
        shutil.rmtree(store.cache / ch.id, ignore_errors=True)
        store.update_meta(ch.id, builtin=BUILTIN_VERSION, name=ch.name, order=ch.order, version=new_version)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    build(Path(sys.argv[1]))
