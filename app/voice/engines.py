"""Speech-to-text and text-to-speech engines.

Built in: faster-whisper and Piper, run inside Kotodama on the CPU. Their models are downloaded into the data
directory the first time they're used. Either can instead be a Wyoming service (wyoming.py), e.g. existing
wyoming-faster-whisper / wyoming-piper containers.

Heavy libraries are imported lazily so the server starts (and tests run) without loading them.
"""
from __future__ import annotations

import logging
import os
import re
import threading
import urllib.request
from pathlib import Path

import numpy as np

from . import wyoming

log = logging.getLogger("kotodama.voice")

RATE = 16000  # the speakers' sample rate, both ways

WHISPER_MODELS = ["tiny.en", "base.en", "small.en", "distil-small.en", "distil-medium.en", "medium.en",
                  "tiny", "base", "small", "medium", "large-v3", "distil-large-v3"]

# English Piper voices offered in the admin UI (any valid voice name can be entered). Each voice's licence is in its
# MODEL_CARD at https://huggingface.co/rhasspy/piper-voices.
PIPER_VOICES = ["en_US-lessac-medium", "en_US-amy-medium", "en_US-kristin-medium", "en_US-hfc_female-medium",
                "en_US-ljspeech-high", "en_US-ryan-high", "en_US-joe-medium", "en_US-norman-medium",
                "en_GB-alba-medium", "en_GB-jenny_dioco-medium", "en_GB-cori-high", "en_GB-alan-medium",
                "en_GB-northern_english_male-medium"]
PIPER_VOICE_RE = re.compile(r"^([a-z]{2,3})_([A-Z]{2})-([a-z0-9_]+)-(x_low|low|medium|high)$")
PIPER_BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0"


def to_float(pcm: bytes) -> np.ndarray:
    return np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0


def resample(pcm: bytes, rate: int, target: int = RATE) -> bytes:
    """16-bit mono PCM from `rate` to `target`: windowed-sinc low-pass, then linear interpolation."""
    if rate == target or not pcm:
        return pcm
    x = to_float(pcm)
    if target < rate:  # remove what the lower rate can't hold, or it folds back as noise
        cutoff = 0.45 * target / rate
        n = np.arange(-32, 33)
        taps = 2 * cutoff * np.sinc(2 * cutoff * n) * np.hamming(len(n))
        x = np.convolve(x, taps / taps.sum(), mode="same")
    t = np.arange(0, len(x) * target / rate) * rate / target
    y = np.interp(t, np.arange(len(x)), x)
    return (np.clip(y, -1, 1) * 32767).astype("<i2").tobytes()


class SpeechToText:
    """Built-in faster-whisper, one model loaded at a time."""

    def __init__(self, models_dir: Path) -> None:
        self.models_dir = models_dir
        self._lock = threading.Lock()
        self._model = None
        self._name = ""

    def _load(self, name: str):
        if self._model is None or self._name != name:
            from faster_whisper import WhisperModel
            log.info("loading Whisper model %s (downloaded on first use)", name)
            threads = int(os.environ.get("KOTODAMA_WHISPER_THREADS", "0")) or min(8, os.cpu_count() or 4)
            self._model = WhisperModel(name, device="cpu", compute_type="int8", cpu_threads=threads,
                                       download_root=str(self.models_dir / "whisper"))
            self._name = name
        return self._model

    def transcribe(self, pcm: bytes, *, engine: str, model: str, address: str, language: str) -> str:
        if engine == "wyoming":
            return wyoming.transcribe(address, pcm, RATE, language).strip()
        with self._lock:
            whisper = self._load(model)
            lang = language if language and not model.endswith(".en") else None
            segments, _ = whisper.transcribe(to_float(pcm), language=lang, beam_size=1, vad_filter=True,
                                             condition_on_previous_text=False)
            return " ".join(s.text.strip() for s in segments).strip()


class TextToSpeech:
    """Built-in Piper; voices are cached once loaded."""

    def __init__(self, models_dir: Path) -> None:
        self.dir = models_dir / "piper"
        self._lock = threading.Lock()
        self._voices: dict[str, object] = {}

    def _download(self, voice: str) -> Path:
        m = PIPER_VOICE_RE.match(voice)
        if not m:
            raise ValueError(f"'{voice}' isn't a Piper voice name like en_US-lessac-medium")
        lang, region, name, quality = m.groups()
        model = self.dir / f"{voice}.onnx"
        if not model.exists() or not model.with_suffix(".onnx.json").exists():
            self.dir.mkdir(parents=True, exist_ok=True)
            base = f"{PIPER_BASE}/{lang}/{lang}_{region}/{name}/{quality}/{voice}"
            log.info("downloading Piper voice %s", voice)
            for suffix in (".onnx.json", ".onnx"):
                tmp = self.dir / f"{voice}{suffix}.part"
                urllib.request.urlretrieve(base + suffix, tmp)
                tmp.replace(self.dir / f"{voice}{suffix}")
        return model

    def _load(self, voice: str):
        if voice not in self._voices:
            from piper import PiperVoice
            self._voices[voice] = PiperVoice.load(str(self._download(voice)))
        return self._voices[voice]

    def synthesize(self, text: str, *, engine: str, voice: str, address: str) -> bytes:
        """16-bit mono PCM at RATE."""
        if engine == "wyoming":
            pcm, rate = wyoming.synthesize(address, text, voice)
            return resample(pcm, rate)
        with self._lock:
            piper = self._load(voice)
            if hasattr(piper, "synthesize_stream_raw"):  # piper-tts 1.2
                pcm, rate = b"".join(piper.synthesize_stream_raw(text)), piper.config.sample_rate
            else:  # piper-tts 1.3+
                chunks = list(piper.synthesize(text))
                pcm = b"".join(c.audio_int16_bytes for c in chunks)
                rate = chunks[0].sample_rate if chunks else RATE
        return resample(pcm, rate)
