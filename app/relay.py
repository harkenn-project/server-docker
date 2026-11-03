"""Speakers away from home, through the relay (docs/RELAY.md).

A speaker set up for remote connection that can't reach Harkenn connects to the relay instead (HARKENN_RELAY_URL,
e.g. wss://flash.mylife.ninja). While such a speaker is offline here, this server connects to the relay for it
too, and the relay pairs the two. Everything between them is encrypted with a key only they have, so the relay just
passes frames along. Inside, the speaker's usual WebSocket conversation runs as if it were connected directly
(device_api.serve_device), plus "fetch" for the files it would normally download from here.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import secrets
import struct
from contextlib import suppress
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import httpx
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from websockets.asyncio.client import connect

if TYPE_CHECKING:
    from fastapi import FastAPI

    from .context import Context

log = logging.getLogger("harkenn.relay")

KEY = "relay"  # settings table: {device_id: {"id", "key", "ready"}}
CHECK_EVERY_S = 15  # how often to look for remote speakers that went offline
RETRY_S = 10
HANDSHAKE_S = 20
# Frame types (the first byte of every binary frame through the relay)
HELLO, ANSWER, OK, DATA = 0x01, 0x02, 0x03, 0x10
# What a DATA frame carries (the first byte of its plaintext)
TEXT, BINARY, FILE = b"T", b"B", b"F"
FETCH_PREFIXES = ("/assets/", "/api/v1/screens/", "/api/v1/device/firmware/", "/api/v1/characters/")
FETCH_CHUNK = 16 * 1024
MAX_FETCH = 4 * 1024 * 1024


# credentials

def _all(ctx: Context) -> dict[str, Any]:
    return ctx.db.get_setting(KEY) or {}


def credentials(ctx: Context, device_id: str) -> dict[str, Any]:
    """The speaker's relay id and key, made the first time remote connection is turned on."""
    every = _all(ctx)
    entry = every.get(device_id)
    if not entry:
        entry = every[device_id] = {"id": secrets.token_hex(16), "key": secrets.token_hex(32), "ready": False}
        ctx.db.set_setting(KEY, every)
    return entry


def mark_ready(ctx: Context, device_id: str, relay_id: str) -> bool:
    """The speaker reported it has these credentials: from now on, Harkenn meets it at the relay when it's offline.
    Returns True if that's new."""
    every = _all(ctx)
    entry = every.get(device_id)
    if not entry or entry["id"] != relay_id or entry.get("ready"):
        return False
    entry["ready"] = True
    ctx.db.set_setting(KEY, every)
    return True


def forget(ctx: Context, device_id: str) -> None:
    every = _all(ctx)
    if every.pop(device_id, None) is not None:
        ctx.db.set_setting(KEY, every)


def enabled(ctx: Context, device: dict[str, Any]) -> bool:
    return bool(ctx.settings.relay_url) and bool((device["config"] or {}).get("remote"))


def payload(ctx: Context, device: dict[str, Any]) -> dict[str, Any] | None:
    """The "remote" part of a speaker's config: where the relay is and its credentials; null = not remote."""
    if not enabled(ctx, device):
        return None
    entry = credentials(ctx, device["id"])
    return {"url": ctx.settings.relay_url, "id": entry["id"], "key": entry["key"]}


# the encrypted channel

def _mac(key: bytes, label: bytes, *parts: bytes) -> bytes:
    return hmac.new(key, label + b"".join(parts), hashlib.sha256).digest()


