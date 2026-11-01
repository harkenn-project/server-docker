"""Screens: the assistant can show something on a speaker's screen along with its reply.

The assistant adds a marker to its reply (see PROMPT):

    [[screen {"image": "...", "overlays": [...], "dismiss": "tap", "seconds": 60}]]

Harkenn draws the whole screen here (picture, SVG, buttons, text, custom overlays) into one KANI image (kani.py, the
format the speakers already play for characters; an animated GIF stays animated) and sends the speaker a `screen`
message with where to download it and the tappable areas. The speaker only shows the picture and reports taps:
tapping an area sends its label back, which goes to the assistant as the person's next turn; tapping anywhere else
dismisses the screen (unless "dismiss" is "none").
"""
from __future__ import annotations

import base64
import io
import json
import logging
import re
import secrets
import textwrap
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Any
from xml.sax.saxutils import escape

from PIL import Image, ImageDraw, ImageFont, ImageSequence

from . import kani

log = logging.getLogger("harkenn.screens")

SIZE = 240                     # the speakers' round screen, in pixels
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_PIXELS = 25_000_000        # pictures bigger than this aren't decoded
MAX_FRAMES = 48
MAX_KANI_BYTES = 2 * 1024 * 1024  # a longer animation is shown as its first frame
MAX_OVERLAYS = 24
DEFAULT_SECONDS, MAX_SECONDS = 60, 600
KEEP_S = MAX_SECONDS + 120     # how long a drawn screen stays downloadable
FETCH_TIMEOUT_S = 10
FONT_SIZES = {"sm": 14, "md": 18, "lg": 28, "xl": 44}
THEME_COLORS = ("background", "surface", "primary", "accent", "text", "muted")
BUTTON_STYLES = ("primary", "secondary", "danger")
TAPPABLE = {"button", "area"}  # always; other overlays when they have a label

MARK_RE = re.compile(r"\[\[\s*screen\b", re.I)
PARAM_RE = re.compile(r"\{\{\s*([A-Za-z0-9_]+)\s*\}\}")
OVERLAYS_KEY = "screen_overlays"  # settings table: the custom overlays
BUILTIN_TYPES = {"button", "text", "area", "svg", "image", "custom"}

PROMPT = """

The screen: the speaker has a small round color touch screen, 240x240 pixels (x right, y down, the centre is \
120,120; the corners are cut off, so keep things inside the circle). You may show something on it by adding one \
marker anywhere in your reply (it is never spoken):
[[screen {"image": ..., "overlays": [...], "dismiss": "tap", "seconds": 60}]]
All fields are optional.
- "image": a picture URL (https://..., PNG, JPEG, WebP or GIF, which stays animated), or an SVG document \
("<svg viewBox='0 0 240 240' ...>...</svg>") you write yourself for diagrams, charts, icons and the like. "fit": \
"cover" (default, fills the screen) or "contain".
- "overlays", drawn on top in order. Each has "type" and optional "x", "y", "w", "h" (pixels):
  {"type": "button", "label": "Yes", "style": "primary"|"secondary"|"danger"}: without x/y, buttons are laid out \
for you near the bottom (up to 4).
  {"type": "text", "text": "...", "size": "sm"|"md"|"lg"|"xl", "color": "text", "background": "surface"}
  {"type": "area", "label": "Kitchen"}: an invisible tappable area over part of the image.
  {"type": "svg", "svg": "<svg ...>"} and {"type": "image", "url": "https://..."}: your own pieces, placed in the box.
{custom}Colors are "#rrggbb" or the speaker's theme colors: background, surface, primary, accent, text, muted.
- Buttons and areas are tappable; so is any overlay given a "label". A tap comes back to you as the person's next \
message, like: (tapped "Yes" on the screen). Tapping elsewhere just closes the screen ("dismiss": "none" keeps it \
until a labelled tap or the time runs out). "seconds": how long it stays up (default 60, at most 600).
Use the screen when seeing helps: a picture, a short list, a timer, a chart, or buttons for choices. You still \
answer out loud as usual; don't read out everything that is on the screen."""


class ScreenError(ValueError):
    pass


