"""Request bodies and the per-device configuration schema."""
from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .themes import LAYOUTS, THEMES

HEX_COLOR = r"^#[0-9a-fA-F]{6}$"
SLUG = r"^[a-z0-9][a-z0-9_-]{0,31}$"


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Lenient(BaseModel):
    """For bodies sent by firmware: ignore unknown fields so newer firmware still talks to older servers."""
    model_config = ConfigDict(extra="ignore")


# device configuration

class WifiNetwork(Strict):
    ssid: str = Field(min_length=1, max_length=32)
    password: str = Field(default="", max_length=64)

    @field_validator("password")
    @classmethod
    def _wpa_length(cls, v: str) -> str:
        if v and len(v) < 8:
            raise ValueError("WPA passwords are 8-63 characters (leave empty for an open network)")
        return v


class ColorOverrides(Strict):
    background: str | None = Field(None, pattern=HEX_COLOR)
    surface: str | None = Field(None, pattern=HEX_COLOR)
    primary: str | None = Field(None, pattern=HEX_COLOR)
    accent: str | None = Field(None, pattern=HEX_COLOR)
    text: str | None = Field(None, pattern=HEX_COLOR)
    muted: str | None = Field(None, pattern=HEX_COLOR)


class DisplayConfig(Strict):
    layout: str = "character_clock"
    theme: str = "midnight"
    colors: ColorOverrides = Field(default_factory=ColorOverrides)
    brightness: int = Field(75, ge=5, le=100)
    sleep_after_s: int = Field(300, ge=0, le=86400, description="0 = never dim")
    clock_24h: bool = True

    @field_validator("layout")
    @classmethod
    def _known_layout(cls, v: str) -> str:
        if v not in LAYOUTS:
            raise ValueError(f"unknown layout '{v}'")
        return v

    @field_validator("theme")
    @classmethod
    def _known_theme(cls, v: str) -> str:
        if v not in THEMES:
            raise ValueError(f"unknown theme '{v}'")
        return v


class AudioConfig(Strict):
    volume: int = Field(70, ge=0, le=100)
    silence_s: int = Field(5, ge=1, le=30, description="stop listening after this long without speech")
    wake_word: str = Field("ok_nabu", max_length=64)
    sound_pack: str = Field("classic", pattern=SLUG, description="sounds.py; classic = the speaker's own chimes")


class AssistantConfig(Strict):
    url: str = Field("", max_length=256, description="Hermes endpoint; empty until Hermes exists")


HHMM = r"^$|^([01]\d|2[0-3]):[0-5]\d$"


class AgentsConfig(Strict):
    """What AI agents may do on this speaker through the MCP server (mcp_server.py). Enforced by the server; the
    speaker never sees it."""
    access: Literal["ring_and_speak", "speak_only", "do_not_disturb"] = "ring_and_speak"
    quiet_from: str = Field("", pattern=HHMM, description="start of quiet hours (do not disturb), server time")
    quiet_to: str = Field("", pattern=HHMM, description="end of quiet hours")


class DeviceConfig(Strict):
    character: str = Field("default", pattern=SLUG)
    display: DisplayConfig = Field(default_factory=DisplayConfig)
    audio: AudioConfig = Field(default_factory=AudioConfig)
    wifi: list[WifiNetwork] = Field(default_factory=list, max_length=8)
    timezone: str = Field("MST7MDT,M3.2.0,M11.1.0", min_length=3, max_length=64, description="POSIX TZ string")
    assistant: AssistantConfig = Field(default_factory=AssistantConfig)
    agents: AgentsConfig = Field(default_factory=AgentsConfig)
    log_level: Literal["error", "warn", "info", "debug"] = "info"


# voice pipeline (server-wide; see app/voice)

class VoiceSettings(Strict):
    stt_engine: Literal["builtin", "wyoming"] = "builtin"
    stt_model: str = Field("base.en", pattern=r"^[A-Za-z0-9._/-]{2,80}$", description="faster-whisper model")
    stt_address: str = Field("", max_length=200, description="Wyoming speech-to-text host:port")
    language: str = Field("en", pattern=r"^[a-z]{0,3}$")
    tts_engine: Literal["builtin", "wyoming"] = "builtin"
    tts_voice: str = Field("en_US-ljspeech-high", pattern=r"^[A-Za-z0-9._-]{0,80}$")
    tts_address: str = Field("", max_length=200, description="Wyoming text-to-speech host:port")
    assistant_api: Literal["openai", "anthropic", "hermes"] = "openai"
    assistant_url: str = Field("", max_length=300, description="API base URL, e.g. http://ollama:11434/v1")
    assistant_model: str = Field("", max_length=120)
    assistant_key: str = Field("", max_length=400)
    assistant_timeout_s: int = Field(120, ge=5, le=600, description="how long an answer may take (agents use tools)")
    follow_up: Literal["always", "questions", "never"] = Field(
        "always", description="listen for a reply after answering: always, only after a question, or never")
    context_conversations: int = Field(5, ge=0, le=50, description="the assistant sees this many recent conversations")
    context_minutes: int = Field(60, ge=1, le=1440, description="...from at most this many minutes ago")
    system_prompt: str = Field("", max_length=4000, description="empty = built-in prompt")
    keep_recordings: bool = Field(False, description="save what speakers hear, for troubleshooting")

    @field_validator("stt_address", "tts_address", "assistant_url")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()


