"""Replies: a chat model or agent when one is configured, otherwise a few built-in answers so a speaker is useful out
of the box.

Three kinds of API cover most of what's out there:

    openai     OpenAI-compatible chat completions: OpenAI, DeepSeek, OpenRouter, Mistral, Groq, Gemini (its OpenAI
               endpoint), Ollama, llama.cpp, LM Studio, vLLM, LocalAI, ...
    anthropic  The Anthropic Messages API.
    hermes     A Hermes Agent gateway: its (stateless) chat completions, with its tools, skills and long-term memory
               (scoped by X-Hermes-Session-Key to the person, when voiceprints recognize them, else the speaker).

Harkenn keeps the history for all of them, so it stays small: a conversation is one tap or wake word plus the replies
that follow it, and the assistant sees only the last few conversations from the last hour or so (both adjustable).
"""
from __future__ import annotations

import json
import re
import time
import urllib.request
from datetime import datetime
from typing import Any

from .. import screens

LISTEN_MARK = "[[listen]]"
LISTEN_RE = re.compile(r"\[\[\s*listen\s*\]\]", re.I)
# Added to the instructions when the assistant decides whether the speaker waits for a reply (follow_up "assistant").
LISTEN_PROMPT = (
    f"\n\nListening for a reply: after you speak, the speaker stops listening unless you end your reply with "
    f"{LISTEN_MARK}. The person can't answer you without it. So end with {LISTEN_MARK} whenever you expect an "
    f"answer: when you ask a question, offer choices, ask them to confirm something, or need more detail to help. "
    f"Leave it off only when you're done and nothing more is needed. {LISTEN_MARK} is never spoken aloud.\n"
    f"Examples: \"Sure, which room? {LISTEN_MARK}\" / \"Do you want me to turn it off? {LISTEN_MARK}\" / "
    f"\"Done, the porch light is on.\""
)
MAX_TURNS_PER_CONVERSATION = 20  # the oldest exchanges of a very long conversation are dropped
MAX_REPLY_CHARS = 600

DEFAULT_PROMPT = (
    "You are {character}, a friendly voice assistant on a small smart speaker called {speaker}. "
    "Your words are spoken aloud: answer in one to three short sentences, with no lists, markdown, emoji or URLs. "
    "It is {now} where the speaker is."
)
# An agent has its own identity (Hermes: SOUL.md): only say how it's being reached, and how to talk.
AGENT_PROMPT = (
    "Someone is talking to you through a smart speaker called {speaker}. Your words are spoken aloud: answer in one "
    "to three short sentences, with no lists, markdown, emoji or URLs. It is {now} where the speaker is."
)


class Conversations:
    """Each speaker's recent conversations. A conversation starts with a tap or the wake word (start) and goes on
    while the speaker listens for replies; history() is what the assistant gets to see."""

    def __init__(self) -> None:
        self._convs: dict[str, list[dict[str, Any]]] = {}

    def start(self, device_id: str) -> None:
        convs = self._convs.setdefault(device_id, [])
        if not convs or convs[-1]["turns"]:  # an empty one (nothing was said) is reused
            convs.append({"turns": [], "last": time.time(), "person": ""})
        convs[-1]["person"] = ""
        del convs[:-50]

    def person(self, device_id: str, sure: str = "") -> str:
        """Whose long-term memory the current conversation uses: the person voice recognition was sure of at its
        start, until it's sure of someone else. Unsure turns keep it, so one conversation stays in one memory."""
        convs = self._convs.setdefault(device_id, [])
        if not convs:
            convs.append({"turns": [], "last": time.time(), "person": ""})
        if sure:
            convs[-1]["person"] = sure
        return convs[-1].get("person", "")

    def add(self, device_id: str, user: str, assistant: str) -> None:
        convs = self._convs.setdefault(device_id, [])
        if not convs:
            convs.append({"turns": [], "last": time.time()})
        conv = convs[-1]
        conv["turns"] += [{"role": "user", "content": user}, {"role": "assistant", "content": assistant}]
        del conv["turns"][:-MAX_TURNS_PER_CONVERSATION * 2]
        conv["last"] = time.time()

    def history(self, device_id: str, conversations: int = 5, minutes: float = 60) -> list[dict[str, str]]:
        """The turns of the last `conversations` conversations (the current one included) that were active in the
        last `minutes`, oldest first."""
        if conversations <= 0:
            return []
        since = time.time() - minutes * 60
        recent = [c for c in self._convs.get(device_id, []) if c["turns"] and c["last"] >= since]
        return [t for c in recent[-conversations:] for t in c["turns"]]


