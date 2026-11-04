"""Media on the speakers' micro SD cards: what's on each card, what each speaker is playing, and asking for it.

The card is optional. A speaker sends `card` after it connects and whenever the card changes; without one it says
`{"present": false}` and the media features stay hidden for it. The last report is kept, so the admin can show a
speaker's card while it's offline. While it plays, it sends `media_state` (docs/PROTOCOL.md, "The media player").

"Play rain", "pause", "next", "stop in half an hour" are caught before the assistant (parse()), like broadcasts, so
they're quick and work the same with any assistant, Hermes included. Anything it doesn't recognise goes on to the
assistant as usual.
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from .db import Database

if TYPE_CHECKING:
    from .context import Context

SETTING = "card:"  # settings table key prefix, per speaker
MAX_ENTRIES = 64
MAX_NAME = 80
KINDS = ("music", "soundscape", "meditation")
REPEATS = ("off", "all", "one")
ACTIONS = ("play", "pause", "resume", "stop", "next", "previous", "repeat", "sleep")


def _name(value: Any) -> str:
    return str(value or "").strip()[:MAX_NAME]


def _count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def normalize(raw: Any) -> dict[str, Any]:
    """A speaker's report, checked and trimmed: {"present", "total_mb", "free_mb", "music": [{"name", "tracks"}],
    "soundscapes": [names], "meditations": [names], "photos": [{"name", "pictures"}]}."""
    raw = raw if isinstance(raw, dict) else {}
    if not raw.get("present"):
        return {"present": False, "unreadable": True} if raw.get("unreadable") is True else {"present": False}

    def albums(key: str, count: str) -> list[dict[str, Any]]:
        out = [{"name": _name(a.get("name")), count: _count(a.get(count))}
               for a in (raw.get(key) or [])[:MAX_ENTRIES] if isinstance(a, dict)]
        return [a for a in out if a["name"]]

    def names(key: str) -> list[str]:
        return [n for n in (_name(v) for v in (raw.get(key) or [])[:MAX_ENTRIES]) if n]

    managed = [str(p)[:300] for p in (raw.get("managed") or [])[:2000] if isinstance(p, str) and p]
    return {"present": True, "total_mb": _count(raw.get("total_mb")), "free_mb": _count(raw.get("free_mb")),
            "music": albums("music", "tracks"), "soundscapes": names("soundscapes"),
            "meditations": names("meditations"), "photos": albums("photos", "pictures"),
            "sync": raw.get("sync") is True, "managed": managed, "plan": str(raw.get("plan") or "")[:32]}


def own_items(card: dict[str, Any]) -> list[dict[str, str]]:
    """What's on a card that Harkenn didn't put there (copied on by hand): Harkenn never changes these."""
    if not card.get("present"):
        return []
    managed = card.get("managed") or []

    def harkenn(prefix: str) -> bool:
        return any(p.startswith(prefix) for p in managed)

    out = [{"kind": "music", "name": a["name"], "detail": f"{a['tracks']} tracks"} for a in card.get("music", [])
           if not harkenn(f"Music/{a['name']}/")]
    out += [{"kind": "soundscape", "name": n, "detail": ""} for n in card.get("soundscapes", [])
            if not harkenn(f"Soundscapes/{n}.")]
    out += [{"kind": "meditation", "name": n, "detail": ""} for n in card.get("meditations", [])
            if not harkenn(f"Meditations/{n}.")]
    out += [{"kind": "photos", "name": a["name"], "detail": f"{a['pictures']} pictures"} for a in card.get("photos", [])
            if not harkenn(f"Photos/{a['name']}/")]
    return out


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


class Players:
    """What each speaker is playing, as it last said (kept in memory: a speaker sends it again when it connects)."""

    def __init__(self) -> None:
        self.state: dict[str, dict[str, Any]] = {}

    def report(self, device_id: str, raw: Any) -> dict[str, Any]:
        raw = raw if isinstance(raw, dict) else {}
        state: dict[str, Any] = {
            "state": raw.get("state") if raw.get("state") in ("playing", "paused", "stopped") else "stopped",
            "kind": raw.get("kind") if raw.get("kind") in KINDS else "music",
            "album": _name(raw.get("album")), "title": _name(raw.get("title")),
            "track": _count(raw.get("track")), "tracks": _count(raw.get("tracks")),
            "repeat": raw.get("repeat") if raw.get("repeat") in REPEATS else "off",
            "shuffle": bool(raw.get("shuffle")), "error": _name(raw.get("error")),
        }
        if isinstance(raw.get("sleep_s"), int) and not isinstance(raw.get("sleep_s"), bool):
            state["sleep_s"] = max(0, raw["sleep_s"])
        self.state[device_id] = state
        return state

    def get(self, device_id: str) -> dict[str, Any]:
        return self.state.get(device_id) or {"state": "stopped"}

    def active(self, device_id: str) -> bool:
        return self.get(device_id).get("state") in ("playing", "paused")


def command(action: str, **args: Any) -> dict[str, Any]:
    """The `media` message for a speaker, checked. Raises ValueError for anything it can't send."""
    if action not in ACTIONS:
        raise ValueError(f"unknown media action {action!r}")
    msg: dict[str, Any] = {"type": "media", "action": action}
    if action == "play":
        kind = args.get("kind") or "music"
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {', '.join(KINDS)}")
        msg["kind"] = kind
        msg["album"] = _name(args.get("album"))
        if not msg["album"]:
            raise ValueError("say what to play")
        msg["track"] = max(0, int(args.get("track") or 0))
        msg["shuffle"] = bool(args.get("shuffle"))
        if args.get("repeat") in REPEATS:
            msg["repeat"] = args["repeat"]
        if args.get("minutes"):
            msg["minutes"] = max(1, min(int(args["minutes"]), 24 * 60))
    elif action == "repeat":
        if args.get("mode") not in REPEATS:
            raise ValueError(f"repeat must be one of {', '.join(REPEATS)}")
        msg["mode"] = args["mode"]
    elif action == "sleep":
        msg["minutes"] = max(0, min(int(args.get("minutes") or 0), 24 * 60))
    return msg


