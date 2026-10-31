"""Shared application state, and the config payload sent to speakers."""
from __future__ import annotations

import copy
import logging
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError
from starlette.requests import HTTPConnection

from .assets import CharacterStore
from .db import Database
from .firmware import FirmwareStore
from .hub import Hub
from .models import DeviceConfig
from .settings import Settings
from .themes import LAYOUTS, character_sizes, resolve_theme
from .updates import UpdateChecker
from .voice.service import VoiceService

log = logging.getLogger("kotodama")

PASSWORD_MASK = "********"


@dataclass
class Context:
    settings: Settings
    db: Database
    hub: Hub
    characters: CharacterStore
    firmware: FirmwareStore
    updates: UpdateChecker = field(init=False)
    voice: VoiceService = field(init=False)

    def __post_init__(self) -> None:
        self.updates = UpdateChecker(self)
        self.voice = VoiceService(self)


def get_ctx(conn: HTTPConnection) -> Context:
    return conn.app.state.ctx


def default_config(ctx: Context) -> DeviceConfig:
    raw = ctx.db.get_setting("default_config")
    if raw:
        try:
            return DeviceConfig.model_validate(raw)
        except ValidationError as e:
            log.warning("stored default config is invalid, using built-in defaults: %s", e)
    return DeviceConfig()


def mask_secrets(config: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(config)
    for network in out.get("wifi", []):
        if network.get("password"):
            network["password"] = PASSWORD_MASK
    return out


def restore_secrets(new: DeviceConfig, old: dict[str, Any]) -> DeviceConfig:
    """Replace masked Wi-Fi passwords coming back from the admin UI with the stored ones."""
    stored = {n["ssid"]: n.get("password", "") for n in old.get("wifi", [])}
    for network in new.wifi:
        if network.password == PASSWORD_MASK:
            if network.ssid not in stored:
                raise ValueError(f"Enter the password for Wi-Fi network '{network.ssid}'")
            network.password = stored[network.ssid]
    return new


def device_payload(ctx: Context, device: dict[str, Any]) -> dict[str, Any]:
    config = DeviceConfig.model_validate(device["config"])
    theme = resolve_theme(config.display.theme, config.display.colors.model_dump())
    sizes = character_sizes(config.display.layout)
    base = ctx.settings.public_url
    background = theme["background"].lstrip("#")  # animations are flattened onto the speaker's background
    character = (ctx.characters.manifest(config.character, sizes, base, background)
                 or ctx.characters.manifest("default", sizes, base, background))
    return {
        "type": "config",
        "version": device["config_version"],
        "device": {"id": device["id"], "name": device["name"]},
        "config": config.model_dump(),
        "theme": theme,
        "layout": {"id": config.display.layout, "widgets": LAYOUTS[config.display.layout]["widgets"]},
        "character": character,
    }