def speakable(text: str) -> str:
    """Strip what a chat model might add that shouldn't be read out."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)  # reasoning models
    text = re.sub(r"```.*?```", "", text, flags=re.S)
    text = re.sub(r"[*_#`>]+", "", text)
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > MAX_REPLY_CHARS:
        cut = text[:MAX_REPLY_CHARS]
        text = cut[: max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! ")) + 1] or cut
    return text


def clock(now: datetime) -> str:
    return f"{now.hour % 12 or 12}:{now.minute:02d} {'AM' if now.hour < 12 else 'PM'}"


def day(now: datetime) -> str:
    return f"{now:%A}, {now:%B} {now.day}"


def describe_time(now: datetime) -> str:
    return f"{day(now)} {now.year}, {clock(now)}"


def builtin_reply(text: str, now: datetime) -> str:
    t = text.lower()
    if re.search(r"\bwhat time\b|\bthe time\b", t):
        return f"It's {clock(now)}."
    if re.search(r"\b(date|what day)\b", t):
        return f"It's {day(now)}."
    if re.search(r"^(hi|hello|hey)\b", t):
        return "Hi there!"
    return f"You said: {text}. No assistant is connected yet, so that's all I can do for now."


APIS = {"openai": "OpenAI-compatible (most APIs and local servers)", "anthropic": "Anthropic Messages API",
        "hermes": "Hermes Agent (its own memory, tools and skills)"}
DEFAULT_URLS = {"anthropic": "https://api.anthropic.com/v1"}
MAX_TOKENS = 300
MAX_TOKENS_WITH_SCREEN = 3000  # room for an SVG, or a few as animation frames


def post(url: str, body: dict[str, Any], headers: dict[str, str], timeout: float) -> dict[str, Any]:
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def chat_completions(url: str, settings: dict[str, Any], system: str, messages: list[dict[str, str]],
                     timeout: float, headers: dict[str, str] | None = None, max_tokens: int = MAX_TOKENS) -> str:
    body: dict[str, Any] = {"messages": [{"role": "system", "content": system}, *messages], "max_tokens": max_tokens}
    if settings.get("assistant_model"):
        body["model"] = settings["assistant_model"]
    headers = dict(headers or {})
    if settings.get("assistant_key"):
        headers["Authorization"] = f"Bearer {settings['assistant_key']}"
    data = post(f"{url}/chat/completions", body, headers, timeout)
    return data["choices"][0]["message"]["content"] or ""


def anthropic_messages(url: str, settings: dict[str, Any], system: str, messages: list[dict[str, str]],
                       timeout: float, max_tokens: int = MAX_TOKENS) -> str:
    body = {"model": settings.get("assistant_model") or "claude-haiku-5-5", "max_tokens": max_tokens,
            "system": system, "messages": messages}
    headers = {"x-api-key": settings.get("assistant_key") or "", "anthropic-version": "2023-06-01"}
    data = post(f"{url}/messages", body, headers, timeout)
    return "".join(part.get("text", "") for part in data.get("content", []) if part.get("type") == "text")


class Reply(str):
    """What to say; `listen` is True when the assistant asked to hear the person's answer (LISTEN_MARK), `screen` is
    what it wants shown on the speaker's screen (screens.py), if anything."""
    listen: bool = False
    screen: dict[str, Any] | None = None
    screen_error: str = ""

    def __new__(cls, text: str, listen: bool = False, screen: dict[str, Any] | None = None,
                screen_error: str = "") -> "Reply":
        r = super().__new__(cls, text)
        r.listen = listen
        r.screen = screen
        r.screen_error = screen_error
        return r


def request_context(speaker: str, device_id: str, who: str = "") -> str:
    """What the assistant is told about where a request comes from (and, with voiceprints, who it's probably from)."""
    out = (f"\n\nThis request comes from the speaker named \"{speaker}\" (Harkenn speaker id {device_id}: use it "
           f"with Harkenn's tools to reach this speaker).")
    return out + (f" {who}" if who else "")


def ask(text: str, *, device_id: str, speaker: str, character: str, now: datetime, settings: dict[str, Any],
        conversations: Conversations, context: str = "", memory_key: str = "", screen_prompt: str = "",
        voice_tag: str = "") -> Reply:
    api = settings.get("assistant_api") or "openai"
    url = (settings.get("assistant_url") or DEFAULT_URLS.get(api, "")).rstrip("/")
    if not url:
        return Reply(builtin_reply(text, now))
    system = (settings.get("system_prompt") or (AGENT_PROMPT if api == "hermes" else DEFAULT_PROMPT)).format(
        character=character, speaker=speaker, now=describe_time(now))
    system += context + screen_prompt
    if settings.get("follow_up") == "assistant":
        system += LISTEN_PROMPT  # last, so it isn't lost under everything else
    timeout = float(settings.get("assistant_timeout_s") or 120)
    history = conversations.history(device_id, int(settings.get("context_conversations", 5)),
                                    float(settings.get("context_minutes", 60)))
    # who is probably talking (voiceprints), on the message itself and kept in the history, so the assistant can
    # tell who said what when people take turns
    if voice_tag:
        text = f"{voice_tag} {text}"
    messages = [*history, {"role": "user", "content": text}]
    tokens = MAX_TOKENS_WITH_SCREEN if screen_prompt else MAX_TOKENS
    if api == "anthropic":
        reply = anthropic_messages(url, settings, system, messages, timeout, tokens)
    elif api == "hermes":  # stateless: Hermes sees only this history; its long-term memory is scoped to the speaker
        reply = chat_completions(url, settings, system, messages, timeout,
                                 {"X-Hermes-Session-Key": memory_key or f"harkenn-{device_id}"}, tokens)
    else:
        reply = chat_completions(url, settings, system, messages, timeout, max_tokens=tokens)
    # the screen first: speakable() would mangle its JSON. It isn't kept in the history (an SVG would fill it up).
    reply, screen, screen_error = screens.split_marker(reply) if screen_prompt else (reply, None, "")
    if screen_prompt and screen is None and not screen_error:
        screen = screens.bare_picture(reply)
    # the marker, or a question at the end (in case the assistant forgot it)
    listen = bool(LISTEN_RE.search(reply)) or speakable(LISTEN_RE.sub("", reply)).rstrip().endswith("?")
    reply = speakable(LISTEN_RE.sub("", reply))
    conversations.add(device_id, text, reply + (f" {LISTEN_MARK}" if listen else ""))  # so it keeps doing it
    if not reply and screen:
        return Reply("", listen, screen, screen_error)  # just the screen
    return Reply(reply or "Sorry, I don't have an answer for that.", listen and bool(reply), screen, screen_error)
