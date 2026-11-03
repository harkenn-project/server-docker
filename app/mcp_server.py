"""MCP server: lets an AI agent (Hermes, Claude, any MCP client) use the speakers. See docs/MCP.md.

    POST /mcp   Streamable HTTP, stateless, JSON responses. Authorization: Bearer <token>
                (a token made on the admin Agents page, or HARKENN_MCP_TOKEN)

Tools: list_speakers, speak, ring. Harkenn can't tell whether anyone is near a speaker, so everything it says to an
agent assumes no one heard unless someone answered a ring.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import time
from collections import deque
from datetime import datetime
from typing import TYPE_CHECKING, Any

from mcp.server.mcpserver import MCPServer
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from . import __version__, weather
from . import timers as timers_mod
from .models import AgentsConfig
from .voice.calls import MAX_RING_S, MIN_RING_S
from .voice.engines import RATE

if TYPE_CHECKING:
    from .context import Context

log = logging.getLogger("harkenn.mcp")

TOKENS_KEY = "mcp_tokens"
MAX_TEXT = 600
MAX_RINGS_PER_HOUR = 6  # per speaker: ringing is loud, and an agent shouldn't be able to keep it up
REPLY_LISTEN_S = 45  # after the question has been spoken: time to start talking, plus the speaker's own limit

NOT_HEARD = ("Presume no one heard this: Harkenn can't tell whether anyone is near a speaker. If it matters that "
             "someone gets the message, use ring, which tells you whether someone answered.")

INSTRUCTIONS = f"""Harkenn runs the smart speakers in this home. Use it to talk to the people here.

- list_speakers: the speakers, where they are (their names are usually rooms), whether each is online, and what
  you may do there (agents: ring_and_speak, speak_only or do_not_disturb). Respect do not disturb: never try to get
  around it.
- speak: say something aloud on one speaker or all of them. Use it for announcements that don't need a response.
  {NOT_HEARD}
- ring: ring one speaker or all of them for up to {MAX_RING_S} seconds, like a phone. Someone taps a speaker to
  answer. You learn which speaker was answered; then you can have a message spoken there and, if you ask, hear their
  spoken reply. If no one answers, presume no one is there and no one heard anything.
- list_timers / set_timer / set_alarm / cancel_timer: timers and alarms, which live on each speaker and ring there.
- notify: show a notification card on a speaker's screen (a soft chime; an envelope stays until someone looks).
- get_weather: the weather here now and for the coming days (when the place is set in Harkenn).
- show_weather: show an animated weather screen on one speaker or all of them, with the numbers you give; anything
  you leave out comes from the weather here.

Words are spoken aloud by a text-to-speech voice: write short, plain sentences, with no lists, markdown, emoji or
URLs. Don't ring at night or repeatedly unless it's urgent; ringing is loud and interrupts people."""


RING_DESCRIPTION = f"""Ring one speaker (its name or id) or all of them ("all") for {MIN_RING_S}-{MAX_RING_S} seconds,
until someone taps one to answer. Waits until it's answered or time is up.

If answered: `message` (if any) is spoken on the speaker that was answered. With `wait_for_reply`, the speaker then
listens and you get what the person said in `reply` (an empty reply means nothing was understood); if you want a reply
but have no message, they're asked to go ahead.

