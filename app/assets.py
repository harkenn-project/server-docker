"""Animated characters.

Each character is a directory holding ``character.json`` plus one 240x240 GIF per state. GIFs are normalized on
upload (square canvas, 1-bit transparency, capped frame count) and resized on demand to the exact pixel size a layout
needs, so the speaker never has to scale anything.
"""
from __future__ import annotations

import io
import json
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps, ImageSequence, UnidentifiedImageError

STATES = ("idle", "listening", "thinking", "speaking", "sleeping", "error")
MASTER_SIZE = 240
MAX_FRAMES = 120
MIN_FRAME_MS = 20
TRANSPARENT = 255  # palette index reserved for transparency
ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")

Image.MAX_IMAGE_PIXELS = 4096 * 4096


def fit_square(frame: Image.Image, size: int) -> Image.Image:
    """Scale a frame to fit inside size x size, centred on a transparent canvas."""
    rgba = ImageOps.contain(frame.convert("RGBA"), (size, size), Image.Resampling.LANCZOS)
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    canvas.paste(rgba, ((size - rgba.width) // 2, (size - rgba.height) // 2))
    return canvas


def _to_palette(rgba: Image.Image) -> Image.Image:
    p = rgba.convert("RGB").quantize(colors=255, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
    palette = p.getpalette()[: 255 * 3]
    p.putpalette(palette + [0] * (768 - len(palette)))
    p.paste(TRANSPARENT, mask=rgba.getchannel("A").point(lambda a: 255 if a < 128 else 0))
    return p


def encode_gif(frames: list[Image.Image], durations: list[int]) -> bytes:
    paletted = [_to_palette(f) for f in frames]
    buf = io.BytesIO()
    paletted[0].save(buf, "GIF", save_all=True, append_images=paletted[1:], duration=durations, loop=0,
                     transparency=TRANSPARENT, disposal=2, optimize=False)
    return buf.getvalue()


def _frames(im: Image.Image, size: int) -> tuple[list[Image.Image], list[int]]:
    frames, durations = [], []
    for frame in ImageSequence.Iterator(im):
        frames.append(fit_square(frame, size))
        durations.append(max(MIN_FRAME_MS, int(frame.info.get("duration") or 100)))
        if len(frames) >= MAX_FRAMES:
            break
    return frames, durations


def decode_animation(data: bytes, size: int = MASTER_SIZE) -> tuple[list[Image.Image], list[int]]:
    try:
        with Image.open(io.BytesIO(data)) as im:
            if im.format not in ("GIF", "PNG", "WEBP"):
                raise ValueError(f"unsupported image format {im.format}; upload a GIF, PNG or WebP")
            return _frames(im, size)
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError) as e:
        raise ValueError(f"could not read image: {e}") from e


class CharacterStore:
    def __init__(self, root: Path, cache: Path) -> None:
        self.root = root
        self.cache = cache
        root.mkdir(parents=True, exist_ok=True)
        cache.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _dir(self, cid: str) -> Path:
        if not ID_RE.match(cid):
            raise ValueError(f"invalid character id '{cid}'")
        return self.root / cid

    def _write_meta(self, cid: str, meta: dict[str, Any]) -> None:
        path = self._dir(cid) / "character.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(meta, indent=2))
        tmp.replace(path)

    def get(self, cid: str) -> dict[str, Any] | None:
        if not ID_RE.match(cid):
            return None
        path = self.root / cid / "character.json"
        return json.loads(path.read_text()) if path.is_file() else None

    def list(self) -> list[dict[str, Any]]:
        return [m for d in sorted(self.root.iterdir()) if d.is_dir() and (m := self.get(d.name))]

    def create(self, cid: str, name: str) -> dict[str, Any]:
        with self._lock:
            d = self._dir(cid)
            if d.exists():
                raise FileExistsError(f"character '{cid}' already exists")
            d.mkdir()
            meta = {"id": cid, "name": name, "version": 1, "created_at": time.time(), "states": {}}
            self._write_meta(cid, meta)
            return meta

    def delete(self, cid: str) -> None:
        with self._lock:
            shutil.rmtree(self._dir(cid), ignore_errors=True)
            shutil.rmtree(self.cache / cid, ignore_errors=True)

    def save_state(self, cid: str, state: str, frames: list[Image.Image], durations: list[int]) -> dict[str, Any]:
        if state not in STATES:
            raise ValueError(f"unknown state '{state}'")
        data = encode_gif(frames, durations)
        with self._lock:
            meta = self.get(cid)
            if meta is None:
                raise KeyError(cid)
            (self._dir(cid) / f"{state}.gif").write_bytes(data)
            meta["states"][state] = {"frames": len(frames), "duration_ms": sum(durations),
                                     "bytes": len(data), "updated_at": time.time()}
            meta["version"] += 1
            self._write_meta(cid, meta)
            return meta

    def import_state(self, cid: str, state: str, data: bytes) -> dict[str, Any]:
        frames, durations = decode_animation(data)
        if not frames:
            raise ValueError("image has no frames")
        return self.save_state(cid, state, frames, durations)

    def state_file(self, cid: str, state: str, size: int) -> Path | None:
        """Path of the GIF for a state at the requested size, rendering and caching it if needed."""
        meta = self.get(cid)
        if meta is None or state not in meta["states"]:
            return None
        master = self._dir(cid) / f"{state}.gif"
        if size == MASTER_SIZE:
            return master
        version_dir = self.cache / cid / str(meta["version"])
        out = version_dir / f"{state}_{size}.gif"
        if not out.exists():
            with Image.open(master) as im:
                frames, durations = _frames(im, size)
            version_dir.mkdir(parents=True, exist_ok=True)
            for old in version_dir.parent.iterdir():
                if old != version_dir:
                    shutil.rmtree(old, ignore_errors=True)
            tmp = out.with_suffix(".tmp")
            tmp.write_bytes(encode_gif(frames, durations))
            tmp.replace(out)
        return out

    def manifest(self, cid: str, sizes: list[int], base_url: str) -> dict[str, Any] | None:
        meta = self.get(cid)
        if meta is None or "idle" not in meta["states"]:
            return None
        states = {}
        for state in STATES:
            source = state if state in meta["states"] else "idle"
            info = meta["states"][source]
            states[state] = {
                "source": source,
                "frames": info["frames"],
                "duration_ms": info["duration_ms"],
                "urls": {str(s): f"{base_url}/assets/characters/{cid}/{source}.gif?size={s}&v={meta['version']}"
                         for s in sizes},
            }
        return {"id": cid, "name": meta["name"], "version": meta["version"], "states": states}