class Channel:
    """One session's keys: one per direction, from the relay key and both sides' nonces. Every frame is AES-256-GCM
    with a counter that must go up, so nothing can be replayed or reordered."""

    def __init__(self, key: bytes, harkenn_nonce: bytes, speaker_nonce: bytes) -> None:
        self.send_key = AESGCM(_mac(key, b"harkenn-relay h2s", harkenn_nonce, speaker_nonce))
        self.recv_key = AESGCM(_mac(key, b"harkenn-relay s2h", harkenn_nonce, speaker_nonce))
        self.sent = 0
        self.received = 0

    def seal(self, kind: bytes, data: bytes) -> bytes:
        self.sent += 1
        counter = struct.pack(">Q", self.sent)
        return bytes([DATA]) + counter + self.send_key.encrypt(b"\0\0\0\0" + counter, kind + data, None)

    def open(self, frame: bytes) -> tuple[bytes, bytes]:
        if len(frame) < 1 + 8 + 16 + 1 or frame[0] != DATA:
            raise ValueError("not a data frame")
        (counter,) = struct.unpack(">Q", frame[1:9])
        if counter <= self.received:
            raise ValueError("replayed or out-of-order frame")
        plain = self.recv_key.decrypt(b"\0\0\0\0" + frame[1:9], frame[9:], None)  # InvalidTag if tampered with
        self.received = counter
        return plain[:1], plain[1:]


class RelaySocket:
    """A speaker's connection through the relay, looking like its WebSocket to the rest of the server (hub,
    serve_device). The speaker's "fetch" requests are answered here."""

    def __init__(self, ws: Any, channel: Channel, app: FastAPI, token: str) -> None:
        self.ws = ws
        self.channel = channel
        self.app = app
        self.token = token
        self.lock = asyncio.Lock()
        self.fetches: set[asyncio.Task] = set()

    async def _send(self, kind: bytes, data: bytes) -> None:
        async with self.lock:  # seal and send in order: the counter must arrive in sequence
            await self.ws.send(self.channel.seal(kind, data))

    async def send_json(self, message: Any) -> None:
        await self._send(TEXT, json.dumps(message).encode())

    async def send_bytes(self, data: bytes) -> None:
        await self._send(BINARY, data)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        for task in self.fetches:
            task.cancel()
        with suppress(Exception):
            await self.ws.close(code, reason)

    async def receive(self) -> dict[str, Any]:
        """The next frame from the speaker, as Starlette gives it (websocket.receive / websocket.disconnect)."""
        while True:
            try:
                frame = await self.ws.recv()
            except Exception:
                return {"type": "websocket.disconnect", "code": 1006}
            if isinstance(frame, str):
                continue  # the relay's own messages
            try:
                kind, data = self.channel.open(frame)
            except (ValueError, InvalidTag) as e:
                log.warning("dropping the relay session: %s", e)
                return {"type": "websocket.disconnect", "code": 1008}
            if kind == BINARY:
                return {"type": "websocket.receive", "bytes": data}
            if kind != TEXT:
                continue
            text = data.decode(errors="replace")
            if '"fetch"' in text:
                msg = json.loads(text)
                if isinstance(msg, dict) and msg.get("type") == "fetch":
                    task = asyncio.create_task(self._fetch(msg))
                    self.fetches.add(task)
                    task.add_done_callback(self.fetches.discard)
                    continue
            return {"type": "websocket.receive", "text": text}

    async def _fetch(self, msg: dict[str, Any]) -> None:
        """A file the speaker would normally download from here (an animation, a sound, a firmware image): read from
        this server itself, sent as FILE frames."""
        fid = int(msg.get("id") or 0) & 0xFFFFFFFF
        url = urlsplit(str(msg.get("url") or ""))
        path = url.path + (f"?{url.query}" if url.query else "")
        status, body = 403, b""
        if url.path.startswith(FETCH_PREFIXES) and ".." not in url.path:
            try:
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),
                                             base_url="http://harkenn.relay") as client:
                    r = await client.get(path, headers={"Authorization": f"Bearer {self.token}"})
                status, body = r.status_code, r.content
            except Exception as e:
                log.warning("fetch %s: %s", url.path, e)
                status = 500
            if len(body) > MAX_FETCH:
                status, body = 413, b""
        if status != 200:
            body = b""
        await self.send_json({"type": "fetch_result", "id": fid, "status": status, "size": len(body)})
        head = struct.pack(">I", fid)
        for at in range(0, len(body), FETCH_CHUNK):
            await self._send(FILE, head + body[at:at + FETCH_CHUNK])


# meeting speakers at the relay