async def send(ctx: Context, device_id: str, action: str, **args: Any) -> bool:
    """Sends a media command; False if the speaker is offline."""
    return await ctx.hub.send(device_id, command(action, **args))


# Understanding requests

FILLERS = ("hey", "ok", "okay", "please", "can you", "could you", "would you", "will you", "i want to",
           "i'd like to", "let's", "lets", "just", "now")
NUMBERS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
           "nine": 9, "ten": 10, "fifteen": 15, "twenty": 20, "thirty": 30, "forty": 40, "forty-five": 45,
           "forty five": 45, "fifty": 50, "sixty": 60, "ninety": 90}
# What people call the starter albums, beyond their names.
ALIASES = {"music": "Background", "piano": "Background", "piano music": "Background",
           "background music": "Background", "relaxing music": "Background", "soft music": "Background",
           "calm music": "Background", "classical music": "Background", "elevator music": "Background",
           "rain": "City Rain", "the rain": "City Rain", "storm": "Thunderstorm", "a storm": "Thunderstorm",
           "thunder": "Thunderstorm", "a thunderstorm": "Thunderstorm", "fire": "Campfire", "a fire": "Campfire",
           "fireplace": "Campfire", "the fire": "Campfire", "birds": "Birdsong", "a stream": "Babbling Stream",
           "stream": "Babbling Stream", "river": "Babbling Stream", "the river": "Babbling Stream",
           "water": "Babbling Stream", "frogs": "Summer Swamp", "crickets": "Mountain Night",
           "lake": "Lakeside Morning", "chimes": "Blustery Chimes", "wind chimes": "Blustery Chimes",
           "noise": "White Noise"}
