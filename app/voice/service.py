"""The voice pipeline: a speaker streams what it hears, Harkenn turns it into a spoken reply.

    speaker: voice_start {session, local_time}, binary PCM frames (16 kHz, 16-bit mono), voice_end
    server:  listen_stop {session} once the speaker has heard speech and then silence (vad.py)
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

from .. import broadcast, media, screens, timers, weather
from ..models import DeviceConfig, VoiceSettings
from . import brain, vad, voiceprint
from .calls import Rings
from .engines import RATE, SpeechToText, TextToSpeech

if TYPE_CHECKING:
    from ..context import Context

log = logging.getLogger("harkenn.voice")

SETTINGS_KEY = "voice"
MAX_UTTERANCE_BYTES = RATE * 2 * 30  # 30 s, the speakers' limit too
MIN_UTTERANCE_BYTES = RATE * 2 // 4  # under a quarter second is a click, not speech
CHUNK_BYTES = RATE * 2 // 10         # 100 ms per frame sent to the speaker
BURST_S = 3.0                        # how far ahead of playback the speaker's buffer is kept
FOLLOW_UP_WINDOW_S = 120.0           # a reply must start within this long after the answer was sent
WAIT_PING_S = 20.0                   # while an answer takes long, tell the speaker to keep waiting this often
REPLY_ACK = "Thanks, I'll pass that on."
SCREEN_CHUNK_BYTES = 4096            # a screen's picture goes to the speaker in frames of this size


@dataclass
class Utterance:
    session: str
    follow_up: bool = False  # a reply to the last answer, in the same conversation
    started: float = field(default_factory=time.time)
    local_time: datetime | None = None
    audio: bytearray = field(default_factory=bytearray)
    speech_end: vad.SpeechEnd | None = None


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
        self._background: set[asyncio.Task] = set()
        self.speech_end = vad.SpeechEnd  # (silence_s) -> detector; replaced in tests
        self.rings = Rings(ctx.hub)
        self.voiceprints = voiceprint.Voiceprints(models, ctx.db)
        self._samples: dict[str, asyncio.Future] = {}  # speaker -> an enrollment waiting for its next recording
        self._replies: dict[str, asyncio.Future] = {}  # speaker -> someone waiting for what is said next (await_reply)
        self._awaiting_reply: dict[str, float] = {}  # speaker -> when it was asked to listen for a reply (follow_up)
        self._last_spoken: tuple[str, float] = ("", 0.0)  # (speaker, when): the last one someone talked to
        self._binary: dict[str, asyncio.Lock] = {}  # speaker -> held while sending speech or a screen in binary frames
        self._broadcast_next: dict[str, tuple[str, float]] = {}  # speaker -> (where to, when) after "Broadcast" alone

    # settings

    def settings(self) -> VoiceSettings:
        raw = self.ctx.db.get_setting(SETTINGS_KEY)
        try:
            return VoiceSettings.model_validate(raw) if raw else VoiceSettings()
        except ValidationError as e:
            log.warning("stored voice settings are invalid, using defaults: %s", e)
            return VoiceSettings()

    def effective(self) -> VoiceSettings:
        """The settings in use: the admin UI's, with the assistant filled in from HARKENN_ASSISTANT_* where empty."""
        s, env = self.settings(), self.ctx.settings
        if not s.assistant_url and env.assistant_url:
            s.assistant_url = env.assistant_url
            if env.assistant_api in ("openai", "anthropic", "hermes"):
                s.assistant_api = env.assistant_api
            s.assistant_model = s.assistant_model or env.assistant_model
        if not s.assistant_key and env.assistant_key:
            s.assistant_key = env.assistant_key
        return s

    def save_settings(self, s: VoiceSettings) -> None:
        self.ctx.db.set_setting(SETTINGS_KEY, s.model_dump())

    # messages from a speaker

    async def handle(self, device_id: str, msg: dict[str, Any]) -> None:
        kind = msg.get("type")
        if kind == "voice_start":
            self._cancel(device_id)
            # A reply to the last answer continues its conversation. Firmware 0.11 says so; older firmware can't
            # listen by itself after answering, so it's always a new conversation there.
            asked = self._awaiting_reply.pop(device_id, 0)
            follow_up = bool(msg.get("follow_up")) and time.time() - asked < FOLLOW_UP_WINDOW_S
            if not follow_up:
                self.conversations.start(device_id)
            self._utterances[device_id] = Utterance(session=str(msg.get("session", ""))[:32], follow_up=follow_up,
                                                    local_time=parse_local_time(msg.get("local_time")),
                                                    speech_end=self._speech_end(device_id))
            self.ctx.hub.set_activity(device_id, "listening")
        elif kind == "voice_end":
            u = self._utterances.pop(device_id, None)
            if u is not None:
                self.ctx.hub.set_activity(device_id, "thinking")
                self._start(device_id, self._respond(device_id, u))
        elif kind in ("voice_cancel", "speak_stop"):
            self._utterances.pop(device_id, None)
            self._cancel(device_id)
            self.ctx.hub.set_activity(device_id, "idle")
            if kind == "voice_cancel":
                self._deliver_reply(device_id, "")
        elif kind == "ring_answer":
            self.rings.answered(device_id, str(msg.get("ring", "")))
        elif kind == "screen_tap":
            self._utterances.pop(device_id, None)
            label = self.ctx.screens.tapped(device_id, str(msg.get("screen", "")), str(msg.get("label", "")))
            session = str(msg.get("session", ""))[:32]
            if label is None:  # an old screen, or a label that isn't on it
                await self.ctx.hub.send(device_id, {"type": "voice_result", "session": session, "heard": "",
                                                    "reply": ""})
                return
            self.ctx.hub.set_activity(device_id, "thinking")
            self._start(device_id, self._respond_tap(device_id, session, label))
        elif kind == "screen_dismiss":
            self._log(device_id, "I", "screen dismissed")

    def audio(self, device_id: str, data: bytes) -> None:
        u = self._utterances.get(device_id)
        if u is None or len(u.audio) + len(data) > MAX_UTTERANCE_BYTES:
            return
        u.audio += data
        if u.speech_end is None:
            return
        try:
            ended = u.speech_end.feed(data)
        except Exception as e:  # never lose the utterance over this: the speaker's own detector still ends it
            log.warning("end-of-speech detection failed for %s: %s", device_id, e)
            u.speech_end = None
            return
        if ended:  # the speaker stops listening and sends voice_end, as if its own detector had ended it
            task = asyncio.create_task(self.ctx.hub.send(device_id, {"type": "listen_stop", "session": u.session}))
            self._background.add(task)
            task.add_done_callback(self._background.discard)

    def disconnected(self, device_id: str) -> None:
        self._utterances.pop(device_id, None)
        self._cancel(device_id)
        self.ctx.hub.set_activity(device_id, "idle")
        self._deliver_reply(device_id, "")

    # replies to a question someone else asked through a speaker (an agent, via mcp_server.py)

    def expect_reply(self, device_id: str) -> asyncio.Future:
        """The next thing heard on this speaker goes to the returned future instead of the assistant."""
        self._deliver_reply(device_id, "")
        f: asyncio.Future = asyncio.get_running_loop().create_future()
        self._replies[device_id] = f
        return f

    def expect_sample(self, device_id: str) -> asyncio.Future:
        """The next recording on this speaker goes to the returned future (voiceprint enrollment), as raw PCM."""
        old = self._samples.pop(device_id, None)
        if old and not old.done():
            old.set_result(b"")
        f: asyncio.Future = asyncio.get_running_loop().create_future()
        self._samples[device_id] = f
        return f

    async def wait_reply(self, f: asyncio.Future, device_id: str, timeout: float) -> str | None:
        """What was said ("" = nothing understood), or None if the speaker never started listening in time."""
        try:
            return await asyncio.wait_for(asyncio.shield(f), timeout)
        except asyncio.TimeoutError:
            if self._replies.get(device_id) is f:
                self._replies.pop(device_id)
            return None

    def _deliver_reply(self, device_id: str, heard: str) -> bool:
        f = self._replies.pop(device_id, None)
        if f is None or f.done():
            return False
        f.set_result(heard)
        return True

    def _speech_end(self, device_id: str) -> vad.SpeechEnd | None:
        device = self.ctx.db.get_device(device_id) or {}
        try:
            silence_s = DeviceConfig.model_validate(device.get("config") or {}).audio.silence_s
            return self.speech_end(silence_s)
        except Exception as e:
            log.warning("end-of-speech detection unavailable, the speaker decides: %s", e)
            return None

    # pipeline

    def _start(self, device_id: str, coro) -> None:
        self._cancel(device_id)
        task = asyncio.create_task(coro)
        self._tasks[device_id] = task

        def done(t: asyncio.Task) -> None:
            if self._tasks.get(device_id) is t:
                self._tasks.pop(device_id, None)
                if self.ctx.hub.activity(device_id) == "thinking":  # it ended without anything to say
                    self.ctx.hub.set_activity(device_id, "idle")
        task.add_done_callback(done)

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
        from ..context import trim_recordings  # context imports this module
        rid = secrets.token_hex(8)
        rel = f"{device_id}/{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}_{rid}.wav"
        path = self.ctx.settings.recordings_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        data = wav_bytes(pcm)
        path.write_bytes(data)
        self.ctx.db.add_recording({"id": rid, "device_id": device_id, "created_at": time.time(), "kind": "utterance",
                                   "filename": rel, "content_type": "audio/wav", "size": len(data)})
        trim_recordings(self.ctx, device_id)
        self.ctx.hub.publish({"type": "recording", "id": device_id, "recording_id": rid})

    async def _respond(self, device_id: str, u: Utterance) -> None:
        s = self.effective()
        send = self.ctx.hub.send
        pcm = bytes(u.audio)
        try:
            if s.keep_recordings and pcm:
                await run_in_threadpool(self._keep, device_id, pcm)
            sample = self._samples.pop(device_id, None)
            if sample is not None and not sample.done():  # a voiceprint recording, not a question
                sample.set_result(pcm)
                await send(device_id, {"type": "voice_result", "session": u.session, "heard": "", "reply": "Thanks!"})
                await self._stream(device_id, await self.synthesize("Thanks!"), "Thanks!", u.session)
                return
            t0 = time.perf_counter()
            heard, who = "", None
            if len(pcm) >= MIN_UTTERANCE_BYTES:
                # who is talking, at the same time as what they said
                transcribe = run_in_threadpool(self.stt.transcribe, pcm, engine=s.stt_engine, model=s.stt_model,
                                               address=s.stt_address, language=s.language)
                heard, who = await asyncio.gather(transcribe, self._identify(pcm, s.voiceprint_threshold))
            t1 = time.perf_counter()
            if self._deliver_reply(device_id, heard):  # an answer to someone else's question: pass it on
                self._log(device_id, "I", f'heard reply "{heard}" ({t1 - t0:.1f} s)')
                await send(device_id, {"type": "voice_result", "session": u.session, "heard": heard,
                                       "reply": REPLY_ACK if heard else ""})
                if heard:
                    await self._stream(device_id, await run_in_threadpool(
                        self.tts.synthesize, REPLY_ACK, engine=s.tts_engine, voice=s.tts_voice,
                        address=s.tts_address), REPLY_ACK, u.session)
                return
            if not heard:
                self._log(device_id, "I", f"heard nothing in {len(pcm) / (RATE * 2):.1f} s of audio")
                await send(device_id, {"type": "voice_result", "session": u.session, "heard": "", "reply": ""})
                return
            if who:
                self._log(device_id, "I", f"voice: {voiceprint.tag(who)[7:-1]} ({who['score']:.2f})")
            self._last_spoken = (device_id, time.time())
            if await self._broadcast(device_id, u.session, heard, pcm, s, who):
                return
            if await self._media(device_id, u.session, heard):
                return
            await self._answer(device_id, u.session, heard, s, who=who, local_time=u.local_time,
                               heard_note=f"({t1 - t0:.1f} s)")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("voice pipeline failed for %s", device_id)
            self._log(device_id, "E", f"voice pipeline failed: {e}")
            await send(device_id, {"type": "voice_error", "session": u.session, "message": str(e)[:200]})

    async def _respond_tap(self, device_id: str, session: str, label: str) -> None:
        """Someone tapped a labelled part of the screen: an answer to an agent's question, or the person's next turn
        in the conversation."""
        self._awaiting_reply.pop(device_id, None)
        send = self.ctx.hub.send
        text = f'(tapped "{label}" on the screen)'
        try:
            if self._deliver_reply(device_id, text):
                self._log(device_id, "I", f'screen: tapped "{label}" (passed on)')
                await send(device_id, {"type": "voice_result", "session": session, "heard": label,
                                       "reply": REPLY_ACK})
                await self._stream(device_id, await self.synthesize(REPLY_ACK), REPLY_ACK, session)
                return
            await self._answer(device_id, session, text, self.effective())
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("screen tap failed for %s", device_id)
            self._log(device_id, "E", f"screen tap failed: {e}")
            await send(device_id, {"type": "voice_error", "session": session, "message": str(e)[:200]})

    async def _answer(self, device_id: str, session: str, heard: str, s: VoiceSettings, who: dict | None = None,
                      local_time: datetime | None = None, heard_note: str = "") -> None:
        """Ask the assistant, then say its answer (and show its screen) on the speaker."""
        send = self.ctx.hub.send
        t1 = time.perf_counter()
        reply = await self._keep_waiting(device_id, session, self._ask(device_id, heard, s, who, local_time))
        t2 = time.perf_counter()
        # Draw the screen while the speech is synthesized; it goes out before the speech, so it shows as the answer
        # starts. With only a screen (no words), the speaker learns it's done from the empty reply in voice_result.
        speech, _ = await asyncio.gather(
            run_in_threadpool(self.tts.synthesize, reply, engine=s.tts_engine, voice=s.tts_voice,
                              address=s.tts_address) if reply else asyncio.sleep(0, b""),
            self.show_screen(device_id, reply.screen) if reply.screen else asyncio.sleep(0))
        t3 = time.perf_counter()
        await send(device_id, {"type": "voice_result", "session": session, "heard": heard, "reply": reply})
        self._log(device_id, "I", f'heard "{heard}" {heard_note}, replied "{reply}"'
                                  f'{" + screen" if reply.screen else ""} ({t2 - t1:.1f} s, speech {t3 - t2:.1f} s)')
        if not reply:
            return
        listen = (s.follow_up == "always" or (s.follow_up == "assistant" and reply.listen)
                  or (s.follow_up == "questions" and reply.rstrip().endswith("?")))
        if listen:
            self._awaiting_reply[device_id] = time.time()
        await self._stream(device_id, speech, reply, session, listen=listen, follow_up=listen)

    async def _ask(self, device_id: str, heard: str, s: VoiceSettings, who: dict | None = None,
                   local_time: datetime | None = None) -> brain.Reply:
        """The assistant's reply, with its timers and household markers carried out."""
        device = self.ctx.db.get_device(device_id) or {}
        character = self.ctx.characters.get((device.get("config") or {}).get("character", "default")) or {}
        speaker = device.get("name", "speaker")
        person = self.conversations.person(device_id, who["id"] if who and who.get("sure") else "")
        weather_now = await run_in_threadpool(self.ctx.weather.summary)  # cached: at most a fetch every 15 min
        # a chat model gets the household modules; an agent (Hermes) has its own, and Harkenn's through MCP
        household = self.ctx.household if s.assistant_api != "hermes" else None
        notes = {"weather": weather_now.strip(), **self.ctx.board.notes_for(device_id)}
        if household:
            notes |= await run_in_threadpool(household.notes, device_id, person)
        reply = await run_in_threadpool(
            brain.ask, heard, device_id=device_id, speaker=speaker,
            character=character.get("name", "Kotodama"), now=local_time or datetime.now(),
            settings=s.model_dump(), conversations=self.conversations,
            context=brain.request_context(speaker, device_id, voiceprint.describe(who)), timer_prompt=timers.PROMPT,
            notes=notes, memory_key=f"harkenn-person-{person}" if person else "",
            screen_prompt=self.ctx.screens.prompt() + weather.PROMPT if s.screen else "",
            voice_tag=voiceprint.tag(who), household=household, person=person)
        if reply.screen_error:
            self._log(device_id, "W", f"screen: {reply.screen_error}")
        for problem in reply.timer_problems:
            self._log(device_id, "W", f"timer: {problem}")
        for action in reply.timers:
            await self.timer_action(device_id, action)
        for problem in reply.household_problems:
            self._log(device_id, "W", f"household: {problem}")
        for module, body in reply.household:
            try:
                done = await self.ctx.household.apply(module, body, device_id, person)
                if done:
                    self._log(device_id, "I", f"{module}: {done}")
            except ValueError as e:
                self._log(device_id, "W", f"{module}: {e}")
        return reply

    async def proactive(self, device_id: str, prompt: str) -> None:
        """Have the assistant say something unasked (the briefing after a morning alarm)."""
        if not self.ctx.hub.online(device_id):
            return
        try:
            self.conversations.start(device_id)
            reply = await self._ask(device_id, prompt, self.effective())
            if reply:
                self._log(device_id, "I", f'said by itself: "{reply}"')
                await self.say(device_id, str(reply))
        except Exception as e:
            log.exception("proactive reply failed for %s", device_id)
            self._log(device_id, "E", f"couldn't say it: {e}")

    async def _broadcast(self, device_id: str, session: str, heard: str, pcm: bytes, s: VoiceSettings,
                         who: dict | None) -> bool:
        """A broadcast (broadcast.py), caught before the assistant: sent in the person's own voice. False if `heard`
        isn't one."""
        pending = self._broadcast_next.pop(device_id, None)
        if pending and time.time() - pending[1] < FOLLOW_UP_WINDOW_S:  # the message, after "Broadcast" alone
            to, text, clip = pending[0], heard[:1].upper() + heard[1:], pcm
        else:
            req = broadcast.parse(heard, self.ctx.db)
            if not req:
                return False
            if not req["message"]:
                self._broadcast_next[device_id] = (req["to"], time.time())
                ask = "What's the message?"
                self._awaiting_reply[device_id] = time.time()
                await self.ctx.hub.send(device_id, {"type": "voice_result", "session": session, "heard": heard,
                                                    "reply": ask})
                await self._stream(device_id, await self.synthesize(ask), ask, session, listen=True, follow_up=True)
                return True
            to, text, clip = req["to"], req["message"], pcm
            if s.stt_engine == "builtin":  # from where the message starts, not "broadcast that"
                try:
                    words = await run_in_threadpool(self.stt.words, pcm, model=s.stt_model, language=s.language)
                    span = broadcast.message_span(words, req["skip"], text)
                    if span:
                        clip = pcm[int(span[0] * RATE) * 2:int(span[1] * RATE) * 2]
                except Exception as e:  # the whole recording, then
                    log.warning("broadcast: no word timings: %s", e)
        device = self.ctx.db.get_device(device_id) or {}
        sender = (who or {}).get("name") if who and who.get("sure") else device.get("name", "")
        try:
            sent = await broadcast.send(self.ctx, text=text, pcm=clip, to=to, sender=sender, exclude=device_id)
            reply = broadcast.sent_reply(sent["heard_on"])
            self._log(device_id, "I", f'broadcast "{text}" to {broadcast.to_name(self.ctx.db, to)}: '
                                      f'{", ".join(sent["heard_on"]) or "no one online"}')
        except ValueError as e:
            reply = f"Sorry, {e}."
            self._log(device_id, "W", f"broadcast: {e}")
        await self.ctx.hub.send(device_id, {"type": "voice_result", "session": session, "heard": heard,
                                            "reply": reply})
        await self._stream(device_id, await self.synthesize(reply), reply, session)
        return True

    def recent_speaker(self, within_s: float = 300) -> str:
        """The speaker someone last spoke to, if it was in the last `within_s` seconds: what an agent's "this
        speaker" means (mcp_server.py)."""
        device_id, at = self._last_spoken
        return device_id if device_id and time.time() - at < within_s else ""

    async def _media(self, device_id: str, session: str, heard: str) -> bool:
        """"Play rain", "pause", "next"... (media.py), caught before the assistant. False if `heard` isn't one."""
        library = await asyncio.to_thread(self.ctx.cardsync.items)
        names = {"music": [], "soundscapes": [], "meditations": []}
        for i in library.values():
            if i["kind"] == "music":
                names["music"].append({"name": i["name"]})
            else:
                names[i["kind"] + "s"].append(i["name"])
        req = media.parse(heard, self.ctx.cards.get(device_id), self.ctx.players.get(device_id), names)
        if not req:
            return False
        action, reply = req.pop("action"), req.pop("reply")
        if action:
            try:
                await media.send(self.ctx, device_id, action, **req)
                self._log(device_id, "I", f'media: {action} {" ".join(f"{k}={v}" for k, v in req.items() if v)}'
                                          f' ("{heard}")')
            except ValueError as e:
                reply = f"Sorry, {e}."
                self._log(device_id, "W", f"media: {e}")
        await self.ctx.hub.send(device_id, {"type": "voice_result", "session": session, "heard": heard,
                                            "reply": reply})
        if reply:  # the music waits (faded out) until this has been said
            await self._stream(device_id, await self.synthesize(reply), reply, session)
        return True

    async def timer_action(self, device_id: str, action: dict[str, Any]) -> str:
        """Carry out a timer marker or tool call on the speaker (or the one it names). Returns a problem, or ""."""
        target = device_id
        if action.get("speaker"):
            want = str(action["speaker"]).strip().lower()
            found = [d for d in self.ctx.db.list_devices() if want in (d["id"], d["name"].lower())]
            if not found:
                problem = f'no speaker called "{action["speaker"]}"'
                self._log(device_id, "W", f"timer: {problem}")
                return problem
            target = found[0]["id"]
        msg, problem = timers.message(action)
        if msg is None:
            self._log(device_id, "W", f"timer: {problem}")
            return problem
        if not await self.ctx.hub.send(target, msg):
            return "that speaker is offline"
        return ""

    async def show_screen(self, device_id: str, spec: dict[str, Any]) -> list[str]:
        """Draw a screen and send it to the speaker. Returns the problems found (skipped overlays, ...)."""
        from ..context import device_theme
        device = self.ctx.db.get_device(device_id)
        if not device:
            return ["unknown speaker"]
        try:
            screen, warnings = await run_in_threadpool(self.ctx.screens.render, spec, device_theme(device))
        except screens.ScreenError as e:
            self._log(device_id, "W", f"screen: {e}")
            return [str(e)]
        for w in warnings:
            self._log(device_id, "W", f"screen: {w}")
        self.ctx.screens.add(device_id, screen)
        base = self.ctx.hub.base(device_id) or self.ctx.public_url()
        async with self._binary_lock(device_id):  # the picture's frames mustn't mix with speech
            if await self.ctx.hub.send(device_id, screen.message(base)):
                for i in range(0, len(screen.kani), SCREEN_CHUNK_BYTES):
                    if not await self.ctx.hub.send_bytes(device_id, screen.kani[i:i + SCREEN_CHUNK_BYTES]):
                        break
        return warnings

    def _binary_lock(self, device_id: str) -> asyncio.Lock:
        return self._binary.setdefault(device_id, asyncio.Lock())

    async def _identify(self, pcm: bytes, threshold: float) -> dict[str, Any] | None:
        if not voiceprint.available() or not any(p["samples"] for p in self.voiceprints.people()):
            return None
        try:
            return await run_in_threadpool(self.voiceprints.identify, pcm, threshold)
        except Exception as e:  # never lose the question over this
            log.warning("voice recognition failed: %s", e)
            return None

    async def _keep_waiting(self, device_id: str, session: str, work) -> Any:
        """Await `work`, telling the speaker every WAIT_PING_S that the answer is still coming (agents can take a
        while), so it doesn't give up."""
        task = asyncio.ensure_future(work)
        try:
            while True:
                done, _ = await asyncio.wait({task}, timeout=WAIT_PING_S)
                if done:
                    return task.result()
                await self.ctx.hub.send(device_id, {"type": "voice_wait", "session": session})
        finally:
            task.cancel()

    async def _stream(self, device_id: str, pcm: bytes, text: str, session: str = "", listen: bool = False,
                      follow_up: bool = False) -> None:
        async with self._binary_lock(device_id):
            await self._stream_locked(device_id, pcm, text, session, listen, follow_up)

    async def _stream_locked(self, device_id: str, pcm: bytes, text: str, session: str, listen: bool,
                             follow_up: bool) -> None:
        hub = self.ctx.hub
        start = {"type": "speak_start", "session": session, "rate": RATE, "bytes": len(pcm), "text": text}
        if listen:
            start["listen"] = True  # the speaker starts listening when it has finished speaking
        if follow_up:
            start["follow_up"] = True  # ...for a reply in the same conversation (a quiet cue, not the wake sound)
        if not await hub.send(device_id, start):
            return
        hub.set_activity(device_id, "speaking")
        sent_at = time.monotonic()
        for i in range(0, len(pcm), CHUNK_BYTES):
            if not await hub.send_bytes(device_id, pcm[i:i + CHUNK_BYTES]):
                return
            ahead = (i + CHUNK_BYTES) / (RATE * 2) - (time.monotonic() - sent_at)
            if ahead > BURST_S:  # the speaker's buffer is finite: stay at most BURST_S ahead of playback
                await asyncio.sleep(ahead - BURST_S)
        await hub.send(device_id, {"type": "speak_end", "session": session})
        # the speaker is still playing what's in its buffer; it's idle after that (or listening again, if asked)
        playing = len(pcm) / (RATE * 2) - (time.monotonic() - sent_at)
        task = asyncio.create_task(self._idle_after(device_id, max(0.0, playing), listen))
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _idle_after(self, device_id: str, seconds: float, listen: bool) -> None:
        await asyncio.sleep(seconds)
        if self.ctx.hub.activity(device_id) == "speaking" and not listen:
            self.ctx.hub.set_activity(device_id, "idle")

    def warm_up(self) -> None:
        """Download and load the configured models now, so the first question isn't slow. Runs in a thread."""
        s = self.settings()
        try:
            vad.load()
            if voiceprint.available():
                self.voiceprints.warm_up()
            if s.tts_engine == "builtin":
                self.tts.synthesize("Ready.", engine="builtin", voice=s.tts_voice, address="")
            if s.stt_engine == "builtin":
                self.stt.transcribe(b"\0\0" * RATE, engine="builtin", model=s.stt_model, address="",
                                    language=s.language)
            log.info("voice models ready")
        except Exception as e:  # not fatal: the first request will try again and report the error
            log.warning("couldn't prepare the voice models: %s", e)

    async def synthesize(self, text: str, voice: str = "") -> bytes:
        s = self.settings()
        return await run_in_threadpool(self.tts.synthesize, text, engine=s.tts_engine, voice=voice or s.tts_voice,
                                       address=s.tts_address)

    async def say(self, device_id: str, text: str, voice: str = "", listen: bool = False,
                  pcm: bytes | None = None) -> None:
        """Speak `text` on a speaker (admin UI: test a voice; agents: announcements, calls). With `listen`, the
        speaker listens afterwards (pair with expect_reply)."""
        pcm = pcm if pcm is not None else await self.synthesize(text, voice)
        self._start(device_id, self._stream(device_id, pcm, text, listen=listen))
