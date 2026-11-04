"""The media library: music albums, soundscapes and meditations kept on the server, ready to go on the speakers'
SD cards (docs/MEDIA.md).

It lives in its own folder, HARKENN_MEDIA_DIR (default <data>/media), laid out exactly like a card:

    Music/<album>/NN Title.mp3     Soundscapes/Title.wav     Meditations/Title.mp3

so a folder can be copied straight onto a card. `.harkenn.json` beside them keeps each file's title, credit, licence
and source, and which came from the sample pack.

Uploads are made card-friendly: music and meditations become mono MP3s at an even loudness; soundscapes become mono
32 kHz WAVs that loop without a seam (the end crossfades into the start), since MP3 would leave a gap at the join.

The sample media (github.com/harkenn-project/samplemedia, its `pack` branch at a fixed commit) fills a new library
once: at startup, if the library has no marker file (.sample-media), it's downloaded, every file checked against the
pack's manifest, and only then is the marker written. So it's filled exactly once, never on later starts or updates
(deleted samples stay deleted); if a download fails, the next start tries again. HARKENN_SAMPLE_MEDIA=false turns
it off, and the Media page can add the samples again on request.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import re
import shutil
import threading
import urllib.parse
import wave
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

if TYPE_CHECKING:
    from .context import Context

log = logging.getLogger("harkenn.library")

PACK_BASE = "https://raw.githubusercontent.com/harkenn-project/samplemedia"
PACK_COMMIT = "5e2ad265f42686783781069a64030a6f6a51fb51"
FOLDERS = {"music": "Music", "soundscape": "Soundscapes", "meditation": "Meditations"}
AUDIO = {".mp3", ".wav"}
META = ".harkenn.json"
MARKER = ".sample-media"  # written once the sample media has been added (see above)
MAX_NAME = 80
MUSIC_RATE = 44100
LOOP_RATE = 32000
LOOP_FADE_S = 6.0
LOOP_MAX_S = 6 * 60


def clean_name(name: str) -> str:
    """A safe file or folder name: no path separators, nothing Windows or FAT can't hold, no leading dots."""
    name = re.sub(r'[\x00-\x1f<>:"/\\|?*]', "", str(name or "")).strip().strip(".").strip()
    return re.sub(r"\s+", " ", name)[:MAX_NAME]


def title_of(filename: str) -> str:
    """"03 Clair de lune.mp3" -> "Clair de lune": what's shown and said (as the speaker does it)."""
    return re.sub(r"^\d+[\s.\-]+", "", Path(filename).stem)


# Making uploads card-friendly (the same steps as the samplemedia tools)

def decode(data: bytes, rate: int) -> np.ndarray:
    """Any audio file -> mono float samples at `rate`."""
    import av

    chunks = []
    with av.open(io.BytesIO(data)) as container:
        if not container.streams.audio:
            raise ValueError("that file has no sound in it")
        resampler = av.AudioResampler(format="fltp", layout="mono", rate=rate)
        for packet in container.demux(audio=0):
            try:
                frames = packet.decode()
            except av.error.FFmpegError:  # a damaged packet: skip it rather than the file
                continue
            chunks += [f.to_ndarray()[0] for frame in frames for f in resampler.resample(frame)]
        chunks += [f.to_ndarray()[0] for f in resampler.resample(None)]
    if not chunks:
        raise ValueError("couldn't read any sound from that file")
    x = np.concatenate(chunks).astype(np.float64)
    if len(x) < rate:
        raise ValueError("that's less than a second long")
    return x