@dataclass
class Screen:
    id: str
    kani: bytes
    regions: list[dict[str, Any]]
    dismiss: str
    seconds: int
    created: float = field(default_factory=time.time)

    def message(self, base: str) -> dict[str, Any]:
        """The `screen` message for the speaker."""
        return {"type": "screen", "screen": self.id, "url": f"{base}/api/v1/screens/{self.id}.kani",
                "regions": self.regions, "dismiss": self.dismiss, "seconds": self.seconds}


def split_marker(reply: str) -> tuple[str, dict[str, Any] | None, str]:
    """Take the screen marker out of a reply: (the reply without it, the screen spec or None, an error or "")."""
    m = MARK_RE.search(reply)
    if not m:
        return reply, None, ""
    brace = reply.find("{", m.end())
    end_mark = reply.find("]]", m.end())
    if brace < 0 or (0 <= end_mark < brace):
        return reply[:m.start()] + reply[end_mark + 2 if end_mark >= 0 else len(reply):], None, "no screen spec"
    try:
        spec, end = json.JSONDecoder().raw_decode(reply, brace)
    except json.JSONDecodeError as e:
        close = reply.find("]]", brace)
        return reply[:m.start()] + (reply[close + 2:] if close >= 0 else ""), None, f"invalid screen JSON: {e}"
    close = reply.find("]]", end)
    rest = reply[close + 2:] if close >= 0 else reply[end:]
    if not isinstance(spec, dict):
        return reply[:m.start()] + rest, None, "the screen spec must be an object"
    return reply[:m.start()] + rest, spec, ""


# Colors and fonts

def hex_rgb(value: str) -> tuple[int, int, int]:
    v = value.lstrip("#")
    return int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16)


def color(value: Any, theme: dict[str, str], default: str) -> tuple[int, int, int, int]:
    v = str(value or default).strip()
    if v in theme:
        v = theme[v]
    if not re.fullmatch(r"#?[0-9a-fA-F]{6}", v):
        v = theme.get(default, "#ffffff")
    return (*hex_rgb(v), 255)


def font(size: int) -> ImageFont.ImageFont | ImageFont.FreeTypeFont:
    try:
        return ImageFont.load_default(size=size)  # Pillow's own scalable font: no system fonts needed
    except TypeError:  # very old Pillow
        return ImageFont.load_default()


# Pictures

def fetch(url: str) -> bytes:
    if url.startswith("data:"):
        try:
            return base64.b64decode(url.split(",", 1)[1], validate=False)
        except (IndexError, ValueError) as e:
            raise ScreenError(f"bad data URL: {e}") from e
    if not re.match(r"^https?://", url, re.I):
        raise ScreenError("pictures must be http(s) or data: URLs")
    req = urllib.request.Request(url, headers={"User-Agent": "Harkenn/1 (smart speaker screen)"})
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_S) as resp:
        data = resp.read(MAX_IMAGE_BYTES + 1)
    if len(data) > MAX_IMAGE_BYTES:
        raise ScreenError("picture is too large")
    return data


def is_svg(value: str) -> bool:
    head = value.lstrip()[:200].lower()
    return head.startswith("<svg") or (head.startswith("<?xml") and "<svg" in value[:2000].lower())


def safe_svg(svg: str) -> str:
    """No external references: an SVG may only use inline (data:) pictures and its own #ids."""
    return re.sub(r"""(?i)\b((?:xlink:)?href)\s*=\s*(["'])(?!data:|#)[^"']*\2""", r'\1=""', svg)


def render_svg(svg: str, w: int, h: int) -> Image.Image:
    try:
        import cairosvg
    except (ImportError, OSError) as e:  # OSError: the cairo library itself is missing
        raise ScreenError("SVG isn't available on this server") from e
    if len(svg) > 200_000:
        raise ScreenError("SVG is too large")
    try:
        png = cairosvg.svg2png(bytestring=safe_svg(svg).encode(), output_width=w, output_height=h)
    except Exception as e:
        raise ScreenError(f"couldn't draw the SVG: {e}") from e
    return Image.open(io.BytesIO(png)).convert("RGBA")


def decode_picture(data: bytes) -> tuple[list[Image.Image], list[int]]:
    try:
        im = Image.open(io.BytesIO(data))
    except Exception as e:
        raise ScreenError(f"not a picture: {e}") from e
    if im.width * im.height > MAX_PIXELS:
        raise ScreenError("picture is too large")
    frames, durations = [], []
    for frame in ImageSequence.Iterator(im):
        frames.append(frame.convert("RGBA"))
        durations.append(max(40, int(frame.info.get("duration") or 100)))
        if len(frames) >= MAX_FRAMES:
            break
    return frames, durations


