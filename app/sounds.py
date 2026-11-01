"""Sound packs: what a speaker plays when it starts, wakes, stops listening, hits an error and shuts down.

    classic    the speaker's own chimes, synthesized on the speaker (nothing to download)
    built in   six more synthesized packs: Marimba, Glass, Retro, Kalimba, Cosmic, Lullaby
    voiced     a line of text per slot, spoken by a Piper voice (e.g. a character's catchphrases)
    custom     uploaded clips (any format PyAV reads), copied from another pack to start with; a slot can be silent

Clips are 16 kHz, 16-bit mono PCM, at most MAX_CLIP_S long. Speakers download their pack's clips into flash, so they
play instantly, even before the speaker is online (docs/PROTOCOL.md, config "sounds").
"""
from __future__ import annotations

import io
import json
import math
import re
import secrets
import shutil
import threading
import time
import wave
from pathlib import Path
from typing import Any, Callable

import numpy as np

from .voice.engines import RATE, resample

SLOTS = ("boot", "wake", "done", "error", "shutdown")
SLOT_LABELS = {"boot": "Starting up", "wake": "Wake word or tap", "done": "Finished listening", "error": "Error",
               "shutdown": "Shutting down"}
MAX_CLIP_S = 5.0
SYNTH_VERSION = 1  # bump when a built-in pack's sound changes
PACK_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


# synthesis

Note = tuple[float, float]  # (hz, seconds); hz 0 = rest


def _env(n: int, attack: float, decay: float) -> np.ndarray:
    t = np.arange(n) / RATE
    a = np.minimum(1.0, t / max(attack, 1e-4))
    e = a * np.exp(-t / max(decay, 1e-4))
    release = min(n, int(0.006 * RATE))
    if release:
        e[-release:] *= np.linspace(1, 0, release)
    return e


def _partials(partials: list[tuple[float, float]], attack: float, decay: float, ring: float = 1.0):
    """A tone made of (frequency ratio, level) partials; higher partials die away faster."""
    def tone(hz: float, dur: float) -> np.ndarray:
        n = int((dur * ring) * RATE)
        t = np.arange(n) / RATE
        out = np.zeros(n)
        for ratio, level in partials:
            f = hz * ratio
            if f < RATE / 2:
                out += level * np.sin(2 * math.pi * f * t) * _env(n, attack, decay / max(1.0, ratio ** 0.7))
        return out
    return tone


def _square(hz: float, dur: float) -> np.ndarray:
    n = int(dur * RATE)
    t = np.arange(n) / RATE
    out = sum(np.sin(2 * math.pi * hz * k * t) / k for k in (1, 3, 5, 7, 9) if hz * k < RATE / 2)
    return 0.6 * out * _env(n, 0.002, dur * 3)


def _chirp(hz: float, dur: float) -> np.ndarray:
    """A sweep up (hz > 0 in the next octave) with a little vibrato, for Cosmic."""
    n = int(dur * RATE * 1.2)
    t = np.arange(n) / RATE
    f = hz * (1 + 0.5 * t / dur) * (1 + 0.01 * np.sin(2 * math.pi * 7 * t))
    return np.sin(2 * math.pi * np.cumsum(f) / RATE) * _env(n, 0.01, dur * 0.8)


def _sequence(notes: list[Note], tone: Callable[[float, float], np.ndarray]) -> np.ndarray:
    """Notes start one after another but ring on, overlapping."""
    starts, at = [], 0.0
    for hz, dur in notes:
        starts.append(at)
        at += dur
    total = int((at + 1.5) * RATE)
    out = np.zeros(total)
    for (hz, dur), start in zip(notes, starts):
        if hz <= 0:
            continue
        x = tone(hz, dur)
        i = int(start * RATE)
        out[i:i + len(x)] += x[: total - i]
    return out


def _to_pcm(x: np.ndarray, level: float = 0.5) -> bytes:
    x = np.trim_zeros(np.where(np.abs(x) < 1e-4, 0, x), "b")
    peak = np.max(np.abs(x)) if len(x) else 0
    if peak > 0:
        x = x / peak * level
    x = x[: int(MAX_CLIP_S * RATE)]
    return (np.clip(x, -1, 1) * 32767).astype("<i2").tobytes()


def hz(note: str) -> float:
    """'A4' -> 440.0; sharps as 'C#5'."""
    names = {"C": -9, "D": -7, "E": -5, "F": -4, "G": -2, "A": 0, "B": 2}
    semis = names[note[0]] + (1 if "#" in note else 0) + 12 * (int(note[-1]) - 4)
    return 440.0 * 2 ** (semis / 12)


def _melody(spec: str, beat: float) -> list[Note]:
    """'C5 E5 G5:2 -' -> notes; ':n' lengthens by n beats, '-' is a rest."""
    out = []
    for tok in spec.split():
        name, _, mult = tok.partition(":")
        out.append((0.0 if name == "-" else hz(name), beat * float(mult or 1)))
    return out


