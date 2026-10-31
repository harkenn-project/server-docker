"""KANI: the animation format sent to speakers.

LVGL's GIF decoder left stray pixels behind when animations looped, so speakers get a format that is trivial to
decode correctly: native RGB565 pixels, frame 0 complete, later frames as changes against the previous frame.

Layout (all little-endian):

    "KANI"  u8 version=1  u8 reserved  u16 width  u16 height  u16 frame_count
    frame_count x { u16 duration_ms  u32 offset  u32 length }       offsets are from the start of the file
    frame data

Frame data is a stream of ops covering the frame's pixels in row-major order:

    op byte: type = op >> 6, n = op & 0x3F; if n == 0x3F, n += next u16. count = n + 1
    type 0  SKIP     count pixels unchanged from the previous frame (never in frame 0)
    type 1  RUN      count pixels of the u16 colour that follows
    type 2  LITERAL  count pixels, count u16 colours follow

Pixels are RGB565 (r5 g6 b5, red in the high bits), the format LVGL renders on the speaker.
"""
from __future__ import annotations

import struct

import numpy as np
from PIL import Image

MAGIC = b"KANI"
VERSION = 1
SKIP, RUN, LITERAL = 0, 1, 2
MIN_RUN = 3  # shorter runs are cheaper as literals


def to_rgb565(frame: Image.Image) -> np.ndarray:
    rgb = np.asarray(frame.convert("RGB"), dtype=np.uint16)
    return ((rgb[..., 0] >> 3) << 11 | (rgb[..., 1] >> 2) << 5 | (rgb[..., 2] >> 3)).ravel()


def _op(out: bytearray, kind: int, count: int) -> None:
    n = count - 1
    if n < 0x3F:
        out.append(kind << 6 | n)
    else:
        out.append(kind << 6 | 0x3F)
        out += struct.pack("<H", n - 0x3F)


def _emit_changed(out: bytearray, px: np.ndarray) -> None:
    """Encode a span of changed pixels as runs and literals."""
    starts = np.flatnonzero(np.diff(px)) + 1
    bounds = np.concatenate(([0], starts, [len(px)]))
    literal_from = None
    for a, b in zip(bounds[:-1], bounds[1:]):
        if b - a >= MIN_RUN:
            if literal_from is not None:
                _literals(out, px[literal_from:a])
                literal_from = None
            _runs(out, int(px[a]), int(b - a))
        elif literal_from is None:
            literal_from = a
    if literal_from is not None:
        _literals(out, px[literal_from:])


def _runs(out: bytearray, color: int, count: int) -> None:
    while count:
        chunk = min(count, 0x3F + 0xFFFF + 1)
        _op(out, RUN, chunk)
        out += struct.pack("<H", color)
        count -= chunk


def _literals(out: bytearray, px: np.ndarray) -> None:
    for i in range(0, len(px), 0x3F + 0xFFFF + 1):
        chunk = px[i:i + 0x3F + 0xFFFF + 1]
        _op(out, LITERAL, len(chunk))
        out += chunk.astype("<u2").tobytes()


def encode_frame(px: np.ndarray, prev: np.ndarray | None) -> bytes:
    out = bytearray()
    if prev is None:
        _emit_changed(out, px)
        return bytes(out)
    changed = px != prev
    edges = np.flatnonzero(np.diff(changed.astype(np.int8))) + 1
    bounds = np.concatenate(([0], edges, [len(px)]))
    for a, b in zip(bounds[:-1], bounds[1:]):
        if changed[a]:
            _emit_changed(out, px[a:b])
        else:
            count = int(b - a)
            while count:
                chunk = min(count, 0x3F + 0xFFFF + 1)
                _op(out, SKIP, chunk)
                count -= chunk
    return bytes(out)


def encode(frames: list[Image.Image], durations: list[int], background: str) -> bytes:
    """Flatten RGBA frames onto `background` (6 hex digits) and encode them."""
    bg = tuple(int(background[i:i + 2], 16) for i in (0, 2, 4)) + (255,)
    w, h = frames[0].size
    bodies, prev = [], None
    for frame in frames:
        flat = Image.new("RGBA", frame.size, bg)
        flat.alpha_composite(frame.convert("RGBA"))
        px = to_rgb565(flat)
        bodies.append(encode_frame(px, prev))
        prev = px
    header = MAGIC + struct.pack("<BBHHH", VERSION, 0, w, h, len(frames))
    offset = len(header) + 10 * len(frames)
    table = bytearray()
    for body, ms in zip(bodies, durations):
        table += struct.pack("<HII", min(int(ms), 0xFFFF), offset, len(body))
        offset += len(body)
    return header + bytes(table) + b"".join(bodies)


def decode(data: bytes) -> list[np.ndarray]:
    """Reference decoder (used by the tests; the speaker has its own in C++)."""
    assert data[:4] == MAGIC
    _, _, w, h, n = struct.unpack_from("<BBHHH", data, 4)
    fb = np.zeros(w * h, dtype=np.uint16)
    frames = []
    for i in range(n):
        _, off, length = struct.unpack_from("<HII", data, 12 + 10 * i)
        pos, end, p = off, off + length, 0
        while pos < end:
            op = data[pos]
            pos += 1
            kind, count = op >> 6, op & 0x3F
            if count == 0x3F:
                count += struct.unpack_from("<H", data, pos)[0]
                pos += 2
            count += 1
            if kind == SKIP:
                assert i > 0, "frame 0 must not skip"
            elif kind == RUN:
                fb[p:p + count] = struct.unpack_from("<H", data, pos)[0]
                pos += 2
            else:
                fb[p:p + count] = np.frombuffer(data, "<u2", count, pos)
                pos += 2 * count
            p += count
        assert p == w * h, f"frame {i} covers {p} of {w * h} pixels"
        frames.append(fb.copy())
    return frames
