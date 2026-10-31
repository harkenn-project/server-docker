"""A minimal client for the Wyoming protocol (Rhasspy / Home Assistant voice services), used when speech-to-text or
text-to-speech runs in a separate service such as wyoming-faster-whisper or wyoming-piper.

An event is a JSON header line ({"type", "data_length", "payload_length"}), then that many bytes of JSON data, then
that many bytes of binary payload (audio).
"""
from __future__ import annotations

import json
import socket
from typing import Any

TIMEOUT_S = 60
CHUNK_BYTES = 4096


def parse_address(address: str, default_port: int) -> tuple[str, int]:
    """'host', 'host:port' or 'tcp://host:port'."""
    address = address.strip().removeprefix("tcp://").rstrip("/")
    host, _, port = address.rpartition(":") if ":" in address else (address, "", "")
    return (host or address), int(port) if port else default_port


class Connection:
    def __init__(self, address: str, default_port: int) -> None:
        self.sock = socket.create_connection(parse_address(address, default_port), timeout=TIMEOUT_S)
        self.reader = self.sock.makefile("rb")

    def __enter__(self) -> "Connection":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.reader.close()
        self.sock.close()

    def send(self, kind: str, data: dict[str, Any] | None = None, payload: bytes = b"") -> None:
        body = json.dumps(data or {}).encode()
        header = {"type": kind, "version": "1.5.0", "data_length": len(body), "payload_length": len(payload)}
        self.sock.sendall(json.dumps(header).encode() + b"\n" + body + payload)

    def receive(self) -> tuple[str, dict[str, Any], bytes]:
        line = self.reader.readline()
        if not line:
            raise ConnectionError("Wyoming service closed the connection")
        header = json.loads(line)
        data = dict(header.get("data") or {})
        if header.get("data_length"):
            data.update(json.loads(self._read(header["data_length"])))
        payload = self._read(header["payload_length"]) if header.get("payload_length") else b""
        return header["type"], data, payload

    def _read(self, n: int) -> bytes:
        buf = self.reader.read(n)
        if len(buf) != n:
            raise ConnectionError("Wyoming service closed the connection mid-event")
        return buf


def transcribe(address: str, pcm: bytes, rate: int, language: str) -> str:
    fmt = {"rate": rate, "width": 2, "channels": 1}
    with Connection(address, 10300) as c:
        c.send("transcribe", {"language": language} if language else {})
        c.send("audio-start", fmt)
        for i in range(0, len(pcm), CHUNK_BYTES):
            c.send("audio-chunk", fmt, pcm[i:i + CHUNK_BYTES])
        c.send("audio-stop")
        while True:
            kind, data, _ = c.receive()
            if kind == "transcript":
                return str(data.get("text", ""))
            if kind == "error":
                raise RuntimeError(data.get("text", "speech-to-text error"))


def synthesize(address: str, text: str, voice: str) -> tuple[bytes, int]:
    """Returns 16-bit mono PCM and its sample rate."""
    with Connection(address, 10200) as c:
        c.send("synthesize", {"text": text, "voice": {"name": voice}} if voice else {"text": text})
        audio, rate = bytearray(), 22050
        while True:
            kind, data, payload = c.receive()
            if kind == "audio-start":
                rate = int(data.get("rate", rate))
                if data.get("width", 2) != 2 or data.get("channels", 1) != 1:
                    raise RuntimeError("text-to-speech service must send 16-bit mono audio")
            elif kind == "audio-chunk":
                audio += payload
            elif kind == "audio-stop":
                return bytes(audio), rate
            elif kind == "error":
                raise RuntimeError(data.get("text", "text-to-speech error"))
