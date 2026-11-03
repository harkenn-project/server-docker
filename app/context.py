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
from .homeassistant import Bridge
from .screens import Screens
from .sounds import CLASSIC, SoundStore
from .wakewords import WakeWords
from .themes import LAYOUTS, character_sizes, resolve_theme
from .updates import UpdateChecker
from .voice.service import VoiceService

log = logging.getLogger("harkenn")

PASSWORD_MASK = "********"
PUBLIC_URL_KEY = "public_url"  # settings table: public URL set in the admin UI


@dataclass
class Context:
    settings: Settings
    db: Database
    hub: Hub
    characters: CharacterStore
    firmware: FirmwareStore
    updates: UpdateChecker = field(init=False)
    voice: VoiceService = field(init=False)
    sounds: SoundStore = field(init=False)
    wakewords: WakeWords = field(init=False)
    screens: Screens = field(init=False)
    homeassistant: Bridge = field(init=False)

    def __post_init__(self) -> None:
        self.screens = Screens(self.db)
        self.homeassistant = Bridge(self)
        self.updates = UpdateChecker(self)
        self.voice = VoiceService(self)
        self.sounds = SoundStore(self.settings.data_dir / "sounds", self.settings.cache_dir / "sounds")
        self.wakewords = WakeWords(self.settings.wakewords_dir, self.settings.data_dir / "models" / "wakewords",
                                   self.settings.data_dir / "wakewords")

    def public_url(self) -> str:
        """The server's public address: set in the admin UI, else HARKENN_PUBLIC_URL."""
        return (self.db.get_setting(PUBLIC_URL_KEY) or self.settings.public_url).rstrip("/")


def get_ctx(conn: HTTPConnection) -> Context:
    return conn.app.state.ctx


def base_url(conn: HTTPConnection) -> str:
    """The address a speaker used to reach this server. Links sent back to that speaker use it, so they keep working
    whatever the public URL is (e.g. a speaker adopted through http://harkenn)."""
    scheme = {"ws": "http", "wss": "https"}.get(conn.url.scheme, conn.url.scheme)
    host = conn.headers.get("host") or conn.url.netloc
    # Some clients name the default port (the speaker's WebSocket sends "host:443", its HTTP client doesn't). Drop it,
    # so both give the same links and the speaker doesn't download its animations again.
    default_port = {"http": ":80", "https": ":443"}.get(scheme)
    if default_port and host.endswith(default_port):
        host = host[: -len(default_port)]
    return f"{scheme}://{host}"


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


async def push_config(ctx: Context, device_id: str) -> bool:
    """Send a speaker its current config, if it's online."""
    device = ctx.db.get_device(device_id)
    return bool(device) and await ctx.hub.send(device_id, device_payload(ctx, device))


def device_theme(device: dict[str, Any]) -> dict[str, str]:
    config = DeviceConfig.model_validate(device["config"])
    return resolve_theme(config.display.theme, config.display.colors.model_dump())


def device_payload(ctx: Context, device: dict[str, Any], base: str | None = None) -> dict[str, Any]:
    """The config message for a speaker. `base` is the address it connected through (base_url)."""
    from . import local_access, relay  # they import this module
    config = DeviceConfig.model_validate(device["config"])
    theme = resolve_theme(config.display.theme, config.display.colors.model_dump())
    sizes = character_sizes(config.display.layout)
    base = base or ctx.hub.base(device["id"]) or ctx.public_url()
    background = theme["background"].lstrip("#")  # animations are flattened onto the speaker's background
    character = (ctx.characters.manifest(config.character, sizes, base, background)
                 or ctx.characters.manifest("default", sizes, base, background))
    return {
        "type": "config",
        "version": device["config_version"],
        # The speaker remembers this as a second address, after the one it was adopted through.
        "server_url": ctx.public_url(),
        "device": {"id": device["id"], "name": device["name"]},
        "config": config.model_dump(),
        "theme": theme,
        "layout": {"id": config.display.layout, "widgets": LAYOUTS[config.display.layout]["widgets"]},
        "character": character,
        # The sound pack's clips (sounds.py); null = the speaker's own chimes.
        "sounds": ctx.sounds.speaker_manifest(config.audio.sound_pack, base)
        if config.audio.sound_pack != CLASSIC else None,
        # The wake word model and its settings (wakewords.py); null = none. Detected on the speaker itself.
        "wake_word": ctx.wakewords.speaker_manifest(config.audio.wake_word, config.audio.wake_sensitivity, base),
        # The speaker's own web page: password hashes, its hotspot key and the lockout reset (local_access.py).
        "local_access": local_access.payload(ctx, device["id"]),
        # Remote connection through the relay: its address and this speaker's credentials (relay.py); null = none.
        "remote": relay.payload(ctx, device),
    }


def trim_recordings(ctx: Context, device_id: str) -> None:
    """Keep a device's newest recordings only (HARKENN_RECORDINGS_PER_DEVICE)."""
    for rel in ctx.db.trim_recordings(device_id, ctx.settings.recordings_per_device):
        (ctx.settings.recordings_dir / rel).unlink(missing_ok=True)


async def push_all(ctx: Context) -> None:
    """Send every online speaker its config, e.g. after the admin password changed."""
    for device in ctx.db.list_devices():
        if ctx.hub.online(device["id"]):
            await ctx.hub.send(device["id"], device_payload(ctx, device))