# Each built-in pack: (name, description, tone, beat in seconds, level, {slot: melody})
SYNTH_PACKS: dict[str, tuple[str, str, Callable, float, float, dict[str, str]]] = {
    "classic": ("Classic", "Harkenn's soft bells", _partials([(1, 0.85), (2, 0.15)], 0.004, 0.25), 0.09, 0.5,
                {"boot": "C5 E5 G5 C6:3", "wake": "G5 D6:2", "done": "D6 G5:2", "error": "A3:2 - G#3:3",
                 "shutdown": "C6 G5 E5 C5:3"}),
    "marimba": ("Marimba", "Warm wooden mallets", _partials([(1, 1.0), (4, 0.25), (10, 0.06)], 0.002, 0.18), 0.11,
                0.55, {"boot": "G4 C5 E5 G5 C6:2", "wake": "E5 A5:2", "done": "A5 E5:2", "error": "D4 C#4:3",
                       "shutdown": "C6 G5 E5 C5:3"}),
    "glass": ("Glass", "Bright, shimmering crystal", _partials([(1, 0.8), (2.76, 0.25), (5.4, 0.12)], 0.003, 0.7),
              0.14, 0.45, {"boot": "E6 B6 E7:3", "wake": "B6 E7:3", "done": "E7 B6:3", "error": "C6 B5:4",
                           "shutdown": "E7 B6 E6:4"}),
    "retro": ("Retro", "8-bit video game beeps", _square, 0.07, 0.4,
              {"boot": "C5 E5 G5 C6 E6 G6:3", "wake": "C6 G6:2", "done": "G6 C6:2", "error": "G4 F#4 F4 E4:4",
               "shutdown": "G6 E6 C6 G5 E5 C5:3"}),
    "kalimba": ("Kalimba", "Plucked thumb piano", _partials([(1, 1.0), (5.4, 0.3), (8.9, 0.08)], 0.001, 0.35), 0.12,
                0.55, {"boot": "D5 F#5 A5 D6:3", "wake": "A5 D6:2", "done": "D6 A5:2", "error": "F#4 F4:3",
                       "shutdown": "D6 A5 F#5 D5:3"}),
    "cosmic": ("Cosmic", "Spacey sweeps", _chirp, 0.16, 0.4,
               {"boot": "C4 G4 C5:3", "wake": "G5:2", "done": "C5:2", "error": "C3:4", "shutdown": "C5 G4 C4:3"}),
    "lullaby": ("Lullaby", "Soft and gentle, for bedrooms", _partials([(1, 1.0), (2, 0.05)], 0.06, 0.6), 0.2, 0.35,
                {"boot": "C5 E5 G5:3", "wake": "E5 G5:2", "done": "G5 E5:2", "error": "E4 D4:3",
                 "shutdown": "G5 E5 C5:4"}),
}
CLASSIC = "classic"  # played by the speaker's own synthesizer: speakers download nothing for it


def render_synth(pack: str, slot: str) -> bytes:
    _, _, tone, beat, level, slots = SYNTH_PACKS[pack]
    return _to_pcm(_sequence(_melody(slots[slot], beat), tone), level)


# clips from elsewhere

