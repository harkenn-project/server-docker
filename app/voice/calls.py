"""Ringing speakers, for agents (mcp_server.py) that need to know whether anyone is there.

    server:  ring {ring, seconds}          the speaker rings and shows "Tap to answer"
    speaker: ring_answer {ring}            someone tapped it
    server:  ring_stop {ring}              answered elsewhere, or time's up

A ring goes to one or more speakers at once; the first tap answers it and the others stop. See docs/PROTOCOL.md.
"""
from __future__ import annotations

import asyncio
import secrets
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..hub import Hub

MIN_RING_S = 5
MAX_RING_S = 30


class Rings:
    def __init__(self, hub: Hub) -> None:
        self.hub = hub
        self._waiting: dict[str, asyncio.Future] = {}  # ring id -> answering speaker's id

    async def ring(self, device_ids: list[str], seconds: float) -> str | None:
        """Ring these speakers for up to `seconds`. Returns the speaker that was answered, or None."""
        seconds = max(MIN_RING_S, min(MAX_RING_S, seconds))
        rid = secrets.token_hex(4)
        answered: asyncio.Future = asyncio.get_running_loop().create_future()
        self._waiting[rid] = answered
        reached = [d for d in device_ids if await self.hub.send(d, {"type": "ring", "ring": rid, "seconds": seconds})]
        try:
            if not reached:
                return None
            return await asyncio.wait_for(answered, seconds + 1)  # a little slack for the speaker's own timer
        except asyncio.TimeoutError:
            return None
        finally:
            self._waiting.pop(rid, None)
            for d in reached:
                await self.hub.send(d, {"type": "ring_stop", "ring": rid})

    def answered(self, device_id: str, rid: str) -> None:
        f = self._waiting.get(rid)
        if f is not None and not f.done():
            f.set_result(device_id)
