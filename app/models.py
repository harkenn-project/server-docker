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
    wake_word: str = Field("ok_nabu", max_length=64)


class AssistantConfig(Strict):
    url: str = Field("", max_length=256, description="Hermes endpoint; empty until Hermes exists")


class DeviceConfig(Strict):
    character: str = Field("default", pattern=SLUG)
    display: DisplayConfig = Field(default_factory=DisplayConfig)
    audio: AudioConfig = Field(default_factory=AudioConfig)
    wifi: list[WifiNetwork] = Field(default_factory=list, max_length=8)
    timezone: str = Field("MST7MDT,M3.2.0,M11.1.0", min_length=3, max_length=64, description="POSIX TZ string")
    assistant: AssistantConfig = Field(default_factory=AssistantConfig)
    log_level: Literal["error", "warn", "info", "debug"] = "info"


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


class CharacterIn(Strict):
    id: str = Field(pattern=SLUG)
    name: str = Field(min_length=1, max_length=48)