def trim(pcm: bytes, threshold: float = 0.01) -> bytes:
    """Cut silence at both ends (keeping 30 ms), cap the length and normalize to a comfortable level."""
    x = np.frombuffer(pcm[: len(pcm) // 2 * 2], dtype="<i2").astype(np.float64) / 32768
    loud = np.nonzero(np.abs(x) > threshold)[0]
    if not len(loud):
        return b""
    pad = int(0.03 * RATE)
    x = x[max(0, loud[0] - pad): loud[-1] + pad]
    return _to_pcm(x, 0.6)


def decode_upload(data: bytes) -> bytes:
    """Any audio file PyAV can read -> 16 kHz mono PCM, trimmed and normalized."""
    pcm = trim(decode_audio(data))
    if not pcm:
        raise ValueError("that clip is silent")
    return pcm


def decode_audio(data: bytes, max_s: float = 30) -> bytes:
    """Any audio file PyAV can read (WAV, MP3, OGG, a browser's WebM recording...) -> 16 kHz mono PCM."""
    import av

    try:
        with av.open(io.BytesIO(data)) as f:
            stream = next(s for s in f.streams if s.type == "audio")
            resampler = av.AudioResampler(format="s16", layout="mono", rate=RATE)
            out = bytearray()
            for frame in f.decode(stream):
                for r in resampler.resample(frame):
                    out += bytes(r.planes[0])[: r.samples * 2]
                if len(out) > RATE * 2 * max_s:
                    break
            for r in resampler.resample(None):
                out += bytes(r.planes[0])[: r.samples * 2]
    except (StopIteration, av.FFmpegError, ValueError) as e:
        raise ValueError("couldn't read that as audio") from e
    return bytes(out)


def wav(pcm: bytes) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm)
    return buf.getvalue()


# the store

class SoundStore:
    """Packs in <data>/sounds/<id>/: pack.json and one <slot>.pcm per slot (a missing file is a silent slot).
    Built-in packs aren't stored; their clips are rendered on demand and cached."""

    def __init__(self, root: Path, cache: Path) -> None:
        self.root = root
        self.cache = cache
        self._lock = threading.Lock()

    # packs

    def _meta_path(self, pid: str) -> Path:
        return self.root / pid / "pack.json"

    def get(self, pid: str) -> dict[str, Any] | None:
        if pid in SYNTH_PACKS:
            name, description, *_ = SYNTH_PACKS[pid]
            return {"id": pid, "name": name, "description": description, "kind": "builtin",
                    "version": SYNTH_VERSION, "slots": {s: True for s in SLOTS}}
        if not PACK_ID.match(pid):
            return None
        p = self._meta_path(pid)
        return json.loads(p.read_text()) if p.is_file() else None

    def list(self) -> list[dict[str, Any]]:
        packs = [self.get(pid) for pid in SYNTH_PACKS]
        if self.root.is_dir():
            stored = [self.get(d.name) for d in sorted(self.root.iterdir()) if d.is_dir()]
            packs += sorted((p for p in stored if p), key=lambda p: p["name"].lower())
        return packs

    def _write_meta(self, meta: dict[str, Any]) -> None:
        p = self._meta_path(meta["id"])
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(meta, indent=2))
        tmp.replace(p)

    def _new_id(self, name: str) -> str:
        base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:24] or "pack"
        if not PACK_ID.match(base):
            base = "pack-" + base
        pid, k = base, 2
        while pid in SYNTH_PACKS or (self.root / pid).exists():
            pid, k = f"{base}-{k}", k + 1
        return pid

    def create(self, name: str, kind: str, *, voice: str = "", lines: dict[str, str] | None = None,
               copy_from: str = "") -> dict[str, Any]:
        with self._lock:
            pid = self._new_id(name)
            meta = {"id": pid, "name": name, "kind": kind, "version": 1, "created_at": time.time(),
                    "slots": {s: False for s in SLOTS}}
            if kind == "voiced":
                meta.update(voice=voice, lines={s: (lines or {}).get(s, "") for s in SLOTS})
            self._write_meta(meta)
        if copy_from:
            source = self.get(copy_from)
            if source is None:
                self.delete(pid)
                raise KeyError(copy_from)
            for slot in SLOTS:
                pcm = self.clip(copy_from, slot)
                if pcm:
                    self.set_clip(pid, slot, pcm)
        return self.get(pid)

    def update(self, pid: str, **fields: Any) -> dict[str, Any]:
        with self._lock:
            meta = self.get(pid)
            if meta is None or meta["kind"] == "builtin":
                raise KeyError(pid)
            meta.update({k: v for k, v in fields.items() if v is not None})
            meta["version"] += 1
            self._write_meta(meta)
            return meta

    def delete(self, pid: str) -> bool:
        if pid in SYNTH_PACKS or not PACK_ID.match(pid) or not (self.root / pid).is_dir():
            return False
        shutil.rmtree(self.root / pid, ignore_errors=True)
        shutil.rmtree(self.cache / pid, ignore_errors=True)
        return True

    # clips

    def clip(self, pid: str, slot: str) -> bytes | None:
        """The slot's PCM; b"" for a silent slot; None if there's no such pack or slot."""
        if slot not in SLOTS:
            return None
        if pid in SYNTH_PACKS:
            cached = self.cache / pid / f"{slot}.{SYNTH_VERSION}.pcm"
            if not cached.is_file():
                cached.parent.mkdir(parents=True, exist_ok=True)
                tmp = cached.with_suffix(f".{secrets.token_hex(4)}.tmp")
                tmp.write_bytes(render_synth(pid, slot))
                tmp.replace(cached)
            return cached.read_bytes()
        meta = self.get(pid)
        if meta is None:
            return None
        p = self.root / pid / f"{slot}.pcm"
        return p.read_bytes() if p.is_file() else b""

    def set_clip(self, pid: str, slot: str, pcm: bytes) -> dict[str, Any]:
        """Store a slot's clip (b"" = silent)."""
        if slot not in SLOTS:
            raise ValueError(f"unknown slot '{slot}'")
        meta = self.get(pid)
        if meta is None or meta["kind"] == "builtin":
            raise KeyError(pid)
        p = self.root / pid / f"{slot}.pcm"
        if pcm:
            p.write_bytes(pcm[: int(MAX_CLIP_S * RATE) * 2])
        else:
            p.unlink(missing_ok=True)
        slots = dict(meta["slots"], **{slot: bool(pcm)})
        return self.update(pid, slots=slots)

    def speaker_manifest(self, pid: str, base_url: str) -> dict[str, Any] | None:
        """What a speaker needs for a pack: None for classic (its own chimes), else each slot's URL and size, with
        an empty URL for a silent slot."""
        meta = self.get(pid)
        if meta is None or pid == CLASSIC:
            return None
        slots = {}
        for slot in SLOTS:
            pcm = self.clip(pid, slot) or b""
            slots[slot] = {"url": f"{base_url}/assets/sounds/{pid}/{slot}.pcm?v={meta['version']}" if pcm else "",
                           "bytes": len(pcm)}
        return {"pack": pid, "version": meta["version"], "slots": slots}
