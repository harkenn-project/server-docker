"""The weather screen: an animated picture of the weather, with the numbers that go with it, drawn here and shown on a
speaker like any other screen (screens.py). The speaker only plays the finished animation: nothing is drawn there.

The assistant asks for it with a [[weather {...}]] marker in its reply, an agent with the MCP tool show_weather, and
both can leave out whatever they don't know: what's missing comes from the live weather (WeatherService, Open-Meteo,
for the place set on the Server page), and what has no value at all is simply not shown.

To keep what the speaker downloads small, the background and every number stay still and only the weather moves
(sun rays, clouds, rain, snow, fog...), in a short loop; the animation format stores only what changes between
frames.
"""
from __future__ import annotations

import json
import logging
import math
import random
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from PIL import Image, ImageDraw, ImageFont

log = logging.getLogger("harkenn.weather")

SIZE = 240
SS = 2  # drawn at twice the size and scaled down: smooth edges
FRAMES = 16
FRAME_MS = 110
MARK = "weather"

# Every condition the screen knows, with what it's called on screen.
CONDITIONS: dict[str, str] = {
    "clear": "Clear", "mostly_clear": "Mostly clear", "partly_cloudy": "Partly cloudy",
    "mostly_cloudy": "Mostly cloudy", "overcast": "Overcast",
    "fog": "Fog", "freezing_fog": "Freezing fog", "mist": "Mist", "haze": "Haze", "smoke": "Smoke", "smog": "Smog",
    "dust": "Dust", "sand": "Blowing sand",
    "drizzle": "Drizzle", "freezing_drizzle": "Freezing drizzle", "light_rain": "Light rain", "rain": "Rain",
    "heavy_rain": "Heavy rain", "showers": "Showers", "freezing_rain": "Freezing rain", "sleet": "Sleet",
    "ice_pellets": "Ice pellets", "hail": "Hail",
    "light_snow": "Light snow", "snow": "Snow", "heavy_snow": "Heavy snow", "snow_showers": "Snow showers",
    "blowing_snow": "Blowing snow", "blizzard": "Blizzard",
    "thunderstorm": "Thunderstorm", "thunderstorm_hail": "Thunderstorm, hail", "dry_thunderstorm": "Dry thunderstorm",
    "windy": "Windy", "tornado": "Tornado", "hot": "Hot", "cold": "Cold", "frost": "Frost",
}
ALIASES = {"sunny": "clear", "fair": "clear", "cloudy": "mostly_cloudy", "clouds": "mostly_cloudy",
           "partly_sunny": "partly_cloudy", "storm": "thunderstorm", "thunder": "thunderstorm", "ice": "freezing_rain",
           "snowy": "snow", "rainy": "rain", "foggy": "fog", "smoky": "smoke", "breezy": "windy", "wind": "windy"}

# Fields the screen shows (all optional), and how each is written.
FIELDS = {
    "location": "place name", "condition": "one of the conditions", "description": "your own words for it",
    "temp": "temperature now", "feels_like": "feels like", "high": "today's high", "low": "today's low",
    "humidity": "%", "wind": "wind speed", "wind_dir": "degrees or N/NE/...", "gust": "gusts",
    "precip_chance": "% chance of rain/snow", "rain": "rain amount", "snow": "snow amount",
    "uv": "UV index", "aqi": "air quality index", "visibility": "visibility", "pressure": "pressure",
    "is_day": "true/false", "units": "metric or imperial",
    "forecast": "up to 4 of {label, condition, high, low, precip_chance}",
    "view": "\"forecast\" for the coming days here", "seconds": "how long to show it (default 60)",
}


MARK_RE = re.compile(r"\[\[\s*weather\b", re.I)


def split_marker(reply: str) -> tuple[str, dict[str, Any] | None, str]:
    """Take a weather marker out of a reply: (the reply without it, the fields asked for or None, an error or "").
    [[weather]] alone asks for the live weather ({})."""
    m = MARK_RE.search(reply)
    if not m:
        return reply, None, ""
    close = reply.find("]]", m.end())
    brace = reply.find("{", m.end())
    if brace < 0 or (0 <= close < brace):
        return reply[:m.start()] + (reply[close + 2:] if close >= 0 else ""), {}, ""
    try:
        spec, end = json.JSONDecoder().raw_decode(reply, brace)
    except json.JSONDecodeError as e:
        return reply[:m.start()] + (reply[close + 2:] if close >= 0 else ""), {}, f"invalid weather JSON: {e}"
    close = reply.find("]]", end)
    rest = reply[close + 2:] if close >= 0 else reply[end:]
    return reply[:m.start()] + rest, spec if isinstance(spec, dict) else {}, ""


def normalize(condition: Any) -> str:
    c = str(condition or "").strip().lower().replace(" ", "_").replace("-", "_")
    c = ALIASES.get(c, c)
    return c if c in CONDITIONS else ""


# Drawing helpers (in the doubled resolution)

def S(v: float) -> int:
    return int(round(v * SS))


def font(size: int) -> ImageFont.ImageFont | ImageFont.FreeTypeFont:
    try:
        return ImageFont.load_default(size=size * SS)
    except TypeError:
        return ImageFont.load_default()


