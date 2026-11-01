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
from typing import TYPE_CHECKING, Any

from mcp.server.mcpserver import MCPServer
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.responses import JSONResponse

from . import __version__
from .voice.calls import MAX_RING_S, MIN_RING_S
from .voice.engines import RATE

if TYPE_CHECKING:
    from .context import Context

log = logging.getLogger("harkenn.mcp")

TOKENS_KEY = "mcp_tokens"
MAX_TEXT = 600
REPLY_LISTEN_S = 45  # after the question has been spoken: time to start talking, plus the speaker's own limit

NOT_HEARD = ("Presume no one heard this: Harkenn can't tell whether anyone is near a speaker. If it matters that "
             "someone gets the message, use ring, which tells you whether someone answered.")

INSTRUCTIONS = f"""Harkenn runs the smart speakers in this home. Use it to talk to the people here.

- list_speakers: the speakers, where they are (their names are usually rooms), and whether each is online.
- speak: say something aloud on one speaker or all of them. Use it for announcements that don't need a response.
  {NOT_HEARD}
- ring: ring one speaker or all of them for up to {MAX_RING_S} seconds, like a phone. Someone taps a speaker to
  answer. You learn which speaker was answered; then you can have a message spoken there and, if you ask, hear their
  spoken reply. If no one answers, presume no one is there and no one heard anything.

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


def _speakers(ctx: Context) -> list[dict[str, Any]]:
    return [{"id": d["id"], "name": d["name"], "online": ctx.hub.online(d["id"]),
             "state": (d.get("status") or {}).get("state", "unknown"),
             "mic_muted": bool((d.get("status") or {}).get("muted"))}
            for d in ctx.db.list_devices()]


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
        for s in chosen:
            if not s["online"]:
                skipped.append(f'{s["name"]} (offline)')
            elif s["state"] in ("listening", "thinking"):
                skipped.append(f'{s["name"]} (busy in a conversation)')
            else:
                pcm = pcm if pcm is not None else await voice.synthesize(text)
                await voice.say(s["id"], text, pcm=pcm)
                spoken.append(s["name"])
        log_tool(f'speak on {", ".join(spoken) or "nothing"}: "{text}"')
        return {"spoken_on": spoken, "skipped": skipped, "note": NOT_HEARD if spoken else "Nothing was spoken."}

    @server.tool(description=RING_DESCRIPTION)
    async def ring(speaker: str = "all", seconds: int = MAX_RING_S, message: str = "",
                   wait_for_reply: bool = False) -> dict[str, Any]:
        chosen, error = _targets(ctx, speaker)
        if error:
            return {"answered": False, "note": error}
        online = [s for s in chosen if s["online"]]
        if not online:
            return {"answered": False, "note": "None of those speakers is online, so nothing rang. Presume no one heard."}
        seconds = max(MIN_RING_S, min(MAX_RING_S, int(seconds)))
        log_tool(f'ring {", ".join(s["name"] for s in online)} for {seconds} s')
        answered = await voice.rings.ring([s["id"] for s in online], seconds)
        if answered is None:
            log_tool("no one answered")
            return {"answered": False, "rang": [s["name"] for s in online],
                    "note": f"No one answered within {seconds} seconds. Presume no one is there and no one heard."}
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
