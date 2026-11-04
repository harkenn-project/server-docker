"""Request bodies and the per-device configuration schema."""
from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .themes import LAYOUTS, THEMES

HEX_COLOR = r"^#[0-9a-fA-F]{6}$"
SLUG = r"^[a-z0-9][a-z0-9_-]{0,31}$"


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Lenient(BaseModel):
    """For bodies sent by firmware: ignore unknown fields so newer firmware still talks to older servers."""
    model_config = ConfigDict(extra="ignore")


# device configuration

IPV4 = r"^$|^((25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(25[0-5]|2[0-4]\d|1?\d?\d)$"


class WifiNetwork(Strict):
    ssid: str = Field(min_length=1, max_length=32)
    password: str = Field(default="", max_length=64)
    # Static IP on this network; all empty = DHCP. Needs firmware 0.13 or later.
    ip: str = Field("", pattern=IPV4)
    gateway: str = Field("", pattern=IPV4)
    subnet: str = Field("", pattern=IPV4)
    dns: str = Field("", pattern=IPV4)

    @field_validator("password")
    @classmethod
    def _wpa_length(cls, v: str) -> str:
        if v and len(v) < 8:
            raise ValueError("WPA passwords are 8-63 characters (leave empty for an open network)")
        return v

    @model_validator(mode="after")
    def _static_complete(self) -> "WifiNetwork":
        if self.ip and not (self.gateway and self.subnet):
            raise ValueError(f"a static IP on '{self.ssid}' also needs a gateway and a subnet mask")
        if not self.ip and (self.gateway or self.subnet or self.dns):
            raise ValueError(f"enter the static IP for '{self.ssid}', or clear the gateway, subnet and DNS")
        return self


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
    sleep_display: Literal["character", "clock", "off"] = Field(
        "character", description="while asleep: the character sleeping (dimmed), only a big clock (dimmed), or off")
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
    silence_s: int = Field(2, ge=1, le=30, description="stop listening after this long without speech")
    wake_word: str = Field("", max_length=64, description="wakewords.py id; empty (or unknown) = none")
    wake_sensitivity: Literal["low", "normal", "high"] = "normal"
    sound_pack: str = Field("classic", pattern=SLUG, description="sounds.py; classic = the speaker's own chimes")
    wake_arbitration: bool = Field(True, description="when several speakers hear the wake word, only the one that "
                                                     "heard it loudest answers (they signal each other above hearing)")


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
    timezone_name: str = Field("", max_length=64, pattern=r"^$|^[A-Za-z][A-Za-z0-9_+-]*(/[A-Za-z0-9_+-]+)*$",
                               description="the zone's name (America/Edmonton); when known, it sets `timezone`")
    assistant: AssistantConfig = Field(default_factory=AssistantConfig)
    agents: AgentsConfig = Field(default_factory=AgentsConfig)
    log_level: Literal["error", "warn", "info", "debug"] = "info"
    remote: bool = Field(False, description="may connect through the relay when it can't reach Harkenn (relay.py)")


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
    follow_up: Literal["assistant", "always", "questions", "never"] = Field(
        "assistant", description="listen for a reply after answering: when the assistant asks to, always, only "
                                 "after a question, or never")
    context_conversations: int = Field(5, ge=0, le=50, description="the assistant sees this many recent conversations")
    context_minutes: int = Field(60, ge=1, le=1440, description="...from at most this many minutes ago")
    voiceprint_threshold: float = Field(0.7, ge=0.4, le=0.95, description="how alike a voice must be to count as a match")
    screen: bool = Field(True, description="the assistant may show pictures and buttons on the speakers' screens")
    system_prompt: str = Field("", max_length=4000, description="empty = built-in prompt")
    keep_recordings: bool = Field(False, description="save what speakers hear, for troubleshooting")

    @field_validator("stt_address", "tts_address", "assistant_url")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()


class VoiceSettingsIn(VoiceSettings):
    from_environment: dict[str, Any] | None = Field(None, description="read-only, ignored (sent back by the admin UI)")