class VoiceSettingsIn(VoiceSettings):
    from_environment: dict[str, Any] | None = Field(None, description="read-only, ignored (sent back by the admin UI)")


class SoundPackIn(Strict):
    name: str = Field(min_length=1, max_length=40)
    kind: Literal["voiced", "custom"]
    voice: str = Field("", pattern=r"^[A-Za-z0-9._-]{0,80}$", description="voiced: the Piper voice")
    lines: dict[str, str] = Field(default_factory=dict, description="voiced: slot -> text")
    copy_from: str = Field("", pattern=r"^$|" + SLUG[1:], description="custom: start from this pack's clips")

    @field_validator("lines")
    @classmethod
    def _lines(cls, v: dict[str, str]) -> dict[str, str]:
        return {k: " ".join(t.split())[:120] for k, t in v.items()}


class SoundPackUpdate(Strict):
    name: str | None = Field(None, min_length=1, max_length=40)
    voice: str | None = Field(None, pattern=r"^[A-Za-z0-9._-]{1,80}$")
    lines: dict[str, str] | None = None

    @field_validator("lines")
    @classmethod
    def _lines(cls, v: dict[str, str] | None) -> dict[str, str] | None:
        return v and {k: " ".join(t.split())[:120] for k, t in v.items()}


class PlaySoundIn(Strict):
    pack: str = Field(pattern=SLUG)
    slot: str = Field(pattern=r"^[a-z]{1,16}$")


class AgentTokenIn(Strict):
    name: str = Field(min_length=1, max_length=60, description="who the token is for, e.g. Hermes")


class ServerSettingsIn(Strict):
    public_url: str = Field("", max_length=200, pattern=r"^$|^https?://[^\s/?#]+(/[^\s?#]*)?$",
                            description="empty = HARKENN_PUBLIC_URL")

    @field_validator("public_url", mode="before")
    @classmethod
    def _clean(cls, v: Any) -> Any:
        return v.strip().rstrip("/") if isinstance(v, str) else v


class SayIn(Strict):
    text: str = Field(min_length=1, max_length=600)
    voice: str = Field("", pattern=r"^[A-Za-z0-9._-]{0,80}$")


# device -> server

class AdoptionRequestIn(Lenient):
    device_id: str
    hardware: str = Field("", max_length=64)
    firmware: str = Field("", max_length=32)

    @field_validator("device_id", mode="before")
    @classmethod
    def _normalize_mac(cls, v: Any) -> str:
        s = re.sub(r"[^0-9a-fA-F]", "", str(v)).lower()
        if len(s) != 12:
            raise ValueError("device_id must be a MAC address")
        return s


class StatusIn(Lenient):
    state: str | None = Field(None, max_length=24)
    firmware: str | None = Field(None, max_length=32)
    ip: str | None = Field(None, max_length=45)
    ssid: str | None = Field(None, max_length=32)
    rssi: int | None = None
    uptime_s: int | None = None
    free_heap: int | None = None
    free_psram: int | None = None
    battery: int | None = Field(None, ge=0, le=100)
    charging: bool | None = None
    volume: int | None = None
    muted: bool | None = None
    brightness: int | None = None
    config_version: int | None = None
    message: str | None = Field(None, max_length=200)


class LogLine(Lenient):
    ts: float | None = None
    level: str = Field("I", max_length=8)
    tag: str = Field("", max_length=32)
    message: str = Field(max_length=1024)


class LogBatch(Lenient):
    lines: list[LogLine] = Field(max_length=200)


# admin -> server

class ApproveIn(Strict):
    code: str = Field(pattern=r"^\d{3}$")
    name: str = Field("", max_length=48)


class DeviceUpdateIn(Strict):
    name: str = Field(min_length=1, max_length=48)
    config: DeviceConfig


class DefaultsIn(Strict):
    config: DeviceConfig


class CommandIn(Strict):
    command: Literal["reboot", "identify", "refresh_config", "set_volume", "play_state"]
    args: dict[str, Any] = Field(default_factory=dict)


class FirmwareUpdateIn(Strict):
    firmware: str = Field(pattern=r"^[0-9a-f]{16}$")


class CharacterIn(Strict):
    id: str = Field(pattern=SLUG)
    name: str = Field(min_length=1, max_length=48)
