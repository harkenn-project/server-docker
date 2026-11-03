"""Broadcast: an announcement in your own voice on the other speakers.

"Broadcast that dinner's ready" (or "announce...", "broadcast to the bedrooms...") is caught before the assistant
sees it, so it works the same with any assistant, Hermes included. The other speakers show a card with a chime, say
"Announcement from <who>", then play the recording, from where the message starts: Whisper's word timings find where
"broadcast that" ends. (With a Wyoming speech-to-text service there are no word timings, so they hear all of it.)
"Broadcast" alone asks for the message and sends whatever comes next, whole.

The admin Broadcast page sends one too, typed (in the assistant's voice) or recorded in the browser, and lists the
last month of them. They're kept in household_items, as module "broadcast".
"""
from __future__ import annotations

import logging
import re
import secrets
import time
from typing import TYPE_CHECKING, Any

import numpy as np

from . import groups
from .voice.engines import RATE

if TYPE_CHECKING:
    from .context import Context

log = logging.getLogger("harkenn.broadcast")

MODULE = "broadcast"
KEEP_S = 30 * 86400
MAX_S = 30
LEAD_S = 0.0       # kept before the first word (Whisper starts words a little early already)
TAIL_S = 0.35      # kept after its last word
GAP = bytes(int(RATE * 0.25) * 2)
FILLERS = {"hey", "ok", "okay", "please", "can", "could", "would", "you", "go", "ahead", "and"}
TRIGGERS = (("broadcast",), ("announce",), ("make", "an", "announcement"), ("send", "an", "announcement"))
ALL = ("all speakers", "every speaker", "all the speakers", "everyone", "everybody", "everywhere", "the house",
       "the whole house", "whole house", "house", "all")
LINKS = {"that", "this", "saying", "say"}


def _norm(word: str) -> str:
    return re.sub(r"[^\w']", "", word.lower()).strip("'")


def destinations(db: Any) -> list[dict[str, str]]:
    """Where a broadcast can go: all of them, a group, or one speaker."""
    return ([{"id": "all", "name": "All speakers", "kind": "all"}]
            + [{"id": g["id"], "name": g["name"], "kind": "group"} for g in groups.all_groups(db)]
            + [{"id": d["id"], "name": d["name"], "kind": "speaker"} for d in db.list_devices()])


def parse(heard: str, db: Any) -> dict[str, Any] | None:
    """{"to", "message", "skip"} when `heard` asks for a broadcast; `skip` is how many words come before the message.
    None when it doesn't."""
    words = [w for w in re.findall(r"[\w'-]+", heard)]
    low = [_norm(w) for w in words]
    i = 0
    while i < len(low) and low[i] in FILLERS:
        i += 1
    trigger = next((t for t in TRIGGERS if tuple(low[i:i + len(t)]) == t), None)
    if not trigger:
        return None
    i += len(trigger)
    to = "all"
    if i < len(low) and low[i] in ("to", "on", "in", "for", "through", "throughout"):
        j = i + 1
        names = [(a.split(), "all") for a in ALL]
        for d in destinations(db)[1:]:
            names.append(([_norm(w) for w in d["name"].split()], d["id"]))
        names.sort(key=lambda n: -len(n[0]))
        for start in (j + 1, j) if j < len(low) and low[j] == "the" else (j,):
            hit = next(((n, key) for n, key in names if n and low[start:start + len(n)] == n), None)
            if hit:
                to, i = hit[1], start + len(hit[0])
                if i < len(low) and low[i] in ("speakers", "speaker", "rooms"):
                    i += 1
                break
    if i < len(low) and low[i] in LINKS:
        i += 1
    message = " ".join(words[i:]).strip()
    return {"to": to, "message": message[:1].upper() + message[1:] if message else "", "skip": i}


