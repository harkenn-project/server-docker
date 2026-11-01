"""End-of-speech detection: decides when someone has finished talking to a speaker.

Runs Silero VAD (the ONNX model bundled with faster-whisper) over the microphone stream as it arrives, one 32 ms
window at a time, keeping the model's state between frames. A speaker's own detector only measures loudness, so a
fan, a TV or a door can keep it listening; Silero tells speech from noise, and the server ends the utterance with
`listen_stop` instead (docs/PROTOCOL.md, section 5).
"""
from __future__ import annotations

import logging
from typing import Callable

import numpy as np

from .engines import RATE

log = logging.getLogger("harkenn.voice")

WINDOW = 512         # samples per Silero window at 16 kHz (32 ms)
CONTEXT = 64         # samples of the previous window the model sees with each one
THRESHOLD = 0.5      # speech probability at or above this is speech
MIN_SPEECH_S = 0.25  # this much speech, in total, before an utterance can end (a cough or click isn't a question)

Probability = Callable[[np.ndarray], float]  # one window (with context) -> speech probability


class Silero:
    """Streaming wrapper around faster-whisper's Silero session: feed windows in order, get a probability each."""

    def __init__(self) -> None:
        from faster_whisper.vad import get_vad_model

        self.session = get_vad_model().session
        self.h = np.zeros((1, 1, 128), dtype="float32")
        self.c = np.zeros((1, 1, 128), dtype="float32")

    def __call__(self, window: np.ndarray) -> float:
        out, self.h, self.c = self.session.run(None, {"input": window[None, :], "h": self.h, "c": self.c})
        return float(np.asarray(out).reshape(-1)[0])


def load() -> None:
    """Load the model now (warm-up), so the first utterance doesn't wait for it."""
    from faster_whisper.vad import get_vad_model

    get_vad_model()


class SpeechEnd:
    """Watches one utterance. feed() returns True once, when speech was heard and then `silence_s` passed without it."""

    def __init__(self, silence_s: float, probability: Probability | None = None) -> None:
        self.silence_windows = max(1, round(silence_s * RATE / WINDOW))
        self.min_speech_windows = round(MIN_SPEECH_S * RATE / WINDOW)
        self.probability = probability or Silero()
        self.pending = np.zeros(0, dtype="float32")
        self.context = np.zeros(CONTEXT, dtype="float32")
        self.speech_windows = 0
        self.quiet_windows = 0
        self.ended = False

    @property
    def heard_speech(self) -> bool:
        return self.speech_windows >= self.min_speech_windows

    def feed(self, pcm: bytes) -> bool:
        if self.ended:
            return False
        samples = np.frombuffer(pcm[: len(pcm) // 2 * 2], dtype="<i2").astype("float32") / 32768.0
        self.pending = np.concatenate([self.pending, samples])
        while len(self.pending) >= WINDOW:
            window, self.pending = self.pending[:WINDOW], self.pending[WINDOW:]
            p = self.probability(np.concatenate([self.context, window]))
            self.context = window[-CONTEXT:]
            if p >= THRESHOLD:
                self.speech_windows += 1
                self.quiet_windows = 0
            else:
                self.quiet_windows += 1
            if self.heard_speech and self.quiet_windows >= self.silence_windows:
                self.ended = True
                return True
        return False
