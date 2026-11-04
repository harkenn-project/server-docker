"""Keeping the speakers' SD cards in step with the media library (docs/MEDIA.md, "Sending media to speakers").

Each speaker has a wanted list: items from the library, like "music/Background" or "soundscape/City Rain". From it
Harkenn makes a plan: every file those items need (an album's tracks and its cover), with sizes and checksums. The
speaker compares the plan with the list it keeps on the card of what Harkenn put there (/.harkenn/manifest.json):
it deletes Harkenn's files that aren't wanted any more, fetches what's missing or changed, and leaves everything
else alone. So files someone copied onto the card themselves are never touched.

The speaker fetches a file a piece at a time (`card_get`), only while it's idle, and checks each file's checksum.
A plan goes out when the wanted list changes, when the library changes, and when the speaker connects.

    server -> speaker   card_plan  {plan, files: [{path, size, sha256}]}
    speaker -> server   card_get   {path, offset, length}
    server -> speaker   card_chunk {path, offset, bytes}, then the bytes in binary frames
    speaker -> server   card_sync  {plan, state, done_bytes, total_bytes, file, error}
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from typing import TYPE_CHECKING, Any

from .library import COVER, FOLDERS, title_of

if TYPE_CHECKING:
    from .context import Context

log = logging.getLogger("harkenn.cardsync")

WANTED = "card_wanted:"  # settings table key prefix, per speaker
KINDS = ("music", "soundscape", "meditation")
MAX_GET = 64 * 1024      # the most a speaker may ask for at once
FRAME = 4096             # binary frames, as screens use
STATES = ("idle", "syncing", "paused", "done", "error")


def item_key(kind: str, name: str) -> str:
    return f"{kind}/{name}"


def parse_item(item: str) -> tuple[str, str] | None:
    kind, _, name = str(item).partition("/")
    return (kind, name) if kind in KINDS and name else None


class CardSync:
    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx
        self.progress: dict[str, dict[str, Any]] = {}  # speaker -> its last card_sync report
        self.plans: dict[str, dict[str, Any]] = {}     # speaker -> the plan last sent: {"plan", "paths": {path: size}}
        self._push_task: asyncio.Task | None = None

    # wanted lists

    def wanted(self, device_id: str) -> list[str]:
        return [i for i in self.ctx.db.get_setting(WANTED + device_id) or [] if parse_item(i)]

    def set_wanted(self, device_id: str, items: list[str]) -> list[str]:
        items = sorted({i for i in items if parse_item(i)})
        self.ctx.db.set_setting(WANTED + device_id, items)
        return items

    def holders(self, item: str) -> list[str]:
        return [d["id"] for d in self.ctx.db.list_devices() if item in self.wanted(d["id"])]

    def set_holders(self, item: str, device_ids: list[str]) -> None:
        """Puts `item` on exactly these speakers' cards (adds it to their wanted lists, takes it off the others)."""
        if not parse_item(item):
            raise ValueError("unknown item")
        chosen = set(device_ids)
        for d in self.ctx.db.list_devices():
            items = set(self.wanted(d["id"]))
            new = items | {item} if d["id"] in chosen else items - {item}
            if new != items:
                self.set_wanted(d["id"], list(new))

    # plans

    def items(self) -> dict[str, dict[str, Any]]:
        """Everything in the library that can go on a card: key -> {kind, name, files: [rel paths], bytes}."""
        lib = self.ctx.library
        tree = lib.tree()
        out: dict[str, dict[str, Any]] = {}
        for a in tree["music"]:
            files = [t["path"] for t in a["tracks"]] + ([f"{FOLDERS['music']}/{a['name']}/{COVER}"] if a["cover"] else [])
            out[item_key("music", a["name"])] = {"kind": "music", "name": a["name"], "files": files,
                                                "bytes": a["size"] + (lib.resolve(files[-1]).stat().st_size
                                                                      if a["cover"] else 0)}
        for kind, key in (("soundscape", "soundscapes"), ("meditation", "meditations")):
            for f in tree[key]:
                out[item_key(kind, title_of(f["path"].split("/")[-1]))] = {
                    "kind": kind, "name": f["title"], "files": [f["path"]], "bytes": f["size"]}
        return out

    def plan(self, device_id: str) -> dict[str, Any]:
        everything = self.items()
        files = []
        for item in self.wanted(device_id):
            for rel in (everything.get(item) or {}).get("files", []):
                p = self.ctx.library.resolve(rel)
                files.append({"path": rel, "size": p.stat().st_size, "sha256": self.ctx.library.checksum(p)})
        files.sort(key=lambda f: f["path"])
        digest = hashlib.sha256("\n".join(f"{f['path']}|{f['sha256']}" for f in files).encode()).hexdigest()[:16]
        return {"plan": digest, "files": files, "bytes": sum(f["size"] for f in files)}

    def supports(self, device_id: str) -> bool:
        card = self.ctx.cards.get(device_id) or {}
        return bool(card.get("present") and card.get("sync"))

    async def push(self, device_id: str, only_if_changed: bool = False) -> bool:
        """Sends the speaker its plan, if it's online and can sync. `only_if_changed`: not if its card already
        matches it (the card report says which plan it last finished). Returns True if sent."""
        if not self.supports(device_id) or not self.ctx.hub.online(device_id):
            return False
        plan = await asyncio.to_thread(self.plan, device_id)
        if only_if_changed and (self.ctx.cards.get(device_id) or {}).get("plan") == plan["plan"]:
            self.plans[device_id] = {"plan": plan["plan"], "paths": {f["path"]: f["size"] for f in plan["files"]}}
            return False
        self.plans[device_id] = {"plan": plan["plan"], "paths": {f["path"]: f["size"] for f in plan["files"]}}
        return await self.ctx.hub.send(device_id, {"type": "card_plan", "plan": plan["plan"], "files": plan["files"]})

    def push_soon(self, device_ids: list[str] | None = None, delay: float = 3.0) -> None:
        """A plan to these speakers (or every one with a wanted list) shortly: library edits come in bursts."""
        if self._push_task and not self._push_task.done():
            self._push_task.cancel()

        async def later() -> None:
            await asyncio.sleep(delay)
            for d in device_ids if device_ids is not None else [d["id"] for d in self.ctx.db.list_devices()]:
                if self.wanted(d) or d in self.plans:
                    try:
                        await self.push(d, only_if_changed=True)
                    except Exception as e:  # one speaker's trouble mustn't stop the rest
                        log.warning("card plan for %s failed: %s", d, e)

        try:
            self._push_task = asyncio.get_running_loop().create_task(later())
        except RuntimeError:  # no event loop (tests calling the library directly)
            self._push_task = None

    # the speaker's side of it

    async def handle_get(self, device_id: str, msg: dict[str, Any]) -> None:
        path, offset = str(msg.get("path") or ""), msg.get("offset")
        length = msg.get("length")
        known = (self.plans.get(device_id) or {}).get("paths", {})
        if path not in known or not isinstance(offset, int) or not isinstance(length, int) or offset < 0 \
                or length <= 0:
            await self.ctx.hub.send(device_id, {"type": "card_chunk", "path": path, "offset": offset, "bytes": 0,
                                                "error": "not in the plan"})
            return
        try:
            p = self.ctx.library.resolve(path)
            data = await asyncio.to_thread(_read, p, offset, min(length, MAX_GET))
        except (OSError, ValueError) as e:
            await self.ctx.hub.send(device_id, {"type": "card_chunk", "path": path, "offset": offset, "bytes": 0,
                                                "error": str(e)[:120]})
            return
        async with self.ctx.voice._binary_lock(device_id):  # its frames mustn't mix with a screen's or speech
            if await self.ctx.hub.send(device_id, {"type": "card_chunk", "path": path, "offset": offset,
                                                   "bytes": len(data)}):
                for i in range(0, len(data), FRAME):
                    if not await self.ctx.hub.send_bytes(device_id, data[i:i + FRAME]):
                        break

    def report(self, device_id: str, msg: dict[str, Any]) -> dict[str, Any]:
        state = {"plan": str(msg.get("plan") or "")[:32],
                 "state": msg.get("state") if msg.get("state") in STATES else "idle",
                 "done_bytes": _count(msg.get("done_bytes")), "total_bytes": _count(msg.get("total_bytes")),
                 "file": str(msg.get("file") or "")[:200], "error": str(msg.get("error") or "")[:200],
                 "at": time.time()}
        self.progress[device_id] = state
        return state

    def status(self, device_id: str) -> dict[str, Any]:
        """For the admin: what's wanted, how big it is, and how the sync is going."""
        everything = self.items()
        wanted = self.wanted(device_id)
        items = [{"item": i, "kind": parse_item(i)[0], "name": (everything.get(i) or {}).get("name", parse_item(i)[1]),
                  "bytes": (everything.get(i) or {}).get("bytes", 0), "in_library": i in everything}
                 for i in wanted]
        plan = self.plan(device_id)
        card = self.ctx.cards.get(device_id) or {}
        return {"wanted": items, "bytes": sum(i["bytes"] for i in items), "supported": self.supports(device_id),
                "plan": plan["plan"], "in_step": bool(card.get("present")) and card.get("plan") == plan["plan"],
                "progress": self.progress.get(device_id)}


def _read(path: Any, offset: int, length: int) -> bytes:
    with open(path, "rb") as f:
        f.seek(offset)
        return f.read(length)


def _count(v: Any) -> int:
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else 0