def fit(im: Image.Image, w: int, h: int, mode: str, background: tuple[int, int, int, int]) -> Image.Image:
    if mode == "contain":
        scale = min(w / im.width, h / im.height)
    else:
        scale = max(w / im.width, h / im.height)
    resized = im.resize((max(1, round(im.width * scale)), max(1, round(im.height * scale))), Image.LANCZOS)
    if mode == "contain":
        out = Image.new("RGBA", (w, h), background)
        out.alpha_composite(resized, ((w - resized.width) // 2, (h - resized.height) // 2))
        return out
    left, top = (resized.width - w) // 2, (resized.height - h) // 2
    return resized.crop((left, top, left + w, top + h))


def picture(source: str, w: int, h: int, mode: str, background: tuple[int, int, int, int]
            ) -> tuple[list[Image.Image], list[int]]:
    source = source.strip()
    if is_svg(source):
        return [fit(render_svg(source, w, h), w, h, mode, background)], [1000]
    frames, durations = decode_picture(fetch(source))
    return [fit(f, w, h, mode, background) for f in frames], durations


# Overlays

def box(o: dict[str, Any], default: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    def num(key: str, d: int) -> int:
        try:
            return int(float(o.get(key, d)))
        except (TypeError, ValueError):
            return d
    x, y = num("x", default[0]), num("y", default[1])
    w, h = max(1, min(SIZE, num("w", default[2]))), max(1, min(SIZE, num("h", default[3])))
    return x, y, w, h


def auto_buttons(count: int) -> list[tuple[int, int, int, int]]:
    """Places for buttons without coordinates: rows of up to two near the bottom, inside the circle."""
    count = min(count, 4)
    rows = [116, 160] if count > 2 else [160]  # filled top to bottom, in reading order
    out: list[tuple[int, int, int, int]] = []
    for i in range(count):
        row, col = divmod(i, 2)
        in_row = min(2, count - row * 2)
        y, h = rows[row], 36
        w = 168 if in_row == 1 else 84
        x = (SIZE - w) // 2 if in_row == 1 else 34 + col * (w + 4)
        out.append((x, y, w, h))
    return out


def wrap(text: str, f, width: int) -> list[str]:
    avg = max(1, f.getlength("abcdefghijklmnopqrstuvwxyz") / 26)
    lines: list[str] = []
    for para in text.split("\n"):
        lines += textwrap.wrap(para, max(4, int(width / avg))) or [""]
    return lines


def draw_text(layer: Image.Image, text: str, b: tuple[int, int, int, int], size: int, fg, bg, align: str) -> None:
    d = ImageDraw.Draw(layer)
    x, y, w, h = b
    f = font(size)
    lines = wrap(text, f, w - 8)
    line_h = int(size * 1.25)
    lines = lines[:max(1, h // line_h)]
    if bg is not None:
        d.rounded_rectangle((x, y, x + w - 1, y + h - 1), radius=min(12, h // 2), fill=bg)
    top = y + (h - line_h * len(lines)) // 2
    for i, line in enumerate(lines):
        lw = f.getlength(line)
        lx = x + 4 if align == "left" else x + w - 4 - lw if align == "right" else x + (w - lw) / 2
        d.text((lx, top + i * line_h + line_h / 2), line, font=f, fill=fg, anchor="lm")


def draw_button(layer: Image.Image, label: str, b: tuple[int, int, int, int], style: str,
                theme: dict[str, str]) -> None:
    fill, fg = {"primary": ("primary", "background"), "danger": ("#d9534f", "#ffffff")}.get(
        style, ("surface", "text"))
    x, y, w, h = b
    d = ImageDraw.Draw(layer)
    d.rounded_rectangle((x, y, x + w - 1, y + h - 1), radius=h // 2, fill=color(fill, theme, "surface"),
                        outline=color("primary", theme, "primary") if style == "secondary" else None, width=2)
    draw_text(layer, label, b, 16 if h < 44 else 18, color(fg, theme, "text"), None, "center")


def fill_template(template: str, params: dict[str, Any]) -> str:
    return PARAM_RE.sub(lambda m: escape(str(params.get(m.group(1), ""))), template)


@dataclass
class Drawn:
    layer: Image.Image
    regions: list[dict[str, Any]]
    warnings: list[str]


def draw_overlays(overlays: list[Any], theme: dict[str, str], custom: dict[str, dict[str, Any]]) -> Drawn:
    layer = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    regions: list[dict[str, Any]] = []
    warnings: list[str] = []
    items = [o for o in overlays[:MAX_OVERLAYS] if isinstance(o, dict)]
    unplaced = [o for o in items if o.get("type") == "button" and "x" not in o and "y" not in o]
    places = dict(zip(map(id, unplaced), auto_buttons(len(unplaced))))
    for o in items:
        kind = str(o.get("type", "")).strip().lower()
        label = " ".join(str(o.get("label", "")).split())[:60]
        try:
            if kind == "button":
                if not label:
                    raise ScreenError("a button needs a label")
                if id(o) in places:
                    b = places[id(o)]
                elif "x" in o or "y" in o:
                    b = box(o, (60, 160, 120, 36))
                else:
                    raise ScreenError("too many buttons without a position")
                draw_button(layer, label, b, str(o.get("style", "primary")), theme)
            elif kind == "text":
                b = box(o, (30, 24, 180, 60))
                bg = color(o["background"], theme, "surface") if o.get("background") else None
                draw_text(layer, str(o.get("text", ""))[:500], b, FONT_SIZES.get(str(o.get("size")), 18),
                          color(o.get("color"), theme, "text"), bg, str(o.get("align", "center")))
            elif kind == "area":
                b = box(o, (0, 0, SIZE, SIZE))
                if not label:
                    raise ScreenError("an area needs a label")
            elif kind in ("svg", "image"):
                b = box(o, (0, 0, SIZE, SIZE))
                source = str(o.get("svg") if kind == "svg" else o.get("url") or "")
                if kind == "svg" and not is_svg(source):
                    raise ScreenError("svg overlay without an <svg> document")
                frames, _ = picture(source, b[2], b[3], str(o.get("fit", "contain")), (0, 0, 0, 0))
                layer.alpha_composite(frames[0], (b[0], b[1]))
            elif kind == "custom" or kind in custom:
                name = kind if kind in custom else str(o.get("name", ""))
                c = custom.get(name)
                if not c:
                    raise ScreenError(f"unknown custom overlay '{name}'")
                b = box(o, (c.get("x", 0), c.get("y", 0), c.get("w", SIZE), c.get("h", SIZE)))
                params = o.get("params") if isinstance(o.get("params"), dict) else o
                frames, _ = picture(fill_template(c["svg"], params), b[2], b[3], "contain", (0, 0, 0, 0))
                layer.alpha_composite(frames[0], (b[0], b[1]))
            else:
                raise ScreenError(f"unknown overlay type '{kind}'")
        except ScreenError as e:
            warnings.append(str(e))
            continue
        except Exception as e:  # a picture that couldn't be fetched, ...
            warnings.append(f"{kind}: {e}")
            continue
        if label and (kind in TAPPABLE or "label" in o):
            x, y, w, h = b
            x0, y0, x1, y1 = max(0, x), max(0, y), min(SIZE, x + w), min(SIZE, y + h)
            if x1 > x0 and y1 > y0:
                regions.append({"label": label, "x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0})
    return Drawn(layer, regions, warnings)


def render(spec: dict[str, Any], theme: dict[str, str], custom: dict[str, dict[str, Any]] | None = None
           ) -> tuple[Screen, list[str]]:
    """Draw a screen spec. Problems with single overlays are skipped and returned as warnings; a screen with nothing
    usable raises ScreenError."""
    background = color(spec.get("background"), theme, "background")
    warnings: list[str] = []
    frames, durations = [Image.new("RGBA", (SIZE, SIZE), background)], [1000]
    image = spec.get("image")
    if isinstance(image, str) and image.strip():
        try:
            pics, durations = picture(image, SIZE, SIZE, str(spec.get("fit", "cover")), background)
            frames = []
            for p in pics:
                f = Image.new("RGBA", (SIZE, SIZE), background)
                f.alpha_composite(p)
                frames.append(f)
        except ScreenError as e:
            warnings.append(f"image: {e}")
        except Exception as e:
            warnings.append(f"image: couldn't load it ({e})")
    overlays = spec.get("overlays") if isinstance(spec.get("overlays"), list) else []
    drawn = draw_overlays(overlays, theme, custom or {})
    warnings += drawn.warnings
    if not image and not drawn.regions and not drawn.layer.getbbox():
        raise ScreenError("nothing to show: " + ("; ".join(warnings) or "no image or overlays"))
    for f in frames:
        f.alpha_composite(drawn.layer)
    bg_hex = "%02x%02x%02x" % background[:3]
    data = kani.encode(frames, durations, bg_hex)
    if len(data) > MAX_KANI_BYTES and len(frames) > 1:
        data = kani.encode(frames[:1], [1000], bg_hex)
        warnings.append("animation too large: showing its first frame")
    dismiss = "none" if spec.get("dismiss") == "none" else "tap"
    try:
        seconds = int(spec.get("seconds") or DEFAULT_SECONDS)
    except (TypeError, ValueError):
        seconds = DEFAULT_SECONDS
    seconds = max(5, min(MAX_SECONDS, seconds))
    return Screen(secrets.token_urlsafe(16), data, drawn.regions, dismiss, seconds), warnings


class Screens:
    """Drawn screens, downloadable by the speakers for a while (their ids are unguessable), and the custom overlays
    set up in the admin UI: SVG templates with {{param}} placeholders the assistant fills in."""

    def __init__(self, db: Any) -> None:
        self.db = db
        self._screens: dict[str, Screen] = {}
        self._shown: dict[str, str] = {}  # speaker -> the screen it is showing

    def custom(self) -> dict[str, dict[str, Any]]:
        return dict(self.db.get_setting(OVERLAYS_KEY) or {})

    def save_custom(self, name: str, overlay: dict[str, Any]) -> dict[str, Any]:
        if name in BUILTIN_TYPES:
            raise ScreenError(f"'{name}' is a built-in overlay")
        if not is_svg(overlay["svg"]):
            raise ScreenError("the overlay must be an SVG document (<svg ...>)")
        params = list(dict.fromkeys(PARAM_RE.findall(overlay["svg"])))
        if any(p in ("type", "x", "y", "w", "h", "label") for p in params):
            raise ScreenError("type, x, y, w, h and label can't be parameters")
        overlays = self.custom()
        overlays[name] = {**overlay, "params": params}
        self.db.set_setting(OVERLAYS_KEY, overlays)
        return overlays[name]

    def delete_custom(self, name: str) -> bool:
        overlays = self.custom()
        if overlays.pop(name, None) is None:
            return False
        self.db.set_setting(OVERLAYS_KEY, overlays)
        return True

    def render(self, spec: dict[str, Any], theme: dict[str, str]) -> tuple[Screen, list[str]]:
        return render(spec, theme, self.custom())

    def prompt(self) -> str:
        return prompt(self.custom())

    def add(self, device_id: str, screen: Screen) -> None:
        now = time.time()
        for sid in [s for s, v in self._screens.items() if now - v.created > KEEP_S]:
            del self._screens[sid]
        self._screens[screen.id] = screen
        self._shown[device_id] = screen.id

    def get(self, sid: str) -> Screen | None:
        s = self._screens.get(sid)
        return s if s and time.time() - s.created <= KEEP_S else None

    def tapped(self, device_id: str, sid: str, label: str) -> str | None:
        """The label, if it belongs to the screen the speaker is showing."""
        s = self.get(sid)
        if not s or self._shown.get(device_id) != sid:
            return None
        return next((r["label"] for r in s.regions if r["label"] == label), None)


def custom_prompt(custom: dict[str, dict[str, Any]]) -> str:
    if not custom:
        return ""
    lines = ["  Custom overlays set up on this server (pass their parameters as fields):"]
    for name, c in custom.items():
        params = ", ".join(f'"{p}"' for p in c.get("params", []))
        lines.append(f'  {{"type": "{name}"{", " + params + ": ..." if params else ""}}}: '
                     f'{c.get("description") or "no description"} ({c.get("w", SIZE)}x{c.get("h", SIZE)})')
    return "\n".join(lines) + "\n"


def prompt(custom: dict[str, dict[str, Any]]) -> str:
    return PROMPT.replace("{custom}", custom_prompt(custom))
