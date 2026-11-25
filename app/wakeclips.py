"""Wake clips: the few seconds a speaker heard up to each wake word detection, kept as recordings of kind "wake".

They show what set the wake word off, so false wakes can be found and fed back into training the model. A speaker
sends them only when its config says so (`wake_word.clips`, on with *Keep what speakers hear*), once it's idle again:

    wake_clip {rate, bytes, probability}   then `bytes` of 16-bit mono PCM in binary frames

Wake word tests (the admin's *Test wake word*, command `wake_test`) come the same way, always kept, as kind
"wake_test": a few seconds heard after a cue, `probability` the highest score in them, and `detected` whether it would
have woken the speaker. `wake_test {error}` instead says why a test couldn't run. Each result is published to the admin
pages as a `wake_test` event.

While a clip is arriving, binary frames from that speaker are the clip's, not microphone audio for an utterance.
"""
from __future__ import annotations

import logging
import secrets
import time
import wave
from dataclasses import dataclass, field
from io import BytesIO
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .context import Context

log = logging.getLogger(__name__)

KIND = "wake"
TEST_KIND = "wake_test"
KINDS = (KIND, TEST_KIND)  # kept under the wake clip limit
MAX_CLIP_S = 10


@dataclass
class _Pending:
    rate: int
    expected: int
    probability: float | None
    test: bool = False
    detected: bool = False
    started: float = field(default_factory=time.time)
    data: bytearray = field(default_factory=bytearray)


def _wav(pcm: bytes, rate: int) -> bytes:
    buf = BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


class WakeClips:
    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx
        self._pending: dict[str, _Pending] = {}

    def start(self, device_id: str, msg: dict[str, Any]) -> None:
        """A `wake_clip` or `wake_test` message: its audio follows in binary frames."""
        test = msg.get("type") == TEST_KIND
        if test and msg.get("error"):
            self.ctx.hub.publish({"type": "wake_test", "id": device_id, "error": str(msg["error"])[:200]})
            return
        try:
            rate = int(msg.get("rate", 16000))
            size = int(msg.get("bytes", 0))
            p = msg.get("probability")
            probability = None if p is None else min(1.0, max(0.0, float(p)))
        except (TypeError, ValueError):
            log.warning("bad wake_clip from %s", device_id)
            return
        if not 8000 <= rate <= 48000 or size <= 0 or size % 2 or size > rate * 2 * MAX_CLIP_S:
            log.warning("wake_clip from %s refused: %s bytes at %s Hz", device_id, size, rate)
            return
        self._pending[device_id] = _Pending(rate, size, probability, test, bool(msg.get("detected")))
        # (an unfinished earlier one is dropped)

    def feed(self, device_id: str, data: bytes) -> bool:
        """A binary frame: True if it was (part of) a wake clip."""
        p = self._pending.get(device_id)
        if p is None:
            return False
        p.data += data[: p.expected - len(p.data)]
        if len(p.data) >= p.expected:
            del self._pending[device_id]
            self._save(device_id, p)
        return True

    def interrupted(self, device_id: str) -> None:
        """Any other message: a speaker sends a clip's frames back to back, so an unfinished one is broken. Drop it,
        so the frames that follow (an utterance's audio) go where they belong."""
        if self._pending.pop(device_id, None) is not None:
            log.warning("unfinished wake clip from %s dropped", device_id)

    def disconnected(self, device_id: str) -> None:
        self._pending.pop(device_id, None)

    def _save(self, device_id: str, p: _Pending) -> None:
        if not p.test and not self.ctx.voice.effective().keep_recordings:  # switched off since the speaker was told
            return
        from .context import trim_recordings  # context imports this module
        rid = secrets.token_hex(8)
        when = time.strftime("%Y%m%d-%H%M%S", time.gmtime(p.started))
        score = "" if p.probability is None else f"_p{round(p.probability * 100):03d}"
        # tests: test_<when>_pNNN_heard|missed_<id>.wav, so a downloaded file says how it went
        name = f"test_{when}{score}_{'heard' if p.detected else 'missed'}" if p.test else f"{KIND}_{when}{score}"
        rel = f"{device_id}/{name}_{rid}.wav"
        path = self.ctx.settings.recordings_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        data = _wav(bytes(p.data), p.rate)
        path.write_bytes(data)
        self.ctx.db.add_recording({"id": rid, "device_id": device_id, "created_at": p.started, "kind": TEST_KIND if p.test else KIND,
                                   "filename": rel, "content_type": "audio/wav", "size": len(data)})
        trim_recordings(self.ctx, device_id, wake=True)
        self.ctx.hub.publish({"type": "recording", "id": device_id, "recording_id": rid})
        if p.test:
            self.ctx.hub.publish({"type": "wake_test", "id": device_id, "recording_id": rid, "detected": p.detected,
                                  "probability": p.probability})