If no one answers, presume no one is there and no one heard anything."""


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Tokens:
    """Access tokens for the MCP server. Only their hashes are stored; a new token is shown once."""

    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx

    def list(self) -> list[dict[str, Any]]:
        return list(self.ctx.db.get_setting(TOKENS_KEY) or [])

    def create(self, name: str) -> tuple[dict[str, Any], str]:
        token = "hk_" + secrets.token_urlsafe(32)
        entry = {"id": secrets.token_hex(4), "name": name, "hash": _hash(token), "created_at": time.time(),
                 "last_used": None}
        self.ctx.db.set_setting(TOKENS_KEY, [*self.list(), entry])
        return entry, token

    def delete(self, tid: str) -> bool:
        tokens = self.list()
        keep = [t for t in tokens if t["id"] != tid]
        self.ctx.db.set_setting(TOKENS_KEY, keep)
        return len(keep) != len(tokens)

    def check(self, token: str) -> bool:
        if not token:
            return False
        env = self.ctx.settings.mcp_token
        if env and hmac.compare_digest(token, env):
            return True
        digest = _hash(token)
        tokens = self.list()
        for t in tokens:
            if hmac.compare_digest(t["hash"], digest):
                if not t["last_used"] or time.time() - t["last_used"] > 60:
                    t["last_used"] = time.time()
                    self.ctx.db.set_setting(TOKENS_KEY, tokens)
                return True
        return False


def in_quiet_hours(start: str, end: str, now: datetime) -> bool:
    if not start or not end or start == end:
        return False
    t = now.strftime("%H:%M")
    return start <= t < end if start < end else t >= start or t < end  # e.g. 22:00-07:00 spans midnight


def agent_access(config: dict[str, Any], now: datetime) -> str:
    """What agents may do on a speaker now: ring_and_speak, speak_only or do_not_disturb (quiet hours included)."""
    a = AgentsConfig.model_validate(config.get("agents") or {})
    return "do_not_disturb" if in_quiet_hours(a.quiet_from, a.quiet_to, now) else a.access


WEATHER_DESCRIPTION = (
    "Show an animated weather screen (drawn by Harkenn) on one speaker (its name or id) or all of them. Every field is "
    "optional: leave them all out to show the weather here now, or view=\"forecast\" for the coming days. Give fields "
    "to show other weather (another place, a forecast you have); with a different location nothing is taken from the "
    "weather here. condition is one of: " + ", ".join(weather.CONDITIONS) + ". forecast: up to 4 of {label, "
    "condition, high, low, precip_chance}. units: \"metric\" (C, km/h, mm, cm) or \"imperial\" (F, mph, in). It's "
    "shown for `seconds` (5-600), or until someone taps it.")


def _speakers(ctx: Context) -> list[dict[str, Any]]:
    now = datetime.now()
    return [{"id": d["id"], "name": d["name"], "online": ctx.hub.online(d["id"]),
             "state": (d.get("status") or {}).get("state", "unknown"),
             "mic_muted": bool((d.get("status") or {}).get("muted")),
             "agents": agent_access(d.get("config") or {}, now)}
            for d in ctx.db.list_devices()]


DND_NOTE = ("Do not disturb is set for agents on {names}, by the people who live here. Respect it: don't try to reach "
            "them some other way through Harkenn.")


def _targets(ctx: Context, speaker: str) -> tuple[list[dict[str, Any]], str | None]:
    """The speakers `speaker` names (an id, a name, or "all"), or an error."""
    every = _speakers(ctx)
    want = speaker.strip().lower()
    if want in ("", "all", "everywhere", "*"):
        chosen = every
    else:
        chosen = [s for s in every if want in (s["id"].lower(), s["name"].lower())]
        if not chosen:
            names = ", ".join(s["name"] for s in every) or "none"
            return [], f'No speaker called "{speaker}". Speakers: {names}.'
    return chosen, None


def build(ctx: Context) -> tuple[MCPServer, Any]:
    """The MCP server, and the ASGI endpoint to route /mcp to (auth included)."""
    server = MCPServer(name="harkenn", title="Harkenn speakers", version=__version__, instructions=INSTRUCTIONS,
                       website_url="https://github.com/harkenn-project/server-docker")
    voice = ctx.voice
    rings_by_speaker: dict[str, deque] = {}  # speaker -> when agents rang it (MAX_RINGS_PER_HOUR)

    def log_tool(message: str) -> None:
        log.info("mcp: %s", message)
        ctx.hub.publish({"type": "mcp", "message": message})

    @server.tool()
    def list_speakers() -> dict[str, Any]:
        """List the speakers: id, name (usually the room), whether each is online, what it's doing (idle, listening,
        thinking, speaking, sleeping) and whether its microphone is muted. A speaker that's online can still have no
        one near it."""
        return {"speakers": _speakers(ctx)}

    @server.tool()
    async def speak(text: str, speaker: str = "all") -> dict[str, Any]:
        """Say `text` aloud on one speaker (its name or id) or on all of them ("all"). Returns at once; the speech
        plays over the next few seconds. Use short, plain sentences. No one confirms hearing it: presume no one did.
        Speakers busy in a conversation are skipped rather than interrupted."""
        text = " ".join(text.split())[:MAX_TEXT]
        if not text:
            return {"spoken_on": [], "note": "Nothing to say."}
        chosen, error = _targets(ctx, speaker)
        if error:
            return {"spoken_on": [], "note": error}
        spoken, skipped = [], []
        pcm = None
        dnd = [s["name"] for s in chosen if s["agents"] == "do_not_disturb"]
        for s in chosen:
            if s["agents"] == "do_not_disturb":
                skipped.append(f'{s["name"]} (do not disturb)')
            elif not s["online"]:
                skipped.append(f'{s["name"]} (offline)')
            elif s["state"] in ("listening", "thinking"):
                skipped.append(f'{s["name"]} (busy in a conversation)')
            else:
                pcm = pcm if pcm is not None else await voice.synthesize(text)
                await voice.say(s["id"], text, pcm=pcm)
                spoken.append(s["name"])
        log_tool(f'speak on {", ".join(spoken) or "nothing"}: "{text}"')
        note = NOT_HEARD if spoken else "Nothing was spoken."
        if dnd:
            note += " " + DND_NOTE.format(names=", ".join(dnd))
        return {"spoken_on": spoken, "skipped": skipped, "note": note}

    @server.tool(description=RING_DESCRIPTION)
    async def ring(speaker: str = "all", seconds: int = MAX_RING_S, message: str = "",
                   wait_for_reply: bool = False) -> dict[str, Any]:
        chosen, error = _targets(ctx, speaker)
        if error:
            return {"answered": False, "note": error}
        notes = []
        dnd = [s["name"] for s in chosen if s["agents"] == "do_not_disturb"]
        if dnd:
            notes.append(DND_NOTE.format(names=", ".join(dnd)))
        speak_only = [s["name"] for s in chosen if s["agents"] == "speak_only"]
        if speak_only:
            notes.append(f"{', '.join(speak_only)} can't be rung by agents (speak only).")
        now = time.time()
        allowed = []
        for s in chosen:
            if s["agents"] != "ring_and_speak" or not s["online"]:
                continue
            recent = rings_by_speaker.setdefault(s["id"], deque())
            while recent and now - recent[0] > 3600:
                recent.popleft()
            if len(recent) >= MAX_RINGS_PER_HOUR:
                notes.append(f"{s['name']} has been rung {MAX_RINGS_PER_HOUR} times in the last hour; that's the limit.")
                continue
            allowed.append(s)
        online = allowed
        if not online:
            notes.append("Nothing rang. Presume no one heard.")
            return {"answered": False, "note": " ".join(notes)}
        for s in online:
            rings_by_speaker[s["id"]].append(now)
        seconds = max(MIN_RING_S, min(MAX_RING_S, int(seconds)))
        log_tool(f'ring {", ".join(s["name"] for s in online)} for {seconds} s')
        answered = await voice.rings.ring([s["id"] for s in online], seconds)
        if answered is None:
            log_tool("no one answered")
            notes.append(f"No one answered within {seconds} seconds. Presume no one is there and no one heard.")
            return {"answered": False, "rang": [s["name"] for s in online], "note": " ".join(notes)}
        name = next(s["name"] for s in online if s["id"] == answered)
        log_tool(f"answered on {name}")
        out: dict[str, Any] = {"answered": True, "answered_on": name}
        text = " ".join(message.split())[:MAX_TEXT] or ("Go ahead." if wait_for_reply else "")
        if not text:
            out["note"] = f"Someone answered on {name}; nothing was said to them."
            return out
        pcm = await voice.synthesize(text)
        reply_f = voice.expect_reply(answered) if wait_for_reply else None
        await voice.say(answered, text, listen=wait_for_reply, pcm=pcm)
        if reply_f is None:
            out["note"] = f"Someone answered on {name} and the message was spoken to them."
            return out
        reply = await voice.wait_reply(reply_f, answered, len(pcm) / (RATE * 2) + REPLY_LISTEN_S)
        out["reply"] = reply or ""
        if reply:
            log_tool(f'reply on {name}: "{reply}"')
            out["note"] = "This is what the person said, as transcribed by speech-to-text (it may have small errors)."
        else:
            out["note"] = "They didn't say anything Harkenn understood."
        return out

    def _online(speaker: str) -> tuple[list[dict[str, Any]], list[str], str | None]:
        chosen, error = _targets(ctx, speaker)
        if error:
            return [], [], error
        ok = [s for s in chosen if s["online"] and s["agents"] != "do_not_disturb"]
        skipped = [f'{s["name"]} ({"do not disturb" if s["agents"] == "do_not_disturb" else "offline"})'
                   for s in chosen if s not in ok]
        return ok, skipped, None

    @server.tool()
    def list_timers(speaker: str = "all") -> dict[str, Any]:
        """The timers and alarms on one speaker or all of them, as each last reported: id, kind (timer or alarm),
        label, time left, and for alarms the time of day and days. Also what happened to timers in the last half hour
        and each speaker's unread notifications."""
        chosen, error = _targets(ctx, speaker)
        if error:
            return {"speakers": [], "note": error}
        now = time.time()
        return {"speakers": [{"name": s["name"], "online": s["online"],
                              "timers": [timers_mod.describe_item(i, now) for i in ctx.board.items.get(s["id"], [])],
                              "lately": [e for t, e in ctx.board.events.get(s["id"], []) if now - t < 1800],
                              "unread_notifications": [n["text"] for n in ctx.board.notes.get(s["id"], [])]}
                             for s in chosen]}

    @server.tool()
    async def set_timer(duration: str, label: str = "", speaker: str = "") -> dict[str, Any]:
        """Start a timer on a speaker (its name or id): `duration` like "9m", "1h30m" or seconds. It rings there when
        it's done, until someone stops it (or after 5 minutes)."""
        return await _timer_on(speaker, {"start": duration, "label": label})

    @server.tool()
    async def set_alarm(time_of_day: str, label: str = "", days: str = "", date: str = "",
                        speaker: str = "") -> dict[str, Any]:
        """Set an alarm on a speaker: `time_of_day` "07:30" (24 h, or "7:30 am"); `days` "daily", "weekdays",
        "weekends" or names ("mon,wed"), or empty for once; or `date` "YYYY-MM-DD" for that day."""
        return await _timer_on(speaker, {"alarm": time_of_day, "label": label, "days": days, "date": date})

    @server.tool()
    async def cancel_timer(which: str, speaker: str = "") -> dict[str, Any]:
        """Cancel timers or alarms on a speaker: by label, by id, or "all", "timers", "alarms". Also stops it if
        it's ringing."""
        return await _timer_on(speaker, {"cancel": which})

    async def _timer_on(speaker: str, action: dict[str, Any]) -> dict[str, Any]:
        if not speaker.strip():
            names = [s["name"] for s in _speakers(ctx)]
            if len(names) != 1:
                return {"done": False, "note": "Say which speaker: " + ", ".join(names) + "."}
            speaker = names[0]
        chosen, error = _targets(ctx, speaker)
        if error or len(chosen) != 1:
            return {"done": False, "note": error or "Name one speaker."}
        s = chosen[0]
        if not s["online"]:
            return {"done": False, "note": f'{s["name"]} is offline.'}
        problem = await voice.timer_action(s["id"], action)
        log_tool(f'timer on {s["name"]}: {action}')
        return {"done": not problem, "speaker": s["name"], "note": problem or "Done. list_timers shows it in a moment."}

    @server.tool()
    async def notify(text: str, title: str = "", speaker: str = "all", seconds: int = 12) -> dict[str, Any]:
        """Show a notification on one speaker or all of them: a card that drops in with a soft chime, shown for
        `seconds`, then an envelope stays on the screen until someone taps it. Nothing is spoken (use speak for
        that). The assistant there is told about unread ones."""
        text = " ".join(text.split())[:300]
        if not text:
            return {"shown_on": [], "note": "Nothing to show."}
        ok, skipped, error = _online(speaker)
        if error:
            return {"shown_on": [], "note": error}
        for s in ok:
            ctx.board.notify(s["id"], title, text)
            await ctx.hub.send(s["id"], {"type": "notify", "title": title[:60], "text": text,
                                         "seconds": max(3, min(120, seconds))})
            ctx.hub.publish({"type": "device", "id": s["id"]})
        log_tool(f'notify on {", ".join(s["name"] for s in ok) or "nothing"}: "{text}"')
        return {"shown_on": [s["name"] for s in ok], "skipped": skipped}

    @server.tool()
    async def get_weather() -> dict[str, Any]:
        """The weather where the speakers are, now and for the next days: condition, temperature, feels-like, wind,
        humidity, precipitation, air quality and so on (in the units given). Empty if Harkenn has no place set."""
        w = await run_in_threadpool(ctx.weather.current)
        return {"weather": w or {}, "note": "" if w else "No place is set on Harkenn's Server page, or the weather "
                                                          "service couldn't be reached."}

    @server.tool(description=WEATHER_DESCRIPTION)
    async def show_weather(speaker: str = "all", condition: str = "", location: str = "",
                           temp: float | None = None, feels_like: float | None = None, high: float | None = None,
                           low: float | None = None, wind: float | None = None, wind_dir: str = "",
                           gust: float | None = None, humidity: float | None = None,
                           precip_chance: float | None = None, rain: float | None = None, snow: float | None = None,
                           uv: float | None = None, aqi: float | None = None, description: str = "",
                           units: str = "", is_day: bool | None = None, view: str = "",
                           forecast: list[dict[str, Any]] | None = None, seconds: int = 60) -> dict[str, Any]:
        chosen, error = _targets(ctx, speaker)
        if error:
            return {"shown_on": [], "note": error}
        asked = {k: v for k, v in {
            "condition": condition, "location": location, "temp": temp, "feels_like": feels_like, "high": high,
            "low": low, "wind": wind, "wind_dir": wind_dir, "gust": gust, "humidity": humidity,
            "precip_chance": precip_chance, "rain": rain, "snow": snow, "uv": uv, "aqi": aqi,
            "description": description, "units": units, "is_day": is_day, "view": view, "forecast": forecast,
            "seconds": seconds}.items() if v not in (None, "")}
        shown, skipped, problems = [], [], []
        for s in chosen:
            if s["agents"] == "do_not_disturb":
                skipped.append(f'{s["name"]} (do not disturb)')
            elif not s["online"]:
                skipped.append(f'{s["name"]} (offline)')
            else:
                issues = await voice.show_screen(s["id"], {"weather": asked})
                (problems if issues else shown).append(s["name"] if not issues else f'{s["name"]}: {issues[0]}')
        log_tool(f'weather on {", ".join(shown) or "nothing"}')
        return {"shown_on": shown, "skipped": skipped, "problems": problems}

    @server.prompt()
    def announce(message: str) -> str:
        """Announce something on every speaker."""
        return (f"Use Harkenn's speak tool to announce this on all speakers, rewritten as one or two short spoken "
                f"sentences: {message}\nDon't assume anyone heard it.")

    @server.prompt()
    def ask_someone_at_home(question: str) -> str:
        """Ring the speakers, ask whoever answers a question, and report their answer."""
        return (f"Use Harkenn's ring tool on all speakers with wait_for_reply set, and this question as the message, "
                f"phrased to be spoken aloud: {question}\nReport who answered and what they said. If no one answers, "
                "say so; don't guess an answer.")

    @server.resource("harkenn://guide", name="guide", title="Using Harkenn's speakers", mime_type="text/markdown")
    def guide() -> str:
        """How to use the speakers well."""
        return "# Harkenn\n\n" + INSTRUCTIONS

    server.streamable_http_app(stateless_http=True, json_response=True, host="0.0.0.0")
    manager: StreamableHTTPSessionManager = server.session_manager
    return server, Endpoint(manager, Tokens(ctx))


class Endpoint:
    """The ASGI app for /mcp: checks the bearer token, then hands the request to the MCP SDK. (A class, so Starlette
    routes raw ASGI to it rather than treating it as a request handler.)"""

    def __init__(self, manager: StreamableHTTPSessionManager, tokens: Tokens) -> None:
        self.manager = manager
        self.tokens = tokens

    async def __call__(self, scope, receive, send) -> None:
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        auth = headers.get("authorization", "")
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        if not self.tokens.check(token):
            response = JSONResponse({"error": "unauthorized: use a token from Harkenn's admin page, Agents"},
                                    status_code=401, headers={"WWW-Authenticate": "Bearer"})
            await response(scope, receive, send)
            return
        await self.manager.handle_request(scope, receive, send)