def text_center(d: ImageDraw.ImageDraw, x: float, y: float, text: str, size: int, fill, shadow=True) -> None:
    f = font(size)
    l, t, r, b = d.textbbox((0, 0), text, font=f)
    px, py = S(x) - (r - l) / 2 - l, S(y) - (b - t) / 2 - t
    if shadow:
        d.text((px + SS, py + SS), text, font=f, fill=(0, 0, 0, 110))
    d.text((px, py), text, font=f, fill=fill)


def text_width(text: str, size: int) -> float:
    f = font(size)
    l, _, r, _ = ImageDraw.Draw(Image.new("L", (1, 1))).textbbox((0, 0), text, font=f)
    return (r - l) / SS


def lerp(a, b, t: float):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(len(a)))


@dataclass
class Look:
    sky_top: tuple
    sky_bottom: tuple
    body: str = "sun"            # sun, moon, none, dim_sun, hot_sun, snowflake, funnel
    clouds: int = 0              # 0-3
    cloud_shade: float = 0.0     # 0 white .. 1 dark grey
    precip: str = ""             # rain, drizzle, snow, sleet, hail, freezing, pellets
    intensity: float = 0.0       # 0..1
    slant: float = 0.15          # how much it blows sideways
    extras: tuple = ()           # fog, haze, smoke, smog, dust, wind, lightning, frost, shimmer


DAY = ((54, 120, 214), (128, 186, 240))
NIGHT = ((12, 18, 46), (38, 52, 96))
GREY_DAY = ((86, 100, 122), (146, 158, 176))
GREY_NIGHT = ((22, 26, 38), (52, 58, 74))
STORM = ((34, 38, 58), (78, 84, 110))


def look_for(condition: str, is_day: bool) -> Look:
    sky = DAY if is_day else NIGHT
    grey = GREY_DAY if is_day else GREY_NIGHT
    body = "sun" if is_day else "moon"
    c = condition or "clear"
    table: dict[str, Look] = {
        "clear": Look(*sky, body=body),
        "mostly_clear": Look(*sky, body=body, clouds=1),
        "partly_cloudy": Look(*sky, body=body, clouds=2),
        "mostly_cloudy": Look(*lerp_sky(sky, grey, .5), body=body, clouds=3, cloud_shade=.25),
        "overcast": Look(*grey, body="none", clouds=3, cloud_shade=.5),
        "fog": Look(*grey, body="none", clouds=0, extras=("fog",)),
        "freezing_fog": Look(*grey, body="none", extras=("fog", "frost")),
        "mist": Look(*lerp_sky(sky, grey, .5), body="dim_sun" if is_day else "moon", extras=("mist",)),
        "haze": Look((150, 140, 110), (210, 190, 140), body="dim_sun" if is_day else "moon", extras=("haze",)),
        "smoke": Look((110, 90, 80), (170, 130, 100), body="dim_sun" if is_day else "none", extras=("smoke",)),
        "smog": Look((120, 112, 80), (176, 160, 110), body="dim_sun" if is_day else "none", extras=("smog",)),
        "dust": Look((150, 112, 70), (204, 160, 104), body="dim_sun" if is_day else "none", extras=("dust",)),
        "sand": Look((168, 128, 72), (220, 180, 112), body="none", extras=("dust", "wind")),
        "drizzle": Look(*grey, body="none", clouds=2, cloud_shade=.4, precip="drizzle", intensity=.4),
        "freezing_drizzle": Look(*grey, body="none", clouds=2, cloud_shade=.4, precip="freezing", intensity=.4,
                                 extras=("frost",)),
        "light_rain": Look(*grey, body="none", clouds=2, cloud_shade=.5, precip="rain", intensity=.35),
        "rain": Look(*grey, body="none", clouds=2, cloud_shade=.6, precip="rain", intensity=.65),
        "heavy_rain": Look(*STORM, body="none", clouds=3, cloud_shade=.75, precip="rain", intensity=1, slant=.25),
        "showers": Look(*sky, body=body, clouds=2, cloud_shade=.45, precip="rain", intensity=.5),
        "freezing_rain": Look(*grey, body="none", clouds=2, cloud_shade=.55, precip="freezing", intensity=.7,
                              extras=("frost",)),
        "sleet": Look(*grey, body="none", clouds=2, cloud_shade=.5, precip="sleet", intensity=.6),
        "ice_pellets": Look(*grey, body="none", clouds=2, cloud_shade=.5, precip="pellets", intensity=.6),
        "hail": Look(*STORM, body="none", clouds=2, cloud_shade=.7, precip="hail", intensity=.7),
        "light_snow": Look(*grey, body="none", clouds=2, cloud_shade=.3, precip="snow", intensity=.35),
        "snow": Look(*grey, body="none", clouds=2, cloud_shade=.35, precip="snow", intensity=.65),
        "heavy_snow": Look(*grey, body="none", clouds=3, cloud_shade=.45, precip="snow", intensity=1),
        "snow_showers": Look(*sky, body=body, clouds=2, cloud_shade=.3, precip="snow", intensity=.5),
        "blowing_snow": Look(*grey, body="none", clouds=1, cloud_shade=.3, precip="snow", intensity=.7, slant=1.1,
                             extras=("wind",)),
        "blizzard": Look((120, 130, 150), (190, 198, 212), body="none", clouds=3, cloud_shade=.4, precip="snow",
                         intensity=1, slant=1.4, extras=("wind",)),
        "thunderstorm": Look(*STORM, body="none", clouds=2, cloud_shade=.85, precip="rain", intensity=.8,
                             extras=("lightning",)),
        "thunderstorm_hail": Look(*STORM, body="none", clouds=2, cloud_shade=.85, precip="hail", intensity=.7,
                                  extras=("lightning",)),
        "dry_thunderstorm": Look(*STORM, body="none", clouds=2, cloud_shade=.8, extras=("lightning",)),
        "windy": Look(*sky, body=body, clouds=1, extras=("wind",)),
        "tornado": Look(*STORM, body="funnel", clouds=2, cloud_shade=.85, extras=("wind",)),
        "hot": Look((214, 96, 40), (250, 176, 84), body="hot_sun" if is_day else "moon", extras=("shimmer",)),
        "cold": Look((70, 120, 190), (170, 210, 240), body="snowflake"),
        "frost": Look(*sky, body=body, extras=("frost",)),
    }
    return table.get(c, table["clear"])