NUM = r"(\d+|" + "|".join(sorted((re.escape(k) for k in NUMBERS), key=len, reverse=True)) + r")"
DURATION = r"\s+(?:for|in)\s+(?:(half an hour)|" + NUM + r"\s*(minutes?|mins?|hours?|hrs?))"


def _clean(heard: str) -> str:
    text = re.sub(r"[^\w' -]", " ", heard.lower())
    text = re.sub(r"\s+", " ", text).strip()
    changed = True
    while changed:
        changed = False
        for f in FILLERS:
            if text == f:
                return ""
            if text.startswith(f + " "):
                text, changed = text[len(f) + 1:], True
            if text.endswith(" " + f):
                text, changed = text[:-len(f) - 1], True
    return text


def _minutes(m: re.Match) -> int:
    if m.group(1):
        return 30
    n = m.group(2)
    value = int(n) if n.isdigit() else NUMBERS.get(n, 0)
    return value * (60 if m.group(3).startswith(("hour", "hr")) else 1)


def say_minutes(minutes: int) -> str:
    if minutes == 30:
        return "half an hour"
    if minutes % 60 == 0:
        hours = minutes // 60
        return "an hour" if hours == 1 else f"{hours} hours"
    return f"{minutes} minute" + ("" if minutes == 1 else "s")


def _match(want: str, card: dict[str, Any], prefer: str | None) -> tuple[str, str] | None:
    """(kind, name) on the card for what someone asked for, or None."""
    options = ([("music", a["name"]) for a in card.get("music", [])]
               + [("soundscape", n) for n in card.get("soundscapes", [])]
               + [("meditation", n) for n in card.get("meditations", [])])
    if prefer:
        options.sort(key=lambda o: o[0] != prefer)
    if not options:
        return None
    bare = re.sub(r"^(the|a|an|my|our) ", "", want)
    alias = ALIASES.get(want) or ALIASES.get(bare)
    for target in [want, bare] + ([alias.lower()] if alias else []):
        for kind, name in options:
            if name.lower() == target:
                return kind, name
    if want in ("", "the", "me", "something relaxing") and prefer in ("meditation", "soundscape"):
        # "play some soundscapes", "play a meditation": the first one there is
        names = card.get("meditations" if prefer == "meditation" else "soundscapes") or []
        if names:
            return prefer, names[0]
    words = set(want.split()) - {"the", "a", "an", "some", "of", "and", "me"}
    best, score = None, 0
    for kind, name in options:
        overlap = len(words & set(name.lower().split()))
        if overlap > score:
            best, score = (kind, name), overlap
    if best:
        return best
    if prefer == "music" and card.get("music"):  # "play some music", whatever the albums are called
        return "music", card["music"][0]["name"]
    return None


