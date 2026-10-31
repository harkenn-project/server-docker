"""Replies: an OpenAI-compatible chat API when one is configured (Ollama, llama.cpp, LM Studio, a cloud API, Hermes,
...), otherwise a few built-in answers so a speaker is useful out of the box.
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


def ask(text: str, *, device_id: str, speaker: str, character: str, now: datetime, settings: dict[str, Any],
        conversations: Conversations) -> str:
    url = (settings.get("assistant_url") or "").rstrip("/")
    if not url:
        return builtin_reply(text, now)
    prompt = (settings.get("system_prompt") or DEFAULT_PROMPT).format(
        character=character, speaker=speaker, now=describe_time(now))
    messages = [{"role": "system", "content": prompt}, *conversations.history(device_id),
                {"role": "user", "content": text}]
    body: dict[str, Any] = {"messages": messages, "max_tokens": 300}
    if settings.get("assistant_model"):
        body["model"] = settings["assistant_model"]
    headers = {"Content-Type": "application/json"}
    if settings.get("assistant_key"):
        headers["Authorization"] = f"Bearer {settings['assistant_key']}"
    req = urllib.request.Request(f"{url}/chat/completions", data=json.dumps(body).encode(), headers=headers)
    with urllib.request.urlopen(req, timeout=90) as resp:
        data = json.loads(resp.read())
    reply = speakable(data["choices"][0]["message"]["content"] or "")
    conversations.add(device_id, text, reply)
    return reply or "Sorry, I don't have an answer for that."