def lerp_sky(a: tuple, b: tuple, t: float) -> tuple:
    return lerp(a[0], b[0], t), lerp(a[1], b[1], t)


# Scene pieces. `p` is the loop's phase, 0..1; everything comes back to where it started at p = 1.

def sky(img: Image.Image, top: tuple, bottom: tuple) -> None:
    d = ImageDraw.Draw(img)
    h = img.height
    for y in range(h):
        d.line([(0, y), (img.width, y)], fill=lerp(top, bottom, y / h) + (255,))


def sun(d: ImageDraw.ImageDraw, cx: float, cy: float, r: float, p: float, hot=False, dim=False) -> None:
    core = (255, 214, 64) if not hot else (255, 150, 40)
    if dim:
        core = (236, 214, 150)
    glow = int(70 + 30 * math.sin(2 * math.pi * p))
    d.ellipse([S(cx - r * 1.55), S(cy - r * 1.55), S(cx + r * 1.55), S(cy + r * 1.55)],
              fill=core + (glow // (2 if dim else 1),))
    if not dim:
        for i in range(12):  # rays turn one step (30 degrees) per loop
            a = math.radians(i * 30 + p * 30)
            r1, r2 = r * 1.2, r * (1.62 if i % 2 else 1.45)
            d.line([S(cx + r1 * math.cos(a)), S(cy + r1 * math.sin(a)), S(cx + r2 * math.cos(a)),
                    S(cy + r2 * math.sin(a))], fill=core + (255,), width=S(3))
    d.ellipse([S(cx - r), S(cy - r), S(cx + r), S(cy + r)], fill=core + (255,))


def moon(d: ImageDraw.ImageDraw, cx: float, cy: float, r: float, p: float, sky_color: tuple) -> None:
    rng = random.Random(7)
    for _ in range(14):  # twinkling stars
        x, y, ph = rng.uniform(20, 220), rng.uniform(22, 120), rng.random()
        if (x - cx) ** 2 + (y - cy) ** 2 < (r * 1.6) ** 2:
            continue
        a = int(120 + 120 * (0.5 + 0.5 * math.sin(2 * math.pi * (p + ph))))
        s = 1.4
        d.ellipse([S(x - s), S(y - s), S(x + s), S(y + s)], fill=(255, 255, 230, a))
    d.ellipse([S(cx - r * 1.4), S(cy - r * 1.4), S(cx + r * 1.4), S(cy + r * 1.4)], fill=(240, 236, 200, 40))
    d.ellipse([S(cx - r), S(cy - r), S(cx + r), S(cy + r)], fill=(246, 240, 206, 255))
    d.ellipse([S(cx - r * .45), S(cy - r * 1.05), S(cx + r * 1.35), S(cy + r * .75)], fill=sky_color + (255,))


def cloud(d: ImageDraw.ImageDraw, cx: float, cy: float, w: float, shade: float) -> None:
    base = lerp((250, 252, 255), (92, 98, 112), shade)
    under = lerp(base, (40, 44, 56), 0.25)
    h = w * 0.42
    parts = [(-.34, .12, .30), (-.08, -.08, .38), (.24, .04, .30), (.40, .18, .2), (-.5, .22, .2)]
    for dx, dy, rr in parts:  # a darker underside first, then the cloud
        x, y, rad = cx + dx * w, cy + dy * w + 2.5, rr * w
        d.ellipse([S(x - rad), S(y - rad), S(x + rad), S(y + rad)], fill=under + (255,))
    d.rounded_rectangle([S(cx - w * .62), S(cy + h * .05), S(cx + w * .6), S(cy + h * .62)], radius=S(h * .3),
                        fill=under + (255,))
    for dx, dy, rr in parts:
        x, y, rad = cx + dx * w, cy + dy * w, rr * w
        d.ellipse([S(x - rad), S(y - rad), S(x + rad), S(y + rad)], fill=base + (255,))
    d.rounded_rectangle([S(cx - w * .62), S(cy), S(cx + w * .6), S(cy + h * .55)], radius=S(h * .28),
                        fill=base + (255,))


def clouds(d: ImageDraw.ImageDraw, n: int, shade: float, p: float, with_body: bool) -> None:
    sway = 4 * math.sin(2 * math.pi * p)
    if n == 1:
        cloud(d, 146 + sway, 84, 62 if with_body else 76, shade)
    elif n == 2:
        cloud(d, 92 - sway * .6, 68, 64, min(1, shade + .1))
        cloud(d, 140 + sway, 86, 84, shade)
    elif n >= 3:
        cloud(d, 82 - sway * .5, 62, 70, min(1, shade + .15))
        cloud(d, 158 + sway * .4, 60, 66, min(1, shade + .1))
        cloud(d, 120 + sway, 88, 96, shade)


def falling(d: ImageDraw.ImageDraw, kind: str, intensity: float, slant: float, p: float, top=106, bottom=126,
            left=46, right=194) -> None:
    """Rain, snow and the rest, falling from under the clouds. Each piece moves a whole number of band heights per
    loop, so the loop is seamless."""
    rng = random.Random(kind)
    count = int(8 + 34 * intensity) if kind not in ("snow",) else int(10 + 30 * intensity)
    h = bottom - top
    for i in range(count):
        x0, y0 = rng.uniform(left, right), rng.uniform(0, h)
        piece = kind
        if kind == "sleet":
            piece = "rain" if i % 2 else "pellet"
        speed = {"rain": 3, "drizzle": 2, "freezing": 3, "snow": 1, "pellet": 2, "pellets": 2, "hail": 3}.get(piece, 2)
        y = (y0 + p * h * speed) % h
        x = x0 + slant * (y - h / 2) * (1.6 if piece == "snow" else 1)
        if piece == "snow":
            x += 3 * math.sin(2 * math.pi * (p + i / count))
        yy = top + y
        if piece in ("rain", "freezing"):
            ln = 9 if kind != "drizzle" else 5
            color = (170, 210, 255, 230) if piece == "rain" else (190, 240, 255, 240)
            d.line([S(x), S(yy), S(x - slant * ln), S(yy + ln)], fill=color, width=S(2))
        elif piece == "drizzle":
            d.line([S(x), S(yy), S(x - slant * 4), S(yy + 4)], fill=(190, 220, 255, 210), width=S(1.6))
        elif piece == "snow":
            r = 2.2 if i % 3 else 3
            d.ellipse([S(x - r), S(yy - r), S(x + r), S(yy + r)], fill=(255, 255, 255, 245))
        elif piece in ("pellet", "pellets"):
            r = 2
            d.ellipse([S(x - r), S(yy - r), S(x + r), S(yy + r)], fill=(225, 240, 255, 255), outline=(150, 190, 230),
                      width=S(1))
        elif piece == "hail":
            r = 3.4 if i % 2 else 2.6
            d.ellipse([S(x - r), S(yy - r), S(x + r), S(yy + r)], fill=(240, 248, 255, 255), outline=(160, 190, 220),
                      width=S(1))


def bands(d: ImageDraw.ImageDraw, p: float, color: tuple, alpha: int, ys: tuple, thick: float) -> None:
    for i, y in enumerate(ys):
        off = 14 * math.sin(2 * math.pi * (p + i * .27)) * (1 if i % 2 else -1)
        d.rounded_rectangle([S(28 + off), S(y), S(212 + off), S(y + thick)], radius=S(thick / 2),
                            fill=color + (alpha,))


def puffs(d: ImageDraw.ImageDraw, p: float, color: tuple, count=7) -> None:
    """Smoke rising from below, growing and fading as it goes; each puff starts again where another began."""
    rng = random.Random(3)
    for i in range(count):
        x0, ph = rng.uniform(60, 180), (p + i / count) % 1
        y = 124 - ph * 80
        r = 8 + ph * 16
        x = x0 + 8 * math.sin(2 * math.pi * (ph + i * .3))
        a = int(170 * (1 - ph) + 20)
        d.ellipse([S(x - r), S(y - r), S(x + r), S(y + r)], fill=color + (a,))


def skyline(d: ImageDraw.ImageDraw, color: tuple) -> None:
    x = 40
    rng = random.Random(11)
    while x < 200:
        w, h = rng.uniform(12, 22), rng.uniform(14, 38)
        d.rectangle([S(x), S(128 - h), S(x + w), S(128)], fill=color + (255,))
        x += w + rng.uniform(1, 4)


def particles(d: ImageDraw.ImageDraw, p: float, color: tuple, count=26) -> None:
    rng = random.Random(5)
    for i in range(count):
        x0, y0 = rng.uniform(0, 170), rng.uniform(46, 126)
        x = 34 + (x0 + p * 170) % 170
        y = y0 + 3 * math.sin(2 * math.pi * (p * 2 + i * .2))
        r = 1.5 if i % 3 else 2.2
        d.ellipse([S(x - r), S(y - r), S(x + r), S(y + r)], fill=color + (220,))


def wind_lines(d: ImageDraw.ImageDraw, p: float, color=(255, 255, 255)) -> None:
    for i, (y, ln) in enumerate([(70, 70), (92, 90), (114, 60)]):
        x = 40 + ((p + i * .33) % 1) * 160 - ln / 2
        pts = [(S(x + t * ln), S(y + 4 * math.sin(t * math.pi * 2 + i))) for t in [k / 10 for k in range(11)]]
        d.line(pts, fill=color + (200,), width=S(2.2), joint="curve")
        ex, ey = pts[-1]
        d.arc([ex - S(8), ey - S(12), ex + S(4), ey], 270, 120, fill=color + (200,), width=S(2.2))


def lightning(d: ImageDraw.ImageDraw, p: float) -> None:
    frame = int(p * FRAMES)
    if frame not in (5, 6, 12):
        return
    bolt = [(126, 92), (112, 116), (124, 116), (110, 140), (138, 108), (124, 108), (134, 92)]
    d.polygon([(S(x), S(y)) for x, y in bolt], fill=(255, 240, 120, 255), outline=(255, 255, 220))


def funnel(d: ImageDraw.ImageDraw, p: float) -> None:
    for i in range(10):
        t = i / 9
        y = 84 + t * 42
        w = 36 * (1 - t) ** 1.4 + 4
        x = 120 + 7 * math.sin(2 * math.pi * (p + t * .6))
        d.ellipse([S(x - w), S(y - 5), S(x + w), S(y + 5)], fill=(84, 88, 104, 235))


def snowflake(d: ImageDraw.ImageDraw, cx: float, cy: float, r: float, p: float) -> None:
    for i in range(6):  # turns one arm's step (60 degrees) per loop
        a = math.radians(i * 60 + p * 60)
        x2, y2 = cx + r * math.cos(a), cy + r * math.sin(a)
        d.line([S(cx), S(cy), S(x2), S(y2)], fill=(235, 248, 255, 255), width=S(3.4))
        for k in (.55,):
            bx, by = cx + r * k * math.cos(a), cy + r * k * math.sin(a)
            for s in (-1, 1):
                b = a + s * math.radians(40)
                d.line([S(bx), S(by), S(bx + r * .3 * math.cos(b)), S(by + r * .3 * math.sin(b))],
                       fill=(235, 248, 255, 255), width=S(2.6))


def frost(d: ImageDraw.ImageDraw) -> None:  # an icy glaze along the bottom of the scene
    d.rounded_rectangle([S(50), S(118), S(190), S(122)], radius=S(2), fill=(200, 236, 255, 230))
    for x in range(58, 186, 11):
        d.polygon([(S(x), S(121)), (S(x + 4), S(121)), (S(x + 2), S(128))], fill=(210, 240, 255, 230))


def shimmer(d: ImageDraw.ImageDraw, p: float) -> None:
    for i, y in enumerate((112, 120, 128)):
        pts = [(S(x), S(y + 2 * math.sin(x / 9 + 2 * math.pi * p + i))) for x in range(60, 182, 4)]
        d.line(pts, fill=(255, 236, 200, 150), width=S(1.6))


def scene(look: Look, p: float) -> Image.Image:
    """The moving part, over a transparent background: the upper part of the screen."""
    img = Image.new("RGBA", (S(SIZE), S(SIZE)), (0, 0, 0, 0))
    d = ImageDraw.Draw(img, "RGBA")
    has_body = look.body in ("sun", "moon", "dim_sun", "hot_sun")
    bx, by = (98, 70) if look.clouds else (120, 80)
    if look.body == "sun":
        sun(d, bx, by, 26, p)
    elif look.body == "hot_sun":
        sun(d, 120, 78, 30, p, hot=True)
    elif look.body == "dim_sun":
        sun(d, 120, 74, 24, p, dim=True)
    elif look.body == "moon":
        moon(d, bx, by, 22, p, look.sky_top)
    elif look.body == "snowflake":
        snowflake(d, 120, 82, 38, p)
    if "haze" in look.extras:
        bands(d, p, (236, 214, 160), 90, (62, 84, 106), 14)
    if "smog" in look.extras:
        skyline(d, (84, 76, 60))
        bands(d, p, (196, 170, 110), 120, (74, 94, 112), 16)
    if "smoke" in look.extras:
        puffs(d, p, (120, 110, 104))
    if "dust" in look.extras:
        bands(d, p, (214, 170, 110), 90, (70, 98), 18)
        particles(d, p, (120, 84, 44))
    if look.body == "funnel":  # under the clouds, so it hangs from them
        funnel(d, p)
    if look.clouds:
        clouds(d, look.clouds, look.cloud_shade, p, has_body)
    if "lightning" in look.extras:
        lightning(d, p)
    if look.precip:
        falling(d, look.precip, look.intensity, look.slant, p)
    if "frost" in look.extras:
        frost(d)
    if "fog" in look.extras:
        bands(d, p, (226, 230, 236), 175, (54, 76, 98, 120), 13)
    if "mist" in look.extras:
        bands(d, p, (236, 240, 246), 110, (86, 106, 124), 10)
    if "wind" in look.extras:
        wind_lines(d, p)
    if "shimmer" in look.extras:
        shimmer(d, p)
    return img


# The numbers

def number(v: Any) -> str:
    if isinstance(v, bool) or v is None or v == "":
        return ""
    if isinstance(v, (int, float)):
        return str(int(round(v))) if abs(v - round(v)) < 0.05 or abs(v) >= 10 else f"{v:.1f}"
    return str(v).strip()[:10]


def deg(v: Any) -> str:  # temperatures in whole degrees
    n = number(round(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else v)
    return n + "°" if n else ""


COMPASS = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")


def compass(v: Any) -> str:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return COMPASS[int((v % 360) / 45 + .5) % 8]
    s = str(v or "").strip().upper()
    return s if s in COMPASS + ("NNE", "ENE", "ESE", "SSE", "SSW", "WSW", "WNW", "NNW") else ""


def stats(w: dict[str, Any]) -> list[str]:
    """The small facts under the temperature, the most useful first; at most three fit."""
    imperial = str(w.get("units", "")).lower().startswith("imp")
    speed, small, big = ("mph", "in", "in") if imperial else ("km/h", "mm", "cm")
    out = []
    if number(w.get("feels_like")) and number(w.get("feels_like")) != number(w.get("temp")):
        out.append(f"Feels {deg(w['feels_like'])}")
    if number(w.get("wind")):
        out.append(f"{compass(w.get('wind_dir'))} {number(w['wind'])} {speed}".strip())
    if number(w.get("snow")):
        out.append(f"Snow {number(w['snow'])} {big}")
    if number(w.get("rain")):
        out.append(f"Rain {number(w['rain'])} {small}")
    if number(w.get("precip_chance")):
        out.append(f"{number(w['precip_chance'])}% chance")
    if number(w.get("gust")):
        out.append(f"Gusts {number(w['gust'])}")
    if number(w.get("humidity")):
        out.append(f"Humidity {number(w['humidity'])}%")
    if number(w.get("aqi")):
        out.append(f"AQI {number(w['aqi'])}")
    if number(w.get("uv")):
        out.append(f"UV {number(w['uv'])}")
    if number(w.get("visibility")):
        out.append(f"Vis {number(w['visibility'])} {'mi' if imperial else 'km'}")
    if number(w.get("pressure")):
        out.append(f"{number(w['pressure'])} {'inHg' if imperial else 'hPa'}")
    return out[:3]


def mini_icon(d: ImageDraw.ImageDraw, cx: float, cy: float, condition: str, is_day=True) -> None:
    """A small still icon, for the forecast row."""
    look = look_for(condition, is_day)
    small = Image.new("RGBA", (S(SIZE), S(SIZE)), (0, 0, 0, 0))
    sd = ImageDraw.Draw(small, "RGBA")
    if look.body in ("sun", "hot_sun", "dim_sun"):
        sun(sd, 120, 110, 22, 0, hot=look.body == "hot_sun", dim=look.body == "dim_sun")
    elif look.body == "moon":
        sd.ellipse([S(98), S(88), S(142), S(132)], fill=(246, 240, 206, 255))
    elif look.body == "snowflake":
        snowflake(sd, 120, 110, 30, 0)
    if look.clouds:
        cloud(sd, 128 if look.body in ("sun", "moon") else 120, 116, 70, look.cloud_shade)
    if look.precip:
        falling(sd, look.precip, .3, .15, .3, top=140, bottom=168, left=94, right=146)
    if "fog" in look.extras or "mist" in look.extras or "haze" in look.extras:
        for y in (110, 124, 138):
            sd.rounded_rectangle([S(86), S(y), S(154), S(y + 8)], radius=S(4), fill=(230, 232, 238, 220))
    if "lightning" in look.extras:
        sd.polygon([(S(x), S(y)) for x, y in [(124, 128), (112, 150), (122, 150), (114, 170), (134, 142), (124, 142),
                                               (130, 128)]], fill=(255, 240, 120, 255))
    icon = small.resize((SIZE * SS // 4, SIZE * SS // 4), Image.LANCZOS)
    d._image.alpha_composite(icon, (S(cx) - icon.width // 2, S(cy) - icon.height // 2 - S(4)))


def still_layer(w: dict[str, Any], condition: str, is_day: bool) -> Image.Image:
    """Everything that doesn't move: the text, at the bottom of the screen."""
    img = Image.new("RGBA", (S(SIZE), S(SIZE)), (0, 0, 0, 0))
    d = ImageDraw.Draw(img, "RGBA")
    white, soft = (255, 255, 255, 255), (230, 236, 250, 235)
    if w.get("location"):
        text_center(d, 120, 22, str(w["location"])[:22], 13, soft)
    forecast = [f for f in (w.get("forecast") or []) if isinstance(f, dict)][:4]
    label = str(w.get("description") or CONDITIONS.get(condition, ""))[:26]
    temp = deg(w.get("temp"))
    hi, lo = deg(w.get("high")), deg(w.get("low"))
    if forecast:  # a compact "now", then the days
        line = "  ".join(x for x in (temp, label) if x)
        text_center(d, 120, 146, line, 18 if len(line) < 18 else 15, white)
        n = len(forecast)
        width = 168 if n > 2 else 120
        for i, f in enumerate(forecast):
            cx = 120 - width / 2 + width * (i + .5) / n
            text_center(d, cx, 168, str(f.get("label", ""))[:5], 11, soft)
            mini_icon(d, cx, 188, normalize(f.get("condition")) or "clear")
            hl = "/".join(x for x in (deg(f.get("high")), deg(f.get("low"))) if x)
            if number(f.get("precip_chance")):
                hl = hl + f" {number(f['precip_chance'])}%" if len(hl) < 6 else hl
            text_center(d, cx, 208, hl, 11, white)
        return img
    if temp:
        text_center(d, 120, 150, temp, 44, white)
    y = 176 if temp else 152
    if label:
        text_center(d, 120, y, label, 15, white)
        y += 18
    if hi or lo:
        text_center(d, 120, y, "   ".join(x for x in (f"H {hi}" if hi else "", f"L {lo}" if lo else "") if x), 13,
                    soft)
        y += 17
    items = stats(w)
    y = min(y + 2, 212)
    room = 2 * math.sqrt(max(0, 112 ** 2 - (y + 6 - 120) ** 2))  # the circle's width there, less a margin
    while items:
        line = "  ·  ".join(items)
        size = 12
        while size > 10 and text_width(line, size) > room:
            size -= 1
        if text_width(line, size) <= room:
            text_center(d, 120, y, line, size, soft)
            break
        items = items[:-1]
    return img


# Putting it together

def is_daytime(w: dict[str, Any]) -> bool:
    v = w.get("is_day")
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    hour = time.localtime().tm_hour
    return 6 <= hour < 20


def render(w: dict[str, Any]) -> tuple[list[Image.Image], list[int], str]:
    """The weather screen's frames (RGBA, 240x240), their durations, and its background colour (hex)."""
    condition = normalize(w.get("condition")) or "clear"
    is_day = is_daytime(w)
    look = look_for(condition, is_day)
    background = Image.new("RGBA", (S(SIZE), S(SIZE)))
    sky(background, look.sky_top, look.sky_bottom)
    still = still_layer(w, condition, is_day)
    frames = []
    for i in range(FRAMES):
        f = background.copy()
        f.alpha_composite(scene(look, i / FRAMES))
        f.alpha_composite(still)
        frames.append(f.resize((SIZE, SIZE), Image.LANCZOS))
    bottom = "%02x%02x%02x" % look.sky_bottom[:3]
    return frames, [FRAME_MS] * FRAMES, bottom


def merge(asked: dict[str, Any], live: dict[str, Any] | None) -> dict[str, Any]:
    """What the assistant gave, filled in from the live weather; a given field always wins. Another place
    ("location" given) takes nothing from here. "view": "forecast" shows the coming days."""
    asked = asked or {}
    elsewhere = asked.get("location") and live and str(asked["location"]).split(",")[0].strip().lower() not in         str(live.get("location", "")).lower()
    out = {} if elsewhere else dict(live or {})
    for k, v in asked.items():
        if v not in (None, ""):
            out[k] = v
    if asked.get("view") == "forecast" and not asked.get("forecast") and out.get("daily"):
        out["forecast"] = out["daily"]
    out.pop("daily", None)
    return out


PROMPT = """

The weather screen: to show the weather on the speaker's screen (an animated picture with the numbers), add the \
marker [[weather]] anywhere in your reply (never spoken). On its own it shows the current weather here. To show \
something else (another place, a forecast, what you were told), give the fields you know, all optional:
[[weather {"location": "Banff", "condition": "light_snow", "temp": -4, "high": -1, "low": -9, "wind": 15, \
"wind_dir": "NW", "snow": 3, "precip_chance": 80}]]
Fields: """ + ", ".join(f"{k} ({v})" for k, v in FIELDS.items()) + """.
Conditions: """ + ", ".join(CONDITIONS) + """.
Numbers are in the units given ("metric": C, km/h, mm, cm; "imperial": F, mph, in). Use it when someone asks about \
the weather; you can still say a short sentence about it.
"""


# The live weather, from Open-Meteo (free, no key): https://open-meteo.com

WMO = {0: "clear", 1: "mostly_clear", 2: "partly_cloudy", 3: "overcast", 45: "fog", 48: "freezing_fog",
       51: "drizzle", 53: "drizzle", 55: "drizzle", 56: "freezing_drizzle", 57: "freezing_drizzle",
       61: "light_rain", 63: "rain", 65: "heavy_rain", 66: "freezing_rain", 67: "freezing_rain",
       71: "light_snow", 73: "snow", 75: "heavy_snow", 77: "ice_pellets", 80: "showers", 81: "showers",
       82: "heavy_rain", 85: "snow_showers", 86: "snow_showers", 95: "thunderstorm", 96: "thunderstorm_hail",
       99: "thunderstorm_hail"}
SETTING = "weather"  # settings table: {"name", "latitude", "longitude", "units"}
CACHE_S = 15 * 60


def _get(url: str) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.loads(r.read(512 * 1024))


def geocode(name: str) -> dict[str, Any] | None:
    q = urllib.parse.urlencode({"name": name, "count": 1, "format": "json"})
    hits = _get(f"https://geocoding-api.open-meteo.com/v1/search?{q}").get("results") or []
    if not hits:
        return None
    h = hits[0]
    label = ", ".join(x for x in (h.get("name"), h.get("admin1")) if x)
    return {"name": label, "latitude": h["latitude"], "longitude": h["longitude"]}


def from_open_meteo(data: dict[str, Any], air: dict[str, Any] | None, name: str, units: str) -> dict[str, Any]:
    c, day = data.get("current", {}), data.get("daily", {})
    condition = WMO.get(int(c.get("weather_code", 0)), "clear")
    w: dict[str, Any] = {
        "location": name, "units": units, "condition": condition, "is_day": bool(c.get("is_day", 1)),
        "temp": c.get("temperature_2m"), "feels_like": c.get("apparent_temperature"),
        "humidity": c.get("relative_humidity_2m"), "wind": c.get("wind_speed_10m"),
        "wind_dir": c.get("wind_direction_10m"), "gust": c.get("wind_gusts_10m"), "pressure": c.get("pressure_msl"),
    }
    if c.get("precipitation"):
        w["snow" if "snow" in condition else "rain"] = c["precipitation"]
    if day.get("temperature_2m_max"):
        w["high"], w["low"] = day["temperature_2m_max"][0], day["temperature_2m_min"][0]
        w["precip_chance"] = (day.get("precipitation_probability_max") or [None])[0]
        w["uv"] = (day.get("uv_index_max") or [None])[0]
        names = ("Today", "Tmrw")
        w["daily"] = [{"label": names[i] if i < 2 else time.strftime("%a", time.strptime(day["time"][i], "%Y-%m-%d")),
                       "condition": WMO.get(int(day["weather_code"][i]), "clear"),
                       "high": day["temperature_2m_max"][i], "low": day["temperature_2m_min"][i],
                       "precip_chance": (day.get("precipitation_probability_max") or [None] * 4)[i]}
                      for i in range(min(4, len(day.get("time", []))))]
    if air:
        aqi = (air.get("current") or {}).get("us_aqi")
        if aqi is not None:
            w["aqi"] = aqi
            pm = (air.get("current") or {}).get("pm2_5") or 0
            if aqi >= 150 and condition in ("clear", "mostly_clear", "partly_cloudy", "mostly_cloudy", "overcast"):
                w["condition"] = "smoke" if pm >= 55 else "haze"  # wildfire smoke and the like
    return {k: v for k, v in w.items() if v is not None}


class WeatherService:
    """The live weather for the place set on the Server page, fetched at most every 15 minutes."""

    def __init__(self, db: Any) -> None:
        self.db = db
        self._cache: tuple[float, dict[str, Any]] | None = None

    def place(self) -> dict[str, Any] | None:
        p = self.db.get_setting(SETTING)
        return p if p and "latitude" in p else None

    def set_place(self, name: str, units: str = "metric") -> dict[str, Any] | None:
        """Look the place up and keep it; None if it isn't found. An empty name turns the live weather off."""
        if not name.strip():
            self.db.set_setting(SETTING, {})
            self._cache = None
            return {}
        found = geocode(name.strip())
        if not found:
            return None
        found["units"] = "imperial" if units == "imperial" else "metric"
        self.db.set_setting(SETTING, found)
        self._cache = None
        return found

    def current(self) -> dict[str, Any] | None:
        """The live weather, or None (no place set, or Open-Meteo unreachable and nothing cached)."""
        place = self.place()
        if not place:
            return None
        if self._cache and time.time() - self._cache[0] < CACHE_S:
            return self._cache[1]
        imperial = place.get("units") == "imperial"
        q = {"latitude": place["latitude"], "longitude": place["longitude"], "timezone": "auto", "forecast_days": 4,
             "current": "temperature_2m,apparent_temperature,relative_humidity_2m,is_day,precipitation,weather_code,"
                        "wind_speed_10m,wind_direction_10m,wind_gusts_10m,pressure_msl",
             "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,uv_index_max"}
        if imperial:
            q |= {"temperature_unit": "fahrenheit", "wind_speed_unit": "mph", "precipitation_unit": "inch"}
        try:
            data = _get(f"https://api.open-meteo.com/v1/forecast?{urllib.parse.urlencode(q)}")
        except Exception as e:
            log.warning("weather: %s", e)
            return self._cache[1] if self._cache else None
        try:
            air = _get("https://air-quality-api.open-meteo.com/v1/air-quality?" + urllib.parse.urlencode(
                {"latitude": place["latitude"], "longitude": place["longitude"], "current": "us_aqi,pm2_5"}))
        except Exception:
            air = None
        w = from_open_meteo(data, air, place.get("name", ""), place.get("units", "metric"))
        self._cache = (time.time(), w)
        return w

    def summary(self) -> str:
        """One line for the assistant's context."""
        w = self.current()
        if not w:
            return ""
        u = "F" if w.get("units") == "imperial" else "C"
        speed = "mph" if w.get("units") == "imperial" else "km/h"
        parts = [CONDITIONS.get(w.get("condition", ""), "").lower(), f"{number(w.get('temp'))}°{u}"]
        if w.get("feels_like") is not None:
            parts.append(f"feels like {number(w['feels_like'])}°{u}")
        if w.get("wind") is not None:
            parts.append(f"wind {number(w['wind'])} {speed} {compass(w.get('wind_dir'))}".rstrip())
        if w.get("high") is not None:
            parts.append(f"today {number(w['low'])} to {number(w['high'])}°{u}")
        if w.get("precip_chance"):
            parts.append(f"{number(w['precip_chance'])}% chance of precipitation")
        if w.get("aqi") is not None:
            parts.append(f"air quality index {number(w['aqi'])}")
        days = [f"{d['label']}: {CONDITIONS.get(d['condition'], '').lower()}, {number(d['low'])} to {number(d['high'])}°{u}"
                for d in w.get("daily", [])[1:]]
        line = f"\nThe weather in {w.get('location', 'here')} now: " + ", ".join(p for p in parts if p) + "."
        return line + (" Coming days: " + "; ".join(days) + "." if days else "")