def message_span(words: list[tuple[str, float, float]], skip: int, message: str) -> tuple[float, float] | None:
    """Where the message is in the recording (seconds), from Whisper's word timings: the first word of the message
    after the request's own words."""
    flat = [(_norm(w), s, e) for w, s, e in words if _norm(w)]
    if not flat:
        return None
    first = _norm(message.split()[0]) if message.split() else ""
    at = skip if skip < len(flat) and flat[skip][0] == first else None
    if at is None and first:  # the two transcriptions split words differently: find the message's first word
        at = next((k for k in range(min(skip, len(flat)) // 2, len(flat)) if flat[k][0] == first), None)
    if at is None:
        at = skip if skip < len(flat) else None
    if at is None:
        return None
    return max(0.0, flat[at][1] - LEAD_S), flat[-1][2] + TAIL_S


def loudness(pcm: bytes) -> bytes:
    """Brought up to a steady level (peaks at about -3 dBFS, at most 6x louder), so quiet speakers are heard."""
    x = np.frombuffer(pcm[: len(pcm) // 2 * 2], dtype="<i2").astype(np.float32)
    if not len(x):
        return pcm
    peak = float(np.percentile(np.abs(x), 99.9)) or 1.0
    gain = min(6.0, 0.7 * 32767 / peak)
    return np.clip(x * gain, -32767, 32767).astype("<i2").tobytes()


def resolve(db: Any, to: str, exclude: str = "") -> list[dict[str, Any]]:
    """The speakers `to` means, less the one it was said on (unless that's all there is). Raises ValueError."""
    want = (to or "all").strip().lower()
    devices = db.list_devices()
    if want in ("all", *ALL):
        found = devices
    else:
        group = next((g for g in groups.all_groups(db) if want in (g["id"], g["name"].lower())), None)
        if group:
            found = [d for d in devices if groups.of_device(db, d)["id"] == group["id"]]
        else:
            found = [d for d in devices if want in (d["id"], d["name"].lower())]
            if not found:
                raise ValueError(f"there's no speaker or group called {to}")
    return [d for d in found if d["id"] != exclude] or found


def to_name(db: Any, to: str) -> str:
    return next((d["name"] for d in destinations(db) if d["id"] == to), to)


async def send(ctx: Context, *, text: str, pcm: bytes | None = None, to: str = "all", sender: str = "",
               exclude: str = "") -> dict[str, Any]:
    """Show and play a broadcast on the speakers it's for (the recording, or `text` in the assistant's voice).
    Returns what's kept for the Broadcast page, with heard_on. Raises ValueError."""
    text = " ".join(str(text or "").split())[:300]
    if not text and not pcm:
        raise ValueError("a broadcast needs something to say")
    targets = resolve(ctx.db, to, exclude)
    intro = f"Announcement from {sender}." if sender else "Announcement."
    if pcm:
        audio = await ctx.voice.synthesize(intro) + GAP + loudness(pcm[: RATE * 2 * MAX_S])
    else:
        audio = await ctx.voice.synthesize(f"{intro} {text}")
    title = f"From {sender}" if sender else "Announcement"
    heard = []
    for d in targets:
        if not ctx.hub.online(d["id"]):
            continue
        ctx.board.notify(d["id"], title, text or "(a voice message)")
        await ctx.hub.send(d["id"], {"type": "notify", "title": title[:60], "text": text or "(a voice message)",
                                     "seconds": 20})
        await ctx.voice.say(d["id"], f"{intro} {text}".strip(), pcm=audio)
        ctx.hub.publish({"type": "device", "id": d["id"]})
        heard.append(d["name"])
    now = time.time()
    item = {"id": secrets.token_hex(5), "module": MODULE, "owner": "", "created_at": now, "updated_at": now,
            "data": {"text": text, "to": to or "all", "from": sender, "voice": bool(pcm), "heard_on": heard,
                     "sent_at": now}}
    ctx.db.put_household_item(item)
    for old in ctx.db.household_items(MODULE):
        if old["created_at"] < now - KEEP_S:
            ctx.db.delete_household_item(old["id"])
    ctx.hub.publish({"type": "broadcast"})
    log.info("broadcast %r from %s on %s", text, sender or "?", ", ".join(heard) or "nothing (all offline)")
    return item["data"]


def history(ctx: Context) -> list[dict[str, Any]]:
    items = sorted(ctx.db.household_items(MODULE), key=lambda i: -i["created_at"])
    return [{"id": i["id"], **i["data"], "to_name": to_name(ctx.db, i["data"]["to"])} for i in items]


def sent_reply(heard_on: list[str]) -> str:
    if not heard_on:
        return "No other speaker is on to hear it."
    if len(heard_on) == 1:
        return f"Sent to the {heard_on[0]}."
    return f"Sent to {len(heard_on)} speakers."
