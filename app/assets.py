"""Animated characters.

Each character is a directory holding ``character.json`` plus one master animation per state: a 240x240 APNG with
full alpha (``<state>.png``). Uploads are normalized into that form (square canvas, capped frame count).

Two kinds of derived files are rendered on demand and cached per character version:

* ``.gif`` previews for the admin UI (transparent, any size)
* ``.kani`` animations for speakers: exact widget size, flattened onto the speaker's background colour
  (see kani.py and docs/PROTOCOL.md)
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

from . import kani

STATES = ("idle", "listening", "thinking", "speaking", "sleeping", "error", "muted", "ringing")
MASTER_SIZE = 240
LARGE_SIZE = 160  # speaker animations above this size are sent at half frame rate
MAX_FRAMES = 120
MIN_FRAME_MS = 20
TRANSPARENT = 255  # GIF palette index reserved for transparency
ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
HEX_RE = re.compile(r"^[0-9a-fA-F]{6}$")

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


def encode_apng(frames: list[Image.Image], durations: list[int]) -> bytes:
    buf = io.BytesIO()
    frames[0].save(buf, "PNG", save_all=True, append_images=frames[1:], duration=durations, loop=0)
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

    def _master(self, cid: str, state: str) -> Path:
        png = self._dir(cid) / f"{state}.png"
        return png if png.exists() else self._dir(cid) / f"{state}.gif"  # .gif: stored by older versions

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
        metas = [m for d in sorted(self.root.iterdir()) if d.is_dir() and (m := self.get(d.name))]
        return sorted(metas, key=lambda m: (m.get("order", 100), m["name"].lower()))

    def create(self, cid: str, name: str, **extra: Any) -> dict[str, Any]:
        with self._lock:
            d = self._dir(cid)
            if d.exists():
                raise FileExistsError(f"character '{cid}' already exists")
            d.mkdir()
            meta = {"id": cid, "name": name, "version": 1, "created_at": time.time(), "states": {}, **extra}
            self._write_meta(cid, meta)
            return meta

    def delete(self, cid: str) -> None:
        with self._lock:
            shutil.rmtree(self._dir(cid), ignore_errors=True)
            shutil.rmtree(self.cache / cid, ignore_errors=True)

    def update_meta(self, cid: str, **fields: Any) -> dict[str, Any]:
        with self._lock:
            meta = self.get(cid)
            if meta is None:
                raise KeyError(cid)
            for k, v in fields.items():
                if v is None:
                    meta.pop(k, None)
                else:
                    meta[k] = v
            self._write_meta(cid, meta)
            return meta

    def save_state(self, cid: str, state: str, frames: list[Image.Image], durations: list[int]) -> dict[str, Any]:
        if state not in STATES:
            raise ValueError(f"unknown state '{state}'")
        data = encode_apng(frames, durations)
        with self._lock:
            meta = self.get(cid)
            if meta is None:
                raise KeyError(cid)
            (self._dir(cid) / f"{state}.png").write_bytes(data)
            (self._dir(cid) / f"{state}.gif").unlink(missing_ok=True)
            meta["states"][state] = {"frames": len(frames), "duration_ms": sum(durations),
                                     "bytes": len(data), "updated_at": time.time()}
            meta["version"] += 1
            self._write_meta(cid, meta)
            return meta

    def import_state(self, cid: str, state: str, data: bytes) -> dict[str, Any]:
        frames, durations = decode_animation(data)
        if not frames:
            raise ValueError("image has no frames")
        self.save_state(cid, state, frames, durations)
        return self.update_meta(cid, customized=True)  # never overwritten by a newer built-in drawing

    def _derived(self, cid: str, state: str, name: str, render) -> Path | None:
        meta = self.get(cid)
        if meta is None or state not in meta["states"]:
            return None
        version_dir = self.cache / cid / str(meta["version"])
        out = version_dir / name
        if not out.exists():
            with Image.open(self._master(cid, state)) as im:
                data = render(im)
            version_dir.mkdir(parents=True, exist_ok=True)
            for old in version_dir.parent.iterdir():
                if old != version_dir:
                    shutil.rmtree(old, ignore_errors=True)
            tmp = out.with_suffix(f".{threading.get_ident()}.tmp")
            tmp.write_bytes(data)
            tmp.replace(out)
        return out

    def preview_gif(self, cid: str, state: str, size: int) -> Path | None:
        """Transparent GIF for the admin UI."""
        return self._derived(cid, state, f"{state}_{size}.gif", lambda im: encode_gif(*_frames(im, size)))

    def device_animation(self, cid: str, state: str, size: int, background: str) -> Path | None:
        """KANI animation for a speaker, at the widget's size, flattened onto its background colour."""
        if not HEX_RE.match(background):
            raise ValueError("background must be 6 hex digits")
        background = background.lower()

        def render(im: Image.Image) -> bytes:
            frames, durations = _frames(im, size)
            if size > LARGE_SIZE and len(frames) > 1:
                # Full-screen animations would not fit in the speaker's memory at full frame rate: halve it.
                durations = [a + b for a, b in zip(durations[::2], durations[1::2] + [0])]
                frames = frames[::2]
            return kani.encode(frames, durations, background)

        return self._derived(cid, state, f"{state}_{size}_{background}.kani", render)

    def manifest(self, cid: str, sizes: list[int], base_url: str, background: str) -> dict[str, Any] | None:
        meta = self.get(cid)
        if meta is None or "idle" not in meta["states"]:
            return None
        background = background.lower()
        states = {}
        for state in STATES:
            source = state if state in meta["states"] else "idle"
            info = meta["states"][source]
            states[state] = {
                "source": source,
                "frames": info["frames"],
                "duration_ms": info["duration_ms"],
                "urls": {str(s): f"{base_url}/assets/characters/{cid}/{source}.kani"
                                 f"?size={s}&bg={background}&v={meta['version']}"
                         for s in sizes},
            }
        return {"id": cid, "name": meta["name"], "version": meta["version"], "format": "kani", "states": states}
