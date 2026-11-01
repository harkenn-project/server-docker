"""Replies: a chat model or agent when one is configured, otherwise a few built-in answers so a speaker is useful out
of the box.

Three kinds of API cover most of what's out there:

    openai     OpenAI-compatible chat completions: OpenAI, DeepSeek, OpenRouter, Mistral, Groq, Gemini (its OpenAI
               endpoint), Ollama, llama.cpp, LM Studio, vLLM, LocalAI, ... Harkenn keeps the recent turns.
    anthropic  The Anthropic Messages API. Harkenn keeps the recent turns.
    hermes     A Hermes Agent gateway (its Responses API). Each speaker is a named conversation that Hermes keeps,
               with its own memory, tools and skills; Harkenn sends only what was just said.
"""
from __future__ import annotations

import json
import re
import time
import urllib.request
from collections import deque
from datetime import datetime
from typing import Any

HISTORY_TURNS = 6
HISTORY_TTL_S = 300  # a conversation is forgotten after five quiet minutes
MAX_REPLY_CHARS = 600

DEFAULT_PROMPT = (
    "You are {character}, a friendly voice assistant on a small smart speaker called {speaker}. "
    "Your words are spoken aloud: answer in one to three short sentences, with no lists, markdown, emoji or URLs. "
    "It is {now} where the speaker is."
)


class Conversations:
    """Recent turns per speaker, so follow-up questions work."""

    def __init__(self) -> None:
        self._turns: dict[str, deque] = {}
        self._last: dict[str, float] = {}

    def history(self, device_id: str) -> list[dict[str, str]]:
        if time.time() - self._last.get(device_id, 0) > HISTORY_TTL_S:
            self._turns.pop(device_id, None)
        return list(self._turns.get(device_id, ()))

    def add(self, device_id: str, user: str, assistant: str) -> None:
        turns = self._turns.setdefault(device_id, deque(maxlen=HISTORY_TURNS * 2))
        turns.extend([{"role": "user", "content": user}, {"role": "assistant", "content": assistant}])
        self._last[device_id] = time.time()


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


def post(url: str, body: dict[str, Any], headers: dict[str, str], timeout: float) -> dict[str, Any]:
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def chat_completions(url: str, settings: dict[str, Any], system: str, messages: list[dict[str, str]],
                     timeout: float) -> str:
    body: dict[str, Any] = {"messages": [{"role": "system", "content": system}, *messages], "max_tokens": MAX_TOKENS}
    if settings.get("assistant_model"):
        body["model"] = settings["assistant_model"]
    key = settings.get("assistant_key")
    data = post(f"{url}/chat/completions", body, {"Authorization": f"Bearer {key}"} if key else {}, timeout)
    return data["choices"][0]["message"]["content"] or ""


def anthropic_messages(url: str, settings: dict[str, Any], system: str, messages: list[dict[str, str]],
                       timeout: float) -> str:
    body = {"model": settings.get("assistant_model") or "claude-haiku-5-5", "max_tokens": MAX_TOKENS,
            "system": system, "messages": messages}
    headers = {"x-api-key": settings.get("assistant_key") or "", "anthropic-version": "2023-06-01"}
    data = post(f"{url}/messages", body, headers, timeout)
    return "".join(part.get("text", "") for part in data.get("content", []) if part.get("type") == "text")


def hermes_responses(url: str, settings: dict[str, Any], system: str, text: str, device_id: str,
                     timeout: float) -> str:
    conversation = f"harkenn-{device_id}"
    body: dict[str, Any] = {"input": text, "instructions": system, "conversation": conversation, "store": True}
    if settings.get("assistant_model"):
        body["model"] = settings["assistant_model"]
    headers = {"X-Hermes-Session-Key": conversation}
    if settings.get("assistant_key"):
        headers["Authorization"] = f"Bearer {settings['assistant_key']}"
    data = post(f"{url}/responses", body, headers, timeout)
    if isinstance(data.get("output_text"), str):
        return data["output_text"]
    return "".join(part.get("text", "") for item in data.get("output", []) if item.get("type") == "message"
                   for part in item.get("content", []) if part.get("type") in ("output_text", "text"))


def ask(text: str, *, device_id: str, speaker: str, character: str, now: datetime, settings: dict[str, Any],
        conversations: Conversations) -> str:
    api = settings.get("assistant_api") or "openai"
    url = (settings.get("assistant_url") or DEFAULT_URLS.get(api, "")).rstrip("/")
    if not url:
        return builtin_reply(text, now)
    system = (settings.get("system_prompt") or DEFAULT_PROMPT).format(
        character=character, speaker=speaker, now=describe_time(now))
    timeout = float(settings.get("assistant_timeout_s") or 120)
    if api == "hermes":  # Hermes keeps the conversation itself
        reply = speakable(hermes_responses(url, settings, system, text, device_id, timeout))
    else:
        messages = [*conversations.history(device_id), {"role": "user", "content": text}]
        call = anthropic_messages if api == "anthropic" else chat_completions
        reply = speakable(call(url, settings, system, messages, timeout))
        conversations.add(device_id, text, reply)
    return reply or "Sorry, I don't have an answer for that."