def _even_loudness(x: np.ndarray, rate: int, dbfs: float) -> np.ndarray:
    block = rate // 10
    rms = np.sqrt(np.mean(x[: len(x) // block * block].reshape(-1, block) ** 2, axis=1))
    x = x * (10 ** (dbfs / 20) / max(float(np.percentile(rms, 90)), 1e-6))
    return np.tanh(x / 0.95) * 0.95


def master_track(x: np.ndarray) -> np.ndarray:
    """Music and meditations: silence trimmed from the ends, louder passages at about -16 dBFS, short fades."""
    block = MUSIC_RATE // 10
    rms = np.sqrt(np.mean(x[: len(x) // block * block].reshape(-1, block) ** 2, axis=1))
    sound = np.nonzero(rms > 10 ** (-55 / 20))[0]
    if len(sound):
        x = x[max(0, sound[0] - 2) * block: (sound[-1] + 3) * block]
    x = _even_loudness(x, MUSIC_RATE, -16)
    fade_in, fade_out = int(0.05 * MUSIC_RATE), min(int(1.0 * MUSIC_RATE), len(x) // 4)
    x[:fade_in] *= np.linspace(0, 1, fade_in)
    x[-fade_out:] *= np.linspace(1, 0, fade_out)
    return x


def make_loop(x: np.ndarray, crossfade: bool = True) -> np.ndarray:
    """Soundscapes: handling noise trimmed, DC and rumble removed, the end crossfaded into the start so it loops with
    no seam, at a gentle, even loudness."""
    trim = min(int(0.5 * LOOP_RATE), len(x) // 8)
    x = x[trim:len(x) - trim]
    spectrum = np.fft.rfft(x - x.mean())
    f = np.fft.rfftfreq(len(x), 1 / LOOP_RATE)
    x = np.fft.irfft(spectrum / np.sqrt(1 + (20 / np.maximum(f, 1e-3)) ** 8), len(x))
    x = x[: int(LOOP_MAX_S * LOOP_RATE)]
    if crossfade:
        fade = min(int(LOOP_FADE_S * LOOP_RATE), len(x) // 4)
        t = np.linspace(0, np.pi / 2, fade)
        body = x[: len(x) - fade].copy()
        body[:fade] = x[:fade] * np.sin(t) + x[len(x) - fade:] * np.cos(t)
        x = body
    return _even_loudness(x, LOOP_RATE, -20)


def mp3_bytes(x: np.ndarray, tags: dict[str, str]) -> bytes:
    import av

    out = io.BytesIO()
    pcm = (np.clip(x, -1, 1) * 32767).astype(np.int16)
    with av.open(out, "w", format="mp3") as container:
        container.metadata.update({k: v for k, v in tags.items() if v})
        stream = container.add_stream("libmp3lame", rate=MUSIC_RATE, layout="mono")
        stream.bit_rate = 128000
        for i in range(0, len(pcm), 1152):
            frame = av.AudioFrame.from_ndarray(pcm[i:i + 1152].reshape(1, -1), format="s16", layout="mono")
            frame.sample_rate = MUSIC_RATE
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    return out.getvalue()


def wav_bytes(x: np.ndarray, rate: int) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes((np.clip(x, -1, 1) * 32767).astype(np.int16).tobytes())
    return out.getvalue()


class Library:
    def __init__(self, root: Path, ctx: Context | None = None) -> None:
        self.root = root
        self.ctx = ctx
        self._lock = threading.RLock()
        self.pack: dict[str, Any] = {"state": "idle"}
        self._pack_task: asyncio.Task | None = None
        self.transport: Any = None  # tests: an httpx transport standing in for GitHub
        for folder in FOLDERS.values():
            (root / folder).mkdir(parents=True, exist_ok=True)

    # metadata

    def _meta(self) -> dict[str, Any]:
        try:
            return json.loads((self.root / META).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"files": {}}

    def _save_meta(self, meta: dict[str, Any]) -> None:
        tmp = self.root / (META + ".tmp")
        tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.root / META)

    def _changed(self) -> None:
        if self.ctx:
            self.ctx.hub.publish({"type": "media"})

    # paths

    def resolve(self, rel: str) -> Path:
        """A path inside the library, or ValueError (no escaping it)."""
        p = (self.root / rel).resolve()
        if self.root.resolve() not in p.parents or not p.name:
            raise ValueError("not in the media library")
        return p

    def _album_dir(self, album: str) -> Path:
        name = clean_name(album)
        if not name:
            raise ValueError("give the album a name")
        return self.root / FOLDERS["music"] / name

    # reading

    def _file(self, path: Path, meta: dict[str, Any]) -> dict[str, Any]:
        rel = path.relative_to(self.root).as_posix()
        m = meta["files"].get(rel, {})
        return {"path": rel, "title": m.get("title") or title_of(path.name), "size": path.stat().st_size,
                "credit": m.get("credit", ""), "licence": m.get("licence", ""), "source": m.get("source", ""),
                "sample": bool(m.get("sample"))}

    def tree(self) -> dict[str, Any]:
        with self._lock:
            meta = self._meta()
            music = []
            for d in sorted((self.root / FOLDERS["music"]).iterdir(), key=lambda p: p.name.lower()):
                if d.is_dir() and not d.name.startswith("."):
                    tracks = [self._file(p, meta) for p in sorted(d.iterdir())
                              if p.is_file() and p.suffix.lower() in AUDIO and not p.name.startswith(".")]
                    music.append({"name": d.name, "tracks": tracks, "size": sum(t["size"] for t in tracks)})

            def files(kind: str) -> list[dict[str, Any]]:
                folder = self.root / FOLDERS[kind]
                return [self._file(p, meta) for p in sorted(folder.iterdir(), key=lambda p: p.name.lower())
                        if p.is_file() and p.suffix.lower() in AUDIO and not p.name.startswith(".")]

            out = {"music": music, "soundscapes": files("soundscape"), "meditations": files("meditation")}
            out["total_bytes"] = (sum(a["size"] for a in music) + sum(f["size"] for f in out["soundscapes"])
                                  + sum(f["size"] for f in out["meditations"]))
            out["pack"] = self.pack | {"added": (self.root / MARKER).exists()}
            return out

    def is_empty(self) -> bool:
        t = self.tree()
        return not (t["music"] or t["soundscapes"] or t["meditations"])

    # albums

    def create_album(self, name: str) -> str:
        d = self._album_dir(name)
        if d.exists():
            raise ValueError(f"there's already an album called {d.name}")
        d.mkdir(parents=True)
        self._changed()
        return d.name

    def rename_album(self, old: str, new: str) -> str:
        with self._lock:
            src, dst = self._album_dir(old), self._album_dir(new)
            if not src.is_dir():
                raise ValueError(f"no album called {old}")
            if dst.exists() and dst != src:
                raise ValueError(f"there's already an album called {dst.name}")
            src.rename(dst)
            meta = self._meta()
            prefix_old, prefix_new = f"{FOLDERS['music']}/{src.name}/", f"{FOLDERS['music']}/{dst.name}/"
            meta["files"] = {(prefix_new + k[len(prefix_old):] if k.startswith(prefix_old) else k): v
                             for k, v in meta["files"].items()}
            self._save_meta(meta)
        self._changed()
        return dst.name

    def delete_album(self, name: str) -> None:
        with self._lock:
            d = self._album_dir(name)
            if not d.is_dir():
                raise ValueError(f"no album called {name}")
            shutil.rmtree(d)
            meta = self._meta()
            prefix = f"{FOLDERS['music']}/{d.name}/"
            meta["files"] = {k: v for k, v in meta["files"].items() if not k.startswith(prefix)}
            self._save_meta(meta)
        self._changed()

    def reorder(self, album: str, paths: list[str]) -> None:
        """Puts an album's tracks in this order: their names get new numbers (01, 02...)."""
        with self._lock:
            d = self._album_dir(album)
            current = {p.relative_to(self.root).as_posix(): p for p in d.iterdir() if p.suffix.lower() in AUDIO}
            if sorted(paths) != sorted(current):
                raise ValueError("the order must list every track in the album once")
            meta = self._meta()
            staged = []
            for i, rel in enumerate(paths, 1):  # two steps, so names can't collide midway
                tmp = current[rel].with_name(f".reorder-{i}{current[rel].suffix}")
                current[rel].rename(tmp)
                staged.append((rel, tmp, f"{i:02d} {title_of(current[rel].name)}{current[rel].suffix}"))
            for rel, tmp, name in staged:
                tmp.rename(d / name)
                if rel in meta["files"]:
                    meta["files"][(d / name).relative_to(self.root).as_posix()] = meta["files"].pop(rel)
            self._save_meta(meta)
        self._changed()

    # files

    def _target(self, kind: str, album: str, title: str, suffix: str) -> Path:
        title = clean_name(title)
        if not title:
            raise ValueError("give it a name")
        if kind == "music":
            d = self._album_dir(album)
            if not d.is_dir():
                raise ValueError(f"no album called {album}")
            n = 1 + max((int(m.group(1)) for p in d.iterdir() if (m := re.match(r"^(\d+)", p.name))), default=0)
            return d / f"{n:02d} {title}{suffix}"
        if kind not in FOLDERS:
            raise ValueError("kind must be music, soundscape or meditation")
        target = self.root / FOLDERS[kind] / f"{title}{suffix}"
        if any(p.stem.lower() == title.lower() for p in target.parent.iterdir()):
            raise ValueError(f"there's already one called {title}")
        return target

    def add(self, kind: str, album: str, filename: str, data: bytes, title: str = "", credit: str = "",
            licence: str = "", source: str = "", loop: bool = True) -> dict[str, Any]:
        """Converts an upload and adds it. Slow (decoding and encoding): call it in a thread."""
        title = clean_name(title) or clean_name(title_of(filename)) or "Untitled"
        if kind == "soundscape":
            out, suffix = wav_bytes(make_loop(decode(data, LOOP_RATE), crossfade=loop), LOOP_RATE), ".wav"
        else:
            tags = {"title": title, "album": album if kind == "music" else "", "artist": credit, "comment": licence}
            out, suffix = mp3_bytes(master_track(decode(data, MUSIC_RATE)), tags), ".mp3"
        with self._lock:
            target = self._target(kind, album, title, suffix)
            tmp = target.with_name("." + target.name + ".tmp")
            tmp.write_bytes(out)
            tmp.replace(target)
            meta = self._meta()
            meta["files"][target.relative_to(self.root).as_posix()] = {
                "title": title, "credit": clean_text(credit), "licence": clean_text(licence),
                "source": clean_text(source)}
            self._save_meta(meta)
            result = self._file(target, meta)
        self._changed()
        return result

    def update(self, rel: str, title: str | None = None, credit: str | None = None, licence: str | None = None,
               source: str | None = None) -> dict[str, Any]:
        with self._lock:
            path = self.resolve(rel)
            if not path.is_file():
                raise ValueError("no such file")
            meta = self._meta()
            m = meta["files"].pop(rel, {})
            if title is not None and clean_name(title) and clean_name(title) != title_of(path.name):
                number = re.match(r"^(\d+[\s.\-]+)", path.name)
                new = path.with_name((number.group(1) if number else "") + clean_name(title) + path.suffix)
                if new.exists():
                    raise ValueError(f"there's already one called {clean_name(title)}")
                path.rename(new)
                path, rel = new, new.relative_to(self.root).as_posix()
                m["title"] = clean_name(title)
            for key, value in (("credit", credit), ("licence", licence), ("source", source)):
                if value is not None:
                    m[key] = clean_text(value)
            meta["files"][rel] = m
            self._save_meta(meta)
            result = self._file(path, meta)
        self._changed()
        return result

    def delete(self, rel: str) -> None:
        with self._lock:
            path = self.resolve(rel)
            if not path.is_file():
                raise ValueError("no such file")
            path.unlink()
            meta = self._meta()
            meta["files"].pop(rel, None)
            self._save_meta(meta)
        self._changed()

    # the sample pack

    def needs_samples(self) -> bool:
        """True until the sample media has been added once (the marker file)."""
        return not (self.root / MARKER).exists()

    def start_pack(self) -> dict[str, Any]:
        if self._pack_task and not self._pack_task.done():
            return self.pack
        self.pack = {"state": "downloading", "done": 0, "total": 0, "bytes": 0, "total_bytes": 0}
        self._pack_task = asyncio.create_task(self._install_pack())
        return self.pack

    async def _install_pack(self) -> None:
        import httpx

        base = f"{PACK_BASE}/{PACK_COMMIT}/"
        try:
            async with httpx.AsyncClient(timeout=60, follow_redirects=True, transport=self.transport) as client:
                r = await client.get(base + "manifest.json")
                r.raise_for_status()
                files = r.json()["files"]
                self.pack.update(total=len(files), total_bytes=sum(f["size"] for f in files))
                self._changed()
                for f in files:
                    rel = clean_rel(f["path"])
                    target = self.resolve(rel)
                    if not (target.is_file() and target.stat().st_size == f["size"]
                            and await asyncio.to_thread(_sha256, target) == f["sha256"]):
                        r = await client.get(base + urllib.parse.quote(f["path"]))
                        r.raise_for_status()
                        if len(r.content) != f["size"] or hashlib.sha256(r.content).hexdigest() != f["sha256"]:
                            raise ValueError(f"{f['path']} didn't match the pack's checksum")
                        target.parent.mkdir(parents=True, exist_ok=True)
                        tmp = target.with_name("." + target.name + ".tmp")
                        tmp.write_bytes(r.content)
                        tmp.replace(target)
                    with self._lock:
                        meta = self._meta()
                        meta["files"][rel] = {"title": f.get("title") or title_of(target.name),
                                              "credit": f.get("credit", ""), "licence": f.get("licence", ""),
                                              "source": f.get("source", ""), "sample": True}
                        self._save_meta(meta)
                    self.pack["done"] += 1
                    self.pack["bytes"] += f["size"]
                    self._changed()
            (self.root / MARKER).write_text(json.dumps({"commit": PACK_COMMIT, "files": len(files)}) + "\n",
                                            encoding="utf-8")
            self.pack["state"] = "done"
            log.info("sample media added: %d files", self.pack["total"])
        except Exception as e:
            log.warning("sample media download failed: %s", e)
            self.pack.update(state="error", error=str(e)[:300])
        self._changed()


def clean_text(value: str | None) -> str:
    return re.sub(r"[\x00-\x1f]", " ", str(value or "")).strip()[:300]


def clean_rel(rel: str) -> str:
    parts = [clean_name(p) for p in str(rel).split("/")]
    if not parts or not all(parts) or parts[0] not in FOLDERS.values():
        raise ValueError(f"unexpected path in the pack: {rel}")
    return "/".join(parts)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()

