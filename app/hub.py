"""Live connections: device WebSockets, plus a fan-out of events to admin browsers (SSE)."""
from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from typing import Any

from fastapi import WebSocket

log = logging.getLogger("kotodama.hub")


class Hub:
    def __init__(self) -> None:
        self._conns: dict[str, tuple[WebSocket, asyncio.Lock]] = {}
        self._subscribers: set[asyncio.Queue] = set()

    def online(self, device_id: str) -> bool:
        return device_id in self._conns

    async def attach(self, device_id: str, ws: WebSocket) -> None:
        old = self._conns.get(device_id)
        self._conns[device_id] = (ws, asyncio.Lock())
        if old:
            with suppress(Exception):
                await old[0].close(code=4000, reason="replaced by a newer connection")

    def detach(self, device_id: str, ws: WebSocket) -> bool:
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
