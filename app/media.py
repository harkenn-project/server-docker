"""What's on each speaker's micro SD card: music albums, soundscapes, meditations and photo albums (docs/MEDIA.md).

The card is optional. A speaker sends `card` after it connects and whenever the card changes; without one it says
`{"present": false}` and the media features stay hidden for it. The last report is kept, so the admin can show a
speaker's card while it's offline.
"""
from __future__ import annotations

from typing import Any

from .db import Database

SETTING = "card:"  # settings table key prefix, per speaker
MAX_ENTRIES = 64
MAX_NAME = 80


def _name(value: Any) -> str:
    return str(value or "").strip()[:MAX_NAME]


def _count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def normalize(raw: Any) -> dict[str, Any]:
    """A speaker's report, checked and trimmed: {"present", "total_mb", "free_mb", "music": [{"name", "tracks"}],
    "soundscapes": [names], "meditations": [names], "photos": [{"name", "pictures"}]}."""
    raw = raw if isinstance(raw, dict) else {}
    if not raw.get("present"):
        return {"present": False}

    def albums(key: str, count: str) -> list[dict[str, Any]]:
        out = [{"name": _name(a.get("name")), count: _count(a.get(count))}
               for a in (raw.get(key) or [])[:MAX_ENTRIES] if isinstance(a, dict)]
        return [a for a in out if a["name"]]

    def names(key: str) -> list[str]:
        return [n for n in (_name(v) for v in (raw.get(key) or [])[:MAX_ENTRIES]) if n]

    return {"present": True, "total_mb": _count(raw.get("total_mb")), "free_mb": _count(raw.get("free_mb")),
            "music": albums("music", "tracks"), "soundscapes": names("soundscapes"),
            "meditations": names("meditations"), "photos": albums("photos", "pictures")}


class Cards:
    def __init__(self, db: Database) -> None:
        self.db = db

    def report(self, device_id: str, raw: Any) -> dict[str, Any]:
        card = normalize(raw)
        self.db.set_setting(SETTING + device_id, card)
        return card

    def get(self, device_id: str) -> dict[str, Any] | None:
        """The last report, or None if the speaker never sent one (firmware before 0.14)."""
        stored = self.db.get_setting(SETTING + device_id)
        return normalize(stored) if stored else None
