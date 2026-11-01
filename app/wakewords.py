"""Wake words, detected on the speaker itself: nothing leaves it until it hears one.

The built-in models are microWakeWord streaming models (TensorFlow Lite Micro) from
github.com/esphome/micro-wake-word-models, Apache-2.0, pinned to a commit. The Docker image bundles them
(`python -m app.wakewords <dir>`); elsewhere they're downloaded into the data folder the first time they're needed.
Custom ones (trained with microWakeWord, e.g. "Hi Janet") are uploaded on the admin Voice page and kept in
<data>/wakewords. A speaker gets its model and settings in its config
("wake_word", docs/PROTOCOL.md) and keeps the model in flash.

    python -m app.wakewords <dir>      # download the models into <dir> (used by the Dockerfile)
"""
from __future__ import annotations

import json
import logging
import re
import sys
import threading
import urllib.request
from pathlib import Path
from typing import Any

log = logging.getLogger("harkenn.wakewords")

MODELS_REPO = "https://raw.githubusercontent.com/esphome/micro-wake-word-models"
MODELS_COMMIT = "40ff33f57f8fc6ad71a75ef085abbf742495b225"
WAKE_WORDS = {"okay_nabu": "Okay Nabu", "hey_jarvis": "Hey Jarvis", "hey_mycroft": "Hey Mycroft", "alexa": "Alexa"}
# How much more or less readily a speaker wakes than the model's own cutoff.
SENSITIVITY = {"low": +0.02, "normal": 0.0, "high": -0.05}
MAX_MODEL_BYTES = 512 * 1024


def _fetch(name: str) -> bytes:
    url = f"{MODELS_REPO}/{MODELS_COMMIT}/models/v2/{name}"
    with urllib.request.urlopen(url, timeout=60) as resp:
        data = resp.read(MAX_MODEL_BYTES + 1)
    if len(data) > MAX_MODEL_BYTES:
        raise ValueError(f"{name} is too big")
    return data


def download(directory: Path, wid: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for ext in ("json", "tflite"):
        data = _fetch(f"{wid}.{ext}")
        tmp = directory / f"{wid}.{ext}.part"
        tmp.write_bytes(data)
        tmp.replace(directory / f"{wid}.{ext}")


DEFAULT_MICRO = {"probability_cutoff": 0.97, "sliding_window_size": 5, "tensor_arena_size": 30000}


class WakeWords:
    def __init__(self, bundled: Path | None, data_dir: Path, custom_dir: Path | None = None) -> None:
        self.dirs = [d for d in (bundled, data_dir) if d]
        self.data_dir = data_dir
        self.custom_dir = custom_dir or data_dir.parent.parent / "wakewords"
        self._lock = threading.Lock()

    # custom models

    def custom(self) -> dict[str, dict[str, Any]]:
        out = {}
        if self.custom_dir.is_dir():
            for meta in sorted(self.custom_dir.glob("*.json")):
                if meta.with_suffix(".tflite").is_file():
                    try:
                        out[meta.stem] = json.loads(meta.read_text())
                    except ValueError:
                        continue
        return out

    def catalog(self) -> dict[str, str]:
        """Every wake word a speaker can use: id -> name."""
        return {**WAKE_WORDS, **{wid: m.get("wake_word") or wid for wid, m in self.custom().items()}}

    def add_custom(self, model: bytes, manifest: dict[str, Any] | None, name: str = "") -> str:
        """Store an uploaded model. Raises ValueError if it isn't a TFLite model."""
        if len(model) > MAX_MODEL_BYTES or model[4:8] != b"TFL3":
            raise ValueError("that isn't a TensorFlow Lite model (.tflite)")
        manifest = dict(manifest or {})
        name = (name or manifest.get("wake_word") or "").strip()[:40]
        if not name:
            raise ValueError("give the wake word a name")
        micro = {**DEFAULT_MICRO, **{k: v for k, v in (manifest.get("micro") or {}).items()
                                     if k in ("probability_cutoff", "sliding_window_size", "tensor_arena_size")}}
        wid = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")[:32] or "custom"
        if wid in WAKE_WORDS:
            wid = f"custom_{wid}"
        self.custom_dir.mkdir(parents=True, exist_ok=True)
        (self.custom_dir / f"{wid}.tflite").write_bytes(model)
        (self.custom_dir / f"{wid}.json").write_text(json.dumps({"wake_word": name, "micro": micro}, indent=2))
        return wid

    def delete_custom(self, wid: str) -> bool:
        if wid not in self.custom():
            return False
        for ext in ("tflite", "json"):
            (self.custom_dir / f"{wid}.{ext}").unlink(missing_ok=True)
        return True

    # models

    def path(self, wid: str) -> Path | None:
        """The model file, downloading a built-in one into the data folder if it isn't bundled."""
        if wid in self.custom():
            return self.custom_dir / f"{wid}.tflite"
        if wid not in WAKE_WORDS:
            return None
        for d in self.dirs:
            if (d / f"{wid}.tflite").is_file() and (d / f"{wid}.json").is_file():
                return d / f"{wid}.tflite"
        with self._lock:
            if not (self.data_dir / f"{wid}.tflite").is_file():
                log.info("downloading wake word model %s", wid)
                download(self.data_dir, wid)
        return self.data_dir / f"{wid}.tflite"

    def speaker_manifest(self, wid: str, sensitivity: str, base_url: str) -> dict[str, Any] | None:
        """What a speaker needs for its wake word; None for none (or if the model can't be had)."""
        names = self.catalog()
        if wid not in names:
            return None
        try:
            model = self.path(wid)
            micro = json.loads(model.with_suffix(".json").read_text())["micro"]
        except Exception as e:  # offline and not bundled: the speaker just has no wake word for now
            log.warning("wake word %s unavailable: %s", wid, e)
            return None
        cutoff = min(0.995, max(0.5, float(micro["probability_cutoff"]) + SENSITIVITY.get(sensitivity, 0.0)))
        version = MODELS_COMMIT[:8] if wid in WAKE_WORDS else str(int(model.stat().st_mtime))
        return {"id": wid, "name": names[wid], "url": f"{base_url}/assets/wakewords/{wid}.tflite?v={version}",
                "bytes": model.stat().st_size, "cutoff": round(cutoff, 3),
                "window": int(micro.get("sliding_window_size", 5)), "arena": int(micro.get("tensor_arena_size", 30000))}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    target = Path(sys.argv[1])
    for wid in WAKE_WORDS:
        download(target, wid)
    print(f"{len(WAKE_WORDS)} wake word models in {target}")
