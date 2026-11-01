"""Voiceprints: a hint about who is talking, for the assistant. Opt-in, per person, kept on this server.

People are enrolled on the admin People page by saying a few sentences, on a speaker or in the browser. Each sample
becomes a voiceprint: a 256-number embedding from WeSpeaker's ResNet34 model (trained on VoxCeleb), computed with
sherpa-onnx. No audio is kept. Each utterance's embedding is compared with everyone's (cosine similarity); the best
match counts if it clears the threshold and is clearly ahead of the next person.

It is a hint, never proof: a recording or a similar voice can fool it, and a two-word command is hard to tell apart.
The assistant is told so (brain.py), and must never treat it as permission for anything sensitive.
"""
from __future__ import annotations

import logging
import os
import secrets
import threading
import time
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

from .engines import RATE

if TYPE_CHECKING:
    from ..db import Database

log = logging.getLogger("harkenn.voice")

MODEL_NAME = "wespeaker_en_voxceleb_resnet34_LM.onnx"
MODEL_URL = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/" + MODEL_NAME)
PEOPLE_KEY = "people"
MAX_SAMPLES = 12          # per person; the oldest goes first
MIN_SAMPLE_S = 1.5        # enrollment: enough speech to be useful
MIN_IDENTIFY_S = 0.6      # shorter than this, don't guess
MARGIN = 0.06             # the best match must beat the next person by this much
SURE_ABOVE = 0.10         # this far over the threshold counts as "fairly sure"

# Sentences to read when enrolling: varied sounds, everyday words.
ENROLL_SENTENCES = [
    "The quick brown fox jumps over the lazy dog by the river.",
    "Please turn on the lights in the kitchen and the hallway.",
    "What will the weather be like this weekend in the mountains?",
    "Remind me to buy apples, bread and orange juice tomorrow.",
    "My favourite song is playing on the radio right now.",
    "Could you tell me how long it takes to cook brown rice?",
]


