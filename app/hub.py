"""Live connections: device WebSockets, plus a fan-out of events to admin browsers (SSE)."""
from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from typing import Any

from fastapi import WebSocket

log = logging.getLogger("harkenn.hub")


class Hub:
    def __init__(self) -> None:
        self._conns: dict[str, tuple[WebSocket, asyncio.Lock]] = {}
        self._bases: dict[str, str] = {}  # the address each speaker connected through
        self._via: dict[str, str] = {}  # "direct", or "relay" for a speaker away from home (relay.py)
        self._subscribers: set[asyncio.Queue] = set()
        self._activity: dict[str, str] = {}  # what each speaker is doing: idle, listening, thinking, speaking, ringing

    def online(self, device_id: str) -> bool:
        return device_id in self._conns

    def base(self, device_id: str) -> str | None:
        """The address an online speaker used to reach the server (context.base_url)."""
        return self._bases.get(device_id) if device_id in self._conns else None

    def via(self, device_id: str) -> str | None:
        """How an online speaker is connected: "direct", or "relay" when it's away from home."""
        return self._via.get(device_id) if device_id in self._conns else None

    async def attach(self, device_id: str, ws: Any, base: str = "", via: str = "direct") -> None:
        old = self._conns.get(device_id)
        self._conns[device_id] = (ws, asyncio.Lock())
        self._bases[device_id] = base
        self._via[device_id] = via
        if old:
            with suppress(Exception):
                await old[0].close(code=4000, reason="replaced by a newer connection")

    def detach(self, device_id: str, ws: Any) -> bool:
        """Forget the connection if it is still the current one. Returns True if it was."""
        conn = self._conns.get(device_id)
        if conn and conn[0] is ws:
            del self._conns[device_id]
            return True
        return False

    async def send(self, device_id: str, message: dict[str, Any]) -> bool:
        conn = self._conns.get(device_id)
        if not conn:
            return False
        ws, lock = conn
        try:
            async with lock:
                await ws.send_json(message)
            return True
        except Exception as e:  # connection already gone; the receive loop will clean up
            log.info("send to %s failed: %s", device_id, e)
            return False

    async def send_bytes(self, device_id: str, data: bytes) -> bool:
        conn = self._conns.get(device_id)
        if not conn:
            return False
        ws, lock = conn
        try:
            async with lock:
                await ws.send_bytes(data)
            return True
        except Exception as e:
            log.info("send to %s failed: %s", device_id, e)
            return False

    async def kick(self, device_id: str, code: int = 4401, reason: str = "token revoked") -> None:
        conn = self._conns.pop(device_id, None)
        if conn:
            with suppress(Exception):
                await conn[0].close(code=code, reason=reason)

    def activity(self, device_id: str) -> str:
        return self._activity.get(device_id, "idle")

    def set_activity(self, device_id: str, state: str) -> None:
        """What a speaker is doing, as the server sees it (the speaker's own status only comes every minute)."""
        if self._activity.get(device_id, "idle") != state:
            self._activity[device_id] = state
            self.publish({"type": "activity", "id": device_id, "state": state})

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    def publish(self, event: dict[str, Any]) -> None:
        for q in list(self._subscribers):
            with suppress(asyncio.QueueFull):
                q.put_nowait(event)