def _controls(text: str, state: dict[str, Any]) -> dict[str, Any] | None:
    """Pause, stop, next and the rest, for whatever's playing or paused."""
    def full(pattern: str) -> bool:
        return re.fullmatch(pattern, text) is not None

    if full(r"pause|hold on|pause (the )?(music|it|that|this|playback|song|track|sounds?)"):
        return {"action": "pause", "reply": ""}
    if full(r"resume|unpause|continue|play|keep playing|carry on|resume (the )?(music|playing)"):
        return {"action": "resume", "reply": ""}
    if full(r"stop|stop (the )?(music|playing|it|that|this|playback|song|track|sounds?)|turn (it|that|the music|the "
            r"sound) off|turn off the (music|sound)|that's enough|enough"):
        return {"action": "stop", "reply": ""}
    if full(r"next|skip|skip (it|this|that)|next (track|song|one|piece)|skip (this|the) (track|song|one)"):
        return {"action": "next", "reply": ""}
    if full(r"previous|go back|back|previous (track|song|one)|last (track|song|one)|play the (previous|last) "
            r"(track|song|one)"):
        return {"action": "previous", "reply": ""}
    if full(r"repeat (this|this (song|track|one)|the (song|track))|play (this|it) on repeat|loop (this|it)"):
        return {"action": "repeat", "mode": "one", "reply": "I'll repeat this one."}
    if full(r"repeat (all|the album|everything)"):
        return {"action": "repeat", "mode": "all", "reply": "I'll repeat the album."}
    if full(r"stop repeating|repeat off|turn off repeat|don't repeat|no repeat"):
        return {"action": "repeat", "mode": "off", "reply": "Repeat's off."}
    if full(r"shuffle|shuffle (it|this|the album|the music)|mix it up") and state.get("kind") == "music":
        return {"action": "play", "kind": "music", "album": state.get("album", ""), "shuffle": True,
                "reply": "Shuffling."}
    if full(r"(what's|what is) (this|playing|this (song|track|piece|music))|what am i listening to|what song is this"
            r"|what's this called"):
        title = state.get("title")
        return {"action": "", "reply": f"This is {title}." if title else "I'm not sure, sorry."}
    m = re.fullmatch(r"(?:stop|stop playing|stop the music|turn (?:it|the music|this) off)" + DURATION, text) \
        or re.fullmatch(r"(?:set )?(?:a )?sleep timer" + DURATION, text)
    if m and _minutes(m):
        minutes = _minutes(m)
        return {"action": "sleep", "minutes": minutes, "reply": f"OK, I'll stop in {say_minutes(minutes)}."}
    return None


def parse(heard: str, card: dict[str, Any] | None, state: dict[str, Any]) -> dict[str, Any] | None:
    """A media request, as {"action", ...} (what command() takes, plus "reply": what to say back; an empty action
    only answers), or None if `heard` isn't one. Playing needs a card; the controls need something playing or
    paused."""
    text = _clean(heard)
    if not text:
        return None
    if state.get("state") in ("playing", "paused"):
        control = _controls(text, state)
        if control:
            return control
    m = re.fullmatch(r"(?:play|put on|start|play me)\s+(.+)", text)
    if not m:
        return None
    what, minutes = m.group(1), 0
    d = re.search(DURATION + r"$", what)
    if d:
        minutes = _minutes(d)
        what = what[:d.start()].strip()
    prefer = None
    for word, kind in (("meditations?", "meditation"), ("music", "music"), ("album", "music"), ("songs", "music"),
                       ("piano", "music"), ("sounds", "soundscape"), ("sound", "soundscape"),
                       ("soundscapes?", "soundscape"), ("noise", "soundscape")):
        if prefer is None and re.search(rf"\b{word}\b", what):
            prefer = kind
    shuffle = bool(re.search(r"\bshuffled?\b", what))
    what = re.sub(r"\b(sounds?|soundscapes?|meditations?|the album|album|shuffled?|some|a|an|any)\b", "", what)
    what = re.sub(r"\s+", " ", what).strip()
    if not (card and card.get("present")):
        if prefer:  # clearly asking for media: say why not, rather than leave it to the assistant
            return {"action": "", "reply": "This speaker doesn't have a memory card, so it can't play music or "
                                           "sounds."}
        return None
    found = _match(what, card, prefer)
    if not found:
        plural = {"music": "music", "soundscape": "soundscapes", "meditation": "meditations"}.get(prefer or "")
        if plural and not what:  # asked for a kind, and the card has none: say so (send some from Harkenn)
            return {"action": "", "reply": f"This speaker's memory card has no {plural} yet. You can send "
                                           f"some from Harkenn's media pages."}
        return None
    kind, name = found
    what_it_is = f"the {name} meditation" if kind == "meditation" and "meditation" not in name.lower() else name
    reply = ("Starting " if kind == "meditation" else "Playing ") + what_it_is \
        + (f" for {say_minutes(minutes)}." if minutes else ".")
    return {"action": "play", "kind": kind, "album": name, "minutes": minutes, "shuffle": shuffle, "reply": reply}
