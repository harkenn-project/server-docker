"""A small MQTT 3.1.1 client (asyncio): what the Home Assistant bridge needs, and nothing more.

QoS 0 only, retained messages, a last will, subscriptions and keepalive pings. Enough for MQTT discovery; avoids a
dependency for a few hundred bytes of protocol.
"""
from __future__ import annotations

import asyncio
import struct
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

CONNECT, CONNACK, PUBLISH, SUBSCRIBE, SUBACK, PINGREQ, PINGRESP, DISCONNECT = 1, 2, 3, 8, 9, 12, 13, 14
CONNACK_ERRORS = {1: "unsupported protocol version", 2: "client id rejected", 3: "broker unavailable",
                  4: "bad username or password", 5: "not authorized"}


class MQTTError(Exception):
    pass


@dataclass(frozen=True)
class Broker:
    host: str
    port: int = 1883
    username: str = ""
    password: str = ""
    tls: bool = False

    @classmethod
    def parse(cls, url: str) -> "Broker":
        """mqtt://[user:password@]host[:port] (mqtts:// for TLS, port 8883 by default)."""
        u = urlsplit(url.strip())
        if u.scheme not in ("mqtt", "mqtts") or not u.hostname:
            raise ValueError("the broker address looks like mqtt://host:1883")
        tls = u.scheme == "mqtts"
        return cls(u.hostname, u.port or (8883 if tls else 1883), unquote(u.username or ""), unquote(u.password or ""),
                   tls)


def encode_length(n: int) -> bytes:
    out = bytearray()
    while True:
        byte, n = n % 128, n // 128
        out.append(byte | (0x80 if n else 0))
        if not n:
            return bytes(out)


def string(s: str | bytes) -> bytes:
    data = s.encode() if isinstance(s, str) else s
    return struct.pack("!H", len(data)) + data


def packet(kind: int, flags: int, body: bytes) -> bytes:
    return bytes([kind << 4 | flags]) + encode_length(len(body)) + body


def connect_packet(client_id: str, keepalive: int, username: str = "", password: str = "",
                   will: tuple[str, bytes, bool] | None = None) -> bytes:
    flags = 0x02  # clean session
    payload = string(client_id)
    if will:
        topic, message, retain = will
        flags |= 0x04 | (0x20 if retain else 0)
        payload += string(topic) + string(message)
    if username:
        flags |= 0x80
        payload += string(username)
        if password:
            flags |= 0x40
            payload += string(password)
    return packet(CONNECT, 0, string("MQTT") + bytes([4, flags]) + struct.pack("!H", keepalive) + payload)


def publish_packet(topic: str, payload: bytes, retain: bool = False) -> bytes:
    return packet(PUBLISH, 1 if retain else 0, string(topic) + payload)


def subscribe_packet(packet_id: int, topics: list[str]) -> bytes:
    return packet(SUBSCRIBE, 2, struct.pack("!H", packet_id) + b"".join(string(t) + b"\x00" for t in topics))


async def read_packet(reader: asyncio.StreamReader) -> tuple[int, int, bytes]:
    first = (await reader.readexactly(1))[0]
    length, shift = 0, 0
    while True:
        byte = (await reader.readexactly(1))[0]
        length |= (byte & 0x7F) << shift
        if not byte & 0x80:
            break
        shift += 7
        if shift > 21:
            raise MQTTError("bad packet length")
    return first >> 4, first & 0x0F, await reader.readexactly(length) if length else b""


def parse_publish(flags: int, body: bytes) -> tuple[str, bytes]:
    n = struct.unpack_from("!H", body)[0]
    topic = body[2:2 + n].decode(errors="replace")
    rest = body[2 + n:]
    if flags & 0x06:  # QoS 1/2 carry a packet id (we only ask for QoS 0, but a broker may still send it)
        rest = rest[2:]
    return topic, rest


class Client:
    """One connection. connect(), then subscribe()/publish(); run() reads until the connection drops (raises)."""

    def __init__(self, broker: Broker, client_id: str, keepalive: int = 30) -> None:
        self.broker = broker
        self.client_id = client_id
        self.keepalive = keepalive
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._lock = asyncio.Lock()
        self._packet_id = 0

    async def connect(self, will: tuple[str, bytes, bool] | None = None, timeout: float = 10) -> None:
        b = self.broker
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(b.host, b.port, ssl=b.tls or None), timeout)
        self._writer.write(connect_packet(self.client_id, self.keepalive, b.username, b.password, will))
        await self._writer.drain()
        kind, _, body = await asyncio.wait_for(read_packet(self._reader), timeout)
        if kind != CONNACK or len(body) < 2:
            raise MQTTError("the broker didn't answer the connection")
        if body[1]:
            raise MQTTError(f"the broker refused: {CONNACK_ERRORS.get(body[1], f'code {body[1]}')}")

    async def _send(self, data: bytes) -> None:
        if not self._writer:
            raise MQTTError("not connected")
        async with self._lock:
            self._writer.write(data)
            await self._writer.drain()

    async def publish(self, topic: str, payload: str | bytes, retain: bool = False) -> None:
        await self._send(publish_packet(topic, payload.encode() if isinstance(payload, str) else payload, retain))

    async def subscribe(self, *topics: str) -> None:
        self._packet_id = self._packet_id % 65535 + 1
        await self._send(subscribe_packet(self._packet_id, list(topics)))

    async def run(self, on_message: Callable[[str, bytes], Awaitable[None]]) -> None:
        """Read messages until the connection ends; pings the broker so it stays open."""
        assert self._reader
        pinger = asyncio.create_task(self._ping())
        try:
            while True:
                kind, flags, body = await asyncio.wait_for(read_packet(self._reader), self.keepalive * 2)
                if kind == PUBLISH:
                    await on_message(*parse_publish(flags, body))
        finally:
            pinger.cancel()

    async def _ping(self) -> None:
        while True:
            await asyncio.sleep(self.keepalive / 2)
            await self._send(packet(PINGREQ, 0, b""))

    async def close(self) -> None:
        if self._writer:
            try:
                await self._send(packet(DISCONNECT, 0, b""))
                self._writer.close()
            except Exception:
                pass
        self._reader = self._writer = None