class RelayManager:
    """While a speaker set up for remote connection is offline here, wait for it at the relay."""

    def __init__(self, ctx: Context, app: FastAPI) -> None:
        self.ctx = ctx
        self.app = app
        self.tasks: dict[str, asyncio.Task] = {}
        self._loop: asyncio.Task | None = None

    def start(self) -> None:
        if self.ctx.settings.relay_url and self._loop is None:
            self._loop = asyncio.create_task(self._watch())

    async def stop(self) -> None:
        for task in [self._loop, *self.tasks.values()]:
            if task:
                task.cancel()
        self._loop = None
        self.tasks.clear()

    async def _watch(self) -> None:
        while True:
            with suppress(Exception):
                self.check()
            await asyncio.sleep(CHECK_EVERY_S)

    def check(self) -> None:
        ready = _all(self.ctx)
        for device in self.ctx.db.list_devices():
            did = device["id"]
            wanted = (enabled(self.ctx, device) and ready.get(did, {}).get("ready")
                      and (not self.ctx.hub.online(did) or self.ctx.hub.via(did) == "relay"))
            task = self.tasks.get(did)
            if wanted and (task is None or task.done()):
                self.tasks[did] = asyncio.create_task(self._meet(did))
            elif not wanted and task is not None:
                task.cancel()
                del self.tasks[did]

    async def _meet(self, device_id: str) -> None:
        from .device_api import hash_token, serve_device  # device_api imports the context, which imports this
        while True:
            device = self.ctx.db.get_device(device_id)
            entry = _all(self.ctx).get(device_id)
            if not device or not entry or not enabled(self.ctx, device):
                return
            if self.ctx.hub.online(device_id) and self.ctx.hub.via(device_id) != "relay":
                return  # it reached us directly
            url = f"{self.ctx.settings.relay_server_url}/relay/harkenn/{entry['id']}"
            try:
                async with connect(url, max_size=2 * 1024 * 1024, ping_interval=30, open_timeout=15) as ws:
                    await self._wait_paired(ws)
                    channel = await asyncio.wait_for(self._handshake(ws, bytes.fromhex(entry["key"])), HANDSHAKE_S)
                    sock = RelaySocket(ws, channel, self.app, "")
                    hello = await asyncio.wait_for(sock.receive(), HANDSHAKE_S)
                    msg = json.loads(hello.get("text") or "null")
                    token = msg.get("token", "") if isinstance(msg, dict) and msg.get("type") == "hello" else ""
                    if not token or device["token_hash"] != hash_token(token):
                        log.warning("relay: %s didn't prove its token", device_id)
                        await sock.close(4401, "unknown or revoked device token")
                        await asyncio.sleep(RETRY_S)
                        continue
                    sock.token = token
                    log.info("device %s connected through the relay", device_id)
                    await serve_device(self.ctx, sock, device, None, self.ctx.public_url(), via="relay")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.info("relay for %s: %s", device_id, e)
            await asyncio.sleep(RETRY_S)

    @staticmethod
    async def _wait_paired(ws: Any) -> None:
        async for frame in ws:  # until the speaker comes (could be a long wait)
            if isinstance(frame, str) and json.loads(frame).get("type") == "paired":
                return
        raise ConnectionError("relay closed before the speaker came")

    @staticmethod
    async def _handshake(ws: Any, key: bytes) -> Channel:
        harkenn_nonce = secrets.token_bytes(16)
        await ws.send(bytes([HELLO]) + harkenn_nonce)
        while True:
            frame = await ws.recv()
            if isinstance(frame, bytes) and len(frame) == 1 + 16 + 16 and frame[0] == ANSWER:
                break
        speaker_nonce, proof = frame[1:17], frame[17:]
        if not hmac.compare_digest(proof, _mac(key, b"harkenn-relay speaker", harkenn_nonce, speaker_nonce)[:16]):
            raise ConnectionError("the speaker at the relay doesn't have the key")
        await ws.send(bytes([OK]) + _mac(key, b"harkenn-relay harkenn", harkenn_nonce, speaker_nonce)[:16])
        return Channel(key, harkenn_nonce, speaker_nonce)
