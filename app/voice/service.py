"""The voice pipeline: a speaker streams what it hears, Kotodama turns it into a spoken reply.

    speaker: voice_start {session, local_time}, binary PCM frames (16 kHz, 16-bit mono), voice_end
    server:  speech-to-text -> reply (brain.py) -> text-to-speech
    server:  voice_result {heard, reply}, speak_start {rate, text, bytes}, binary PCM frames, speak_end

See docs/PROTOCOL.md. One utterance per speaker at a time; a new one replaces the old.
"""
from __future__ import annotations

import asyncio
import logging
import secrets
import time
import wave
from dataclasses import dataclass, field
from datetime import datetime
from io import BytesIO
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from ..models import VoiceSettings
from . import brain
from .engines import RATE, SpeechToText, TextToSpeech

if TYPE_CHECKING:
    from ..context import Context

log = logging.getLogger("kotodama.voice")

SETTINGS_KEY = "voice"
MAX_UTTERANCE_BYTES = RATE * 2 * 30  # 30 s, the speakers' limit too
MIN_UTTERANCE_BYTES = RATE * 2 // 4  # under a quarter second is a click, not speech
CHUNK_BYTES = RATE * 2 // 10         # 100 ms per frame sent to the speaker
BURST_S = 3.0                        # how far ahead of playback the speaker's buffer is kept


@dataclass
class Utterance:
    session: str
    started: float = field(default_factory=time.time)
    local_time: datetime | None = None
    audio: bytearray = field(default_factory=bytearray)


def parse_local_time(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value)) if value else None
    except ValueError:
        return None


def wav_bytes(pcm: bytes) -> bytes:
    buf = BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm)
    return buf.getvalue()


