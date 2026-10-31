"""Firmware images for over-the-air updates.

Each upload is checked to be an ESP32 app image built from the Kotodama firmware: the firmware embeds a marker
string, ``KOTODAMA-FW|<version>|<hardware>|`` (see firmware/src/main.cpp), which gives its version and the board it
was built for. Files are stored as ``<id>.bin`` with a ``<id>.json`` sidecar.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from pathlib import Path
from typing import Any

ESP_IMAGE_MAGIC = 0xE9
MAX_FIRMWARE_BYTES = 0x3F0000  # an app partition (firmware/partitions.csv)
MARKER = re.compile(rb"KOTODAMA-FW\|([0-9A-Za-z.+_-]{1,32})\|([0-9A-Za-z._-]{1,64})\|")


def inspect(data: bytes) -> dict[str, str]:
    """The version and hardware of a firmware image; ValueError if it isn't a Kotodama app image."""
    if len(data) < 1024 or data[0] != ESP_IMAGE_MAGIC:
        raise ValueError("not an ESP32 app image (upload firmware.bin, not the merged or bootloader image)")
    if len(data) > MAX_FIRMWARE_BYTES:
        raise ValueError(f"image is larger than an app partition ({MAX_FIRMWARE_BYTES} bytes)")
    m = MARKER.search(data)
    if m is None:
        raise ValueError("not Kotodama speaker firmware (no version marker)")
    return {"version": m.group(1).decode(), "hardware": m.group(2).decode()}


class FirmwareStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def add(self, data: bytes) -> dict[str, Any]:
        info = inspect(data)
        sha = hashlib.sha256(data).hexdigest()
        fid = sha[:16]
        meta = {"id": fid, **info, "size": len(data), "sha256": sha, "uploaded_at": time.time()}
        with self._lock:
            existing = self.get(fid)
            if existing:
                return existing
            (self.root / f"{fid}.bin").write_bytes(data)
            (self.root / f"{fid}.json").write_text(json.dumps(meta))
        return meta

    def get(self, fid: str) -> dict[str, Any] | None:
        if not re.fullmatch(r"[0-9a-f]{16}", fid):
            return None
        path = self.root / f"{fid}.json"
        return json.loads(path.read_text()) if path.is_file() else None

    def path(self, fid: str) -> Path | None:
        return self.root / f"{fid}.bin" if self.get(fid) else None

    def list(self) -> list[dict[str, Any]]:
        items = [json.loads(p.read_text()) for p in self.root.glob("*.json")]
        return sorted(items, key=lambda m: m["uploaded_at"], reverse=True)

    def delete(self, fid: str) -> bool:
        if not self.get(fid):
            return False
        with self._lock:
            (self.root / f"{fid}.bin").unlink(missing_ok=True)
            (self.root / f"{fid}.json").unlink(missing_ok=True)
        return True