class PersonUpdate(Strict):
    name: str | None = Field(None, min_length=1, max_length=40)
    phonetic: str | None = Field(None, max_length=60)
    gender: str | None = Field(None, max_length=40)


class ScreenOverlayIn(Strict):
    description: str = Field("", max_length=300, description="what it shows, for the assistant")
    svg: str = Field(min_length=5, max_length=100_000, description="SVG with {{param}} placeholders")
    x: int = Field(0, ge=-240, le=240)
    y: int = Field(0, ge=-240, le=240)
    w: int = Field(240, ge=8, le=240)
    h: int = Field(240, ge=8, le=240)


class ScreenSpecIn(Strict):
    spec: dict[str, Any] = Field(description="a screen, as the assistant would write it (screens.py)")


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


class ChannelIn(Strict):
    channel: Literal["stable", "beta", "alpha", "development"]


class PersonIn(Strict):
    name: str = Field(min_length=1, max_length=40)
    phonetic: str = Field("", max_length=60, description="how to say the name, e.g. Ma-ree-ka")
    gender: str = Field("", max_length=40, description="gender identity, as they describe it")
    consent: bool = Field(description="they agreed to have a voiceprint kept")

    @field_validator("consent")
    @classmethod
    def _consent(cls, v: bool) -> bool:
        if not v:
            raise ValueError("a voiceprint needs the person's agreement")
        return v


class AgentTokenIn(Strict):
    name: str = Field(min_length=1, max_length=60, description="who the token is for, e.g. Hermes")


class ServerSettingsIn(Strict):
    public_url: str = Field("", max_length=200, pattern=r"^$|^https?://[^\s/?#]+(/[^\s?#]*)?$",
                            description="empty = HARKENN_PUBLIC_URL")

    @field_validator("public_url", mode="before")
    @classmethod
    def _clean(cls, v: Any) -> Any:
        return v.strip().rstrip("/") if isinstance(v, str) else v


class WeatherPlaceIn(Strict):
    place: str = Field("", max_length=100, description="a town or city; empty = no live weather")
    units: Literal["metric", "imperial"] = "metric"


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
    local_failures: int | None = Field(None, ge=0, description="wrong passwords on its web page since the last right one")
    local_locked: bool | None = Field(None, description="its web page is locked until unlocked from here")
    hotspot: bool | None = None
    largest_free: int | None = Field(None, description="largest free block of internal RAM")
    min_free_heap: int | None = Field(None, description="least internal RAM free since it started")
    stack_free: dict[str, int] | None = Field(None, description="per task: stack never used, in bytes")
    remote: str | None = Field(None, max_length=64, description="the relay id the speaker holds (relay.py)")
    via_relay: bool | None = None
    card: dict[str, int] | None = Field(None, description="the micro SD card: total_mb, free_mb (none: no card)")


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
    group: str | None = Field(None, pattern=SLUG, description="the group it joins; empty = the default group")


class DeviceUpdateIn(Strict):
    name: str = Field(min_length=1, max_length=48)
    config: DeviceConfig
    group: str | None = Field(None, pattern=SLUG, description="its group; empty = stay in it")
    overrides: list[str] | None = Field(None, max_length=200, description="its own settings (groups.py); with "
                                        "these, config.wifi is its own networks. Empty = what differs from the group")


class GroupIn(Strict):
    name: str = Field(min_length=1, max_length=40)
    copy_from: str | None = Field(None, pattern=SLUG)


class GroupUpdateIn(Strict):
    name: str = Field(min_length=1, max_length=40)
    config: DeviceConfig


class GroupMembersIn(Strict):
    devices: list[str] = Field(max_length=200)


class CommandIn(Strict):
    command: Literal["reboot", "identify", "refresh_config", "set_volume", "play_state"]
    args: dict[str, Any] = Field(default_factory=dict)


class SpeakerPasswordIn(Strict):
    password: str = Field("", max_length=63, description="empty = make a random one")


class FirmwareUpdateIn(Strict):
    firmware: str = Field(pattern=r"^[0-9a-f]{16}$")


class CharacterIn(Strict):
    id: str = Field(pattern=SLUG)
    name: str = Field(min_length=1, max_length=48)
