"""Wake words, detected on the speaker itself: nothing leaves it until it hears one.

The models are microWakeWord streaming models (TensorFlow Lite Micro) from github.com/esphome/micro-wake-word-models,
Apache-2.0, pinned to a commit. The Docker image bundles them (`python -m app.wakewords <dir>`); elsewhere they're
downloaded into the data folder the first time they're needed. A speaker gets its model and settings in its config
("wake_word", docs/PROTOCOL.md) and keeps the model in flash.

    python -m app.wakewords <dir>      # download the models into <dir> (used by the Dockerfile)
"""
from __future__ import annotations

import json
import logging
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


class WakeWords:
    def __init__(self, bundled: Path | None, data_dir: Path) -> None:
        self.dirs = [d for d in (bundled, data_dir) if d]
        self.data_dir = data_dir
        self._lock = threading.Lock()

    def path(self, wid: str) -> Path | None:
        """The model file, downloading it into the data folder if it isn't bundled."""
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
        if wid not in WAKE_WORDS:
            return None
        try:
            model = self.path(wid)
            micro = json.loads(model.with_suffix(".json").read_text())["micro"]
        except Exception as e:  # offline and not bundled: the speaker just has no wake word for now
            log.warning("wake word %s unavailable: %s", wid, e)
            return None
        cutoff = min(0.995, max(0.5, float(micro["probability_cutoff"]) + SENSITIVITY.get(sensitivity, 0.0)))
        return {"id": wid, "name": WAKE_WORDS[wid], "url": f"{base_url}/assets/wakewords/{wid}.tflite?v={MODELS_COMMIT[:8]}",
                "bytes": model.stat().st_size, "cutoff": round(cutoff, 3),
                "window": int(micro.get("sliding_window_size", 5)), "arena": int(micro.get("tensor_arena_size", 30000))}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    target = Path(sys.argv[1])
    for wid in WAKE_WORDS:
        download(target, wid)
    print(f"{len(WAKE_WORDS)} wake word models in {target}")