class Voiceprints:
    def __init__(self, models_dir: Path, db: Database) -> None:
        self.dir = models_dir / "voiceprint"
        self.db = db
        self._extractor = None
        self._lock = threading.Lock()

    # the model

    def _load(self):
        with self._lock:
            if self._extractor is None:
                import sherpa_onnx

                path = self.dir / MODEL_NAME
                if not path.is_file():
                    log.info("downloading the voiceprint model (%s)", MODEL_NAME)
                    self.dir.mkdir(parents=True, exist_ok=True)
                    tmp = path.with_suffix(".part")
                    urllib.request.urlretrieve(MODEL_URL, tmp)
                    tmp.replace(path)
                config = sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(path), num_threads=2)
                self._extractor = sherpa_onnx.SpeakerEmbeddingExtractor(config)
            return self._extractor

    def embed(self, pcm: bytes) -> np.ndarray:
        """A unit-length voiceprint for 16 kHz 16-bit mono speech."""
        x = np.frombuffer(pcm[: len(pcm) // 2 * 2], dtype="<i2").astype(np.float32) / 32768
        ex = self._load()
        stream = ex.create_stream()
        stream.accept_waveform(RATE, x)
        stream.input_finished()
        e = np.asarray(ex.compute(stream), dtype=np.float32)
        n = float(np.linalg.norm(e))
        return e / n if n else e

    def warm_up(self) -> None:
        if self.people():
            self._load()

    # people

    def people(self) -> list[dict[str, Any]]:
        return list(self.db.get_setting(PEOPLE_KEY) or [])

    def _save(self, people: list[dict[str, Any]]) -> None:
        self.db.set_setting(PEOPLE_KEY, people)

    def person(self, pid: str) -> dict[str, Any] | None:
        return next((p for p in self.people() if p["id"] == pid), None)

    def add_person(self, name: str, phonetic: str = "", gender: str = "") -> dict[str, Any]:
        p = {"id": secrets.token_hex(4), "name": name, "phonetic": phonetic, "gender": gender,
             "created_at": time.time(), "consent_at": time.time(), "samples": []}
        self._save([*self.people(), p])
        return p

    def update_person(self, pid: str, **fields: str) -> dict[str, Any]:
        """Change a person's name, phonetic spelling or gender identity (only the fields given)."""
        return self._update(pid, lambda p: p.update({k: v for k, v in fields.items() if v is not None}))

    def delete_person(self, pid: str) -> bool:
        people = self.people()
        keep = [p for p in people if p["id"] != pid]
        self._save(keep)
        return len(keep) != len(people)

    def clear_samples(self, pid: str) -> dict[str, Any]:
        return self._update(pid, lambda p: p.update(samples=[]))

    def add_sample(self, pid: str, pcm: bytes) -> dict[str, Any]:
        """Enroll one recording. Raises ValueError if it's too short."""
        if len(pcm) / (RATE * 2) < MIN_SAMPLE_S:
            raise ValueError(f"that was too short: say a whole sentence (at least {MIN_SAMPLE_S:g} seconds)")
        e = [round(float(v), 5) for v in self.embed(pcm)]
        return self._update(pid, lambda p: p.update(samples=[*p["samples"], e][-MAX_SAMPLES:]))

    def _update(self, pid: str, change) -> dict[str, Any]:
        people = self.people()
        p = next((p for p in people if p["id"] == pid), None)
        if p is None:
            raise KeyError(pid)
        change(p)
        self._save(people)
        return p

    # recognition

    def identify(self, pcm: bytes, threshold: float) -> dict[str, Any] | None:
        """Who this probably is: {"id", "name", "score", "sure"}, {"id": None} if no one matched, or None if
        no one is enrolled or there's too little audio to tell."""
        people = [p for p in self.people() if p["samples"]]
        if not people or len(pcm) / (RATE * 2) < MIN_IDENTIFY_S:
            return None
        e = self.embed(pcm)
        scores = []
        for p in people:
            centroid = np.mean(np.asarray(p["samples"], dtype=np.float32), axis=0)
            centroid /= np.linalg.norm(centroid) or 1.0
            scores.append((float(e @ centroid), p))
        scores.sort(key=lambda s: -s[0])
        best, person = scores[0]
        runner_up = scores[1][0] if len(scores) > 1 else -1.0
        if best < threshold:
            return {"id": None, "name": None, "score": round(best, 3), "sure": False}
        if best - runner_up < MARGIN:  # sounds like one of two people, too close to tell which
            return {"id": None, "name": None, "score": round(best, 3), "sure": False,
                    "maybe": [person["name"], scores[1][1]["name"]]}
        return {"id": person["id"], "name": person["name"], "phonetic": person.get("phonetic", ""),
                "gender": person.get("gender", ""), "score": round(best, 3), "sure": best >= threshold + SURE_ABOVE}


# Told to the assistant once, in its instructions; each of the person's lines then carries a tag (tag()).
EXPLAIN = ("Voice recognition: the person's messages start with a tag like [voice: Dana, fairly sure] saying who is "
           "probably speaking, worked out from their voice for that message alone. The tag may also say how to "
           "pronounce their name (write it that way when you say it, since your words are spoken) and their gender "
           "identity (use it when referring to them). Several people may share a "
           "speaker and take turns, so go by each message's own tag, not by who spoke earlier. It's only a hint: it "
           "can be wrong or fooled, so never treat it as proof of identity or as permission for anything sensitive "
           "(locks, alarms, money, private information).")


def tag(who: dict[str, Any] | None) -> str:
    """The tag put in front of what the person said (empty when voiceprints aren't in use)."""
    if who is None:
        return ""
    if who.get("id"):
        return f"[voice: {about(who)}, {'fairly sure' if who['sure'] else 'not certain'}]"
    if who.get("maybe"):
        return f"[voice: {' or '.join(who['maybe'])}, too close to tell]"
    return "[voice: not recognized]"


def about(person: dict[str, Any]) -> str:
    """A person's name with how to say it and their gender identity, when set: Marika (say "Ma-ree-ka"; woman)."""
    details = [f'say "{person["phonetic"]}"'] if person.get("phonetic") else []
    details += [person["gender"]] if person.get("gender") else []
    return person["name"] + (f" ({'; '.join(details)})" if details else "")


def spoken_name(person: dict[str, Any]) -> str:
    """How the speech voice should say the name."""
    return person.get("phonetic") or person["name"]


def describe(who: dict[str, Any] | None) -> str:
    """The note in the assistant's instructions (empty when voiceprints aren't in use)."""
    return EXPLAIN if who is not None else ""


def available() -> bool:
    try:
        import sherpa_onnx  # noqa: F401
    except ImportError:
        return False
    return os.environ.get("HARKENN_VOICEPRINTS", "true") != "false"