class VoiceService:
    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx
        models = ctx.settings.data_dir / "models"
        self.stt = SpeechToText(models)
        self.tts = TextToSpeech(models)
        self.conversations = brain.Conversations()
        self._utterances: dict[str, Utterance] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    # settings

    def settings(self) -> VoiceSettings:
        raw = self.ctx.db.get_setting(SETTINGS_KEY)
        try:
            return VoiceSettings.model_validate(raw) if raw else VoiceSettings()
        except ValidationError as e:
            log.warning("stored voice settings are invalid, using defaults: %s", e)
            return VoiceSettings()

    def save_settings(self, s: VoiceSettings) -> None:
        self.ctx.db.set_setting(SETTINGS_KEY, s.model_dump())

    # messages from a speaker

    async def handle(self, device_id: str, msg: dict[str, Any]) -> None:
        kind = msg.get("type")
        if kind == "voice_start":
            self._cancel(device_id)
            self._utterances[device_id] = Utterance(session=str(msg.get("session", ""))[:32],
                                                    local_time=parse_local_time(msg.get("local_time")))
        elif kind == "voice_end":
            u = self._utterances.pop(device_id, None)
            if u is not None:
                self._start(device_id, self._respond(device_id, u))
        elif kind in ("voice_cancel", "speak_stop"):
            self._utterances.pop(device_id, None)
            self._cancel(device_id)

    def audio(self, device_id: str, data: bytes) -> None:
        u = self._utterances.get(device_id)
        if u is not None and len(u.audio) + len(data) <= MAX_UTTERANCE_BYTES:
            u.audio += data

    def disconnected(self, device_id: str) -> None:
        self._utterances.pop(device_id, None)
        self._cancel(device_id)

    # pipeline

    def _start(self, device_id: str, coro) -> None:
        self._cancel(device_id)
        task = asyncio.create_task(coro)
        self._tasks[device_id] = task
        task.add_done_callback(lambda t: self._tasks.pop(device_id, None) if self._tasks.get(device_id) is t else None)

    def _cancel(self, device_id: str) -> None:
        task = self._tasks.pop(device_id, None)
        if task:
            task.cancel()

    def _log(self, device_id: str, level: str, message: str) -> None:
        now = time.time()
        self.ctx.db.add_logs(device_id, [(now, level, "voice", message)], self.ctx.settings.log_retention_per_device)
        self.ctx.hub.publish({"type": "log", "id": device_id,
                              "lines": [{"ts": now, "level": level, "tag": "voice", "message": message}]})

    def _keep(self, device_id: str, pcm: bytes) -> None:
        rid = secrets.token_hex(8)
        rel = f"{device_id}/{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}_{rid}.wav"
        path = self.ctx.settings.recordings_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        data = wav_bytes(pcm)
        path.write_bytes(data)
        self.ctx.db.add_recording({"id": rid, "device_id": device_id, "created_at": time.time(), "kind": "utterance",
                                   "filename": rel, "content_type": "audio/wav", "size": len(data)})
        self.ctx.hub.publish({"type": "recording", "id": device_id, "recording_id": rid})

    async def _respond(self, device_id: str, u: Utterance) -> None:
        s = self.settings()
        send = self.ctx.hub.send
        pcm = bytes(u.audio)
        try:
            if s.keep_recordings and pcm:
                await run_in_threadpool(self._keep, device_id, pcm)
            t0 = time.perf_counter()
            heard = ""
            if len(pcm) >= MIN_UTTERANCE_BYTES:
                heard = await run_in_threadpool(self.stt.transcribe, pcm, engine=s.stt_engine, model=s.stt_model,
                                                address=s.stt_address, language=s.language)
            t1 = time.perf_counter()
            if not heard:
                self._log(device_id, "I", f"heard nothing in {len(pcm) / (RATE * 2):.1f} s of audio")
                await send(device_id, {"type": "voice_result", "session": u.session, "heard": "", "reply": ""})
                return
            device = self.ctx.db.get_device(device_id) or {}
            character = self.ctx.characters.get((device.get("config") or {}).get("character", "default")) or {}
            reply = await run_in_threadpool(
                brain.ask, heard, device_id=device_id, speaker=device.get("name", "speaker"),
                character=character.get("name", "Kotodama"), now=u.local_time or datetime.now(),
                settings=s.model_dump(), conversations=self.conversations)
            t2 = time.perf_counter()
            await send(device_id, {"type": "voice_result", "session": u.session, "heard": heard, "reply": reply})
            speech = await run_in_threadpool(self.tts.synthesize, reply, engine=s.tts_engine, voice=s.tts_voice,
                                             address=s.tts_address)
            t3 = time.perf_counter()
            self._log(device_id, "I", f'heard "{heard}" ({t1 - t0:.1f} s), replied "{reply}" '
                                      f"({t2 - t1:.1f} s, speech {t3 - t2:.1f} s)")
            await self._stream(device_id, speech, reply, u.session)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("voice pipeline failed for %s", device_id)
            self._log(device_id, "E", f"voice pipeline failed: {e}")
            await send(device_id, {"type": "voice_error", "session": u.session, "message": str(e)[:200]})

    async def _stream(self, device_id: str, pcm: bytes, text: str, session: str = "") -> None:
        hub = self.ctx.hub
        if not await hub.send(device_id, {"type": "speak_start", "session": session, "rate": RATE,
                                          "bytes": len(pcm), "text": text}):
            return
        sent_at = time.monotonic()
        for i in range(0, len(pcm), CHUNK_BYTES):
            if not await hub.send_bytes(device_id, pcm[i:i + CHUNK_BYTES]):
                return
            ahead = (i + CHUNK_BYTES) / (RATE * 2) - (time.monotonic() - sent_at)
            if ahead > BURST_S:  # the speaker's buffer is finite: stay at most BURST_S ahead of playback
                await asyncio.sleep(ahead - BURST_S)
        await hub.send(device_id, {"type": "speak_end", "session": session})

    def warm_up(self) -> None:
        """Download and load the configured models now, so the first question isn't slow. Runs in a thread."""
        s = self.settings()
        try:
            if s.tts_engine == "builtin":
                self.tts.synthesize("Ready.", engine="builtin", voice=s.tts_voice, address="")
            if s.stt_engine == "builtin":
                self.stt.transcribe(b"\0\0" * RATE, engine="builtin", model=s.stt_model, address="",
                                    language=s.language)
            log.info("voice models ready")
        except Exception as e:  # not fatal: the first request will try again and report the error
            log.warning("couldn't prepare the voice models: %s", e)

    async def say(self, device_id: str, text: str, voice: str = "") -> None:
        """Speak `text` on a speaker (admin UI: test a voice, announcements)."""
        s = self.settings()
        pcm = await run_in_threadpool(self.tts.synthesize, text, engine=s.tts_engine, voice=voice or s.tts_voice,
                                      address=s.tts_address)
        self._start(device_id, self._stream(device_id, pcm, text))
