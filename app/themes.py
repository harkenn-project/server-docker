"""Colour schemes and screen layouts for the 240x240 round display."""
from __future__ import annotations

from typing import Any

THEME_KEYS = ("background", "surface", "primary", "accent", "text", "muted")

THEMES: dict[str, dict[str, str]] = {
    "midnight": {"name": "Midnight", "background": "#0b1020", "surface": "#1a2240", "primary": "#7aa2ff",
                 "accent": "#ff7ab6", "text": "#e8ecff", "muted": "#8a93b8"},
    "ocean": {"name": "Ocean", "background": "#04202b", "surface": "#0c3a4a", "primary": "#4fd1c5",
              "accent": "#f6ad55", "text": "#e6fffa", "muted": "#7fb3bf"},
    "sunset": {"name": "Sunset", "background": "#1f0f1a", "surface": "#3b1d2e", "primary": "#ff8c5a",
               "accent": "#ffd166", "text": "#fff1e6", "muted": "#b98a9a"},
    "forest": {"name": "Forest", "background": "#0d1a12", "surface": "#1c3324", "primary": "#7bd389",
               "accent": "#e9c46a", "text": "#eef7ee", "muted": "#8fae96"},
    "paper": {"name": "Paper", "background": "#f4f1ea", "surface": "#e2ddd0", "primary": "#3d5a80",
              "accent": "#e07a5f", "text": "#22223b", "muted": "#7d7a70"},
    "mono": {"name": "Mono", "background": "#000000", "surface": "#222222", "primary": "#ffffff",
             "accent": "#bbbbbb", "text": "#ffffff", "muted": "#888888"},
}

# Widgets are positioned on the 240x240 panel, (0,0) is top-left. Character widgets must be square.
LAYOUTS: dict[str, dict[str, Any]] = {
    "character_full": {
        "name": "Character, full screen",
        "widgets": [
            {"type": "ring", "thickness": 4},
            {"type": "character", "x": 0, "y": 0, "w": 240, "h": 240},
        ],
    },
    "character_clock": {
        "name": "Character with clock",
        "widgets": [
            {"type": "ring", "thickness": 4},
            {"type": "character", "x": 48, "y": 22, "w": 144, "h": 144},
            {"type": "clock", "x": 40, "y": 168, "w": 160, "h": 40, "font": "lg"},
        ],
    },
    "character_status": {
        "name": "Character with status text",
        "widgets": [
            {"type": "ring", "thickness": 4},
            {"type": "character", "x": 56, "y": 18, "w": 128, "h": 128},
            {"type": "status", "x": 30, "y": 150, "w": 180, "h": 56, "font": "md", "lines": 2},
        ],
    },
    "clock_face": {
        "name": "Clock face",
        "widgets": [
            {"type": "ring", "thickness": 3},
            {"type": "clock", "x": 20, "y": 58, "w": 200, "h": 64, "font": "xl"},
            {"type": "date", "x": 40, "y": 124, "w": 160, "h": 24, "font": "sm"},
            {"type": "character", "x": 92, "y": 158, "w": 56, "h": 56},
        ],
    },
}


def character_sizes(layout_id: str) -> list[int]:
    return sorted({w["w"] for w in LAYOUTS[layout_id]["widgets"] if w["type"] == "character"})


ALL_CHARACTER_SIZES = frozenset({240} | {s for lid in LAYOUTS for s in character_sizes(lid)})


def resolve_theme(theme_id: str, overrides: dict[str, str | None]) -> dict[str, str]:
    theme = {k: THEMES[theme_id][k] for k in THEME_KEYS}
    theme.update({k: v for k, v in overrides.items() if v})
    return theme
