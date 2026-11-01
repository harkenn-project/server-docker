"""Home Assistant, through MQTT discovery: every speaker shows up as a device in Home Assistant.

Per speaker:
    Activity        sensor        idle, listening, thinking, speaking, ringing (live, from the server)
    Online          binary_sensor connected to Harkenn
    Mic muted       binary_sensor
    Wi-Fi signal    sensor        dBm (diagnostic)
    Battery         sensor        when the speaker has one
    Volume          number        0-100
    Agents          select        what AI agents may do (ring and speak, speak only, do not disturb)
    Say             notify        speak a message (notify.send_message)
    Identify        button        blink the screen
    Restart         button

Topics: <prefix>/<component>/harkenn_<id>/<object>/config (discovery, retained), harkenn/status (online/offline,
the last will), harkenn/<id>/state (JSON, retained), harkenn/<id>/<object>/set (commands from Home Assistant).
Home Assistant announces itself on <prefix>/status; Harkenn then sends discovery again.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import suppress
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field, ValidationError, field_validator

from .mqtt import Broker, Client

if TYPE_CHECKING:
    from .context import Context

log = logging.getLogger("harkenn.homeassistant")

SETTINGS_KEY = "homeassistant"
BASE = "harkenn"
RETRY_S = (2, 5, 10, 30, 60)
AGENT_OPTIONS = {"ring_and_speak": "Ring and speak", "speak_only": "Speak only", "do_not_disturb": "Do not disturb"}


class HomeAssistantSettings(BaseModel):
    enabled: bool = False
    broker: str = Field("", max_length=300, description="mqtt://[user:password@]host:1883")
    discovery_prefix: str = Field("homeassistant", pattern=r"^[A-Za-z0-9_/-]{1,60}$")

    @field_validator("broker")
    @classmethod
    def _broker(cls, v: str) -> str:
        v = v.strip()
        if v:
            Broker.parse(v)  # raises ValueError with a readable message
        return v


def entities(device: dict[str, Any], prefix: str, base_url: str) -> dict[str, dict[str, Any]]:
    """Discovery topic -> config for one speaker's entities."""
    did = device["id"]
    node = f"harkenn_{did}"
    state = f"{BASE}/{did}/state"
    dev = {"identifiers": [node], "name": device["name"], "manufacturer": "Harkenn",
           "model": device.get("hardware") or "speaker", "sw_version": device.get("firmware") or None,
           "configuration_url": f"{base_url}/admin/devices/{did}"}
    available = {"availability": [{"topic": f"{BASE}/status"},
                                  {"topic": state, "value_template": "{{ 'online' if value_json.online else 'offline' }}"}],
                 "availability_mode": "all"}

    def one(component: str, obj: str, name: str, **extra: Any) -> tuple[str, dict[str, Any]]:
        cfg = {"name": name, "unique_id": f"{node}_{obj}", "device": dev, "state_topic": state, **available, **extra}
        if component in ("button", "notify"):
            cfg.pop("state_topic")
        return f"{prefix}/{component}/{node}/{obj}/config", cfg

    cmd = lambda obj: f"{BASE}/{did}/{obj}/set"  # noqa: E731
    items = [
        one("sensor", "activity", "Activity", value_template="{{ value_json.activity }}", icon="mdi:account-voice",
            device_class="enum", options=["idle", "listening", "thinking", "speaking", "ringing"]),
        one("binary_sensor", "online", "Online", value_template="{{ 'ON' if value_json.online else 'OFF' }}",
            device_class="connectivity", entity_category="diagnostic",
            availability=[{"topic": f"{BASE}/status"}]),
        one("binary_sensor", "muted", "Mic muted", value_template="{{ 'ON' if value_json.muted else 'OFF' }}",
            icon="mdi:microphone-off"),
        one("sensor", "rssi", "Wi-Fi signal", value_template="{{ value_json.rssi }}", device_class="signal_strength",
            unit_of_measurement="dBm", state_class="measurement", entity_category="diagnostic"),
        one("number", "volume", "Volume", value_template="{{ value_json.volume }}", command_topic=cmd("volume"),
            min=0, max=100, step=5, unit_of_measurement="%", icon="mdi:volume-high", mode="slider"),
        one("select", "agents", "Agents", value_template="{{ value_json.agents }}", command_topic=cmd("agents"),
            options=list(AGENT_OPTIONS.values()), icon="mdi:robot"),
        one("notify", "say", "Say", command_topic=cmd("say")),
        one("button", "identify", "Identify", command_topic=cmd("identify"), device_class="identify",
            entity_category="diagnostic"),
        one("button", "restart", "Restart", command_topic=cmd("restart"), device_class="restart",
            entity_category="config"),
    ]
    if device.get("status", {}).get("battery") is not None:
        items.append(one("sensor", "battery", "Battery", value_template="{{ value_json.battery }}",
                         device_class="battery", unit_of_measurement="%", state_class="measurement"))
    return dict(items)


def state(device: dict[str, Any], online: bool, activity: str) -> dict[str, Any]:
    status = device.get("status") or {}
    config = device.get("config") or {}
    access = (config.get("agents") or {}).get("access", "ring_and_speak")
    return {"online": online, "activity": activity if online else "idle", "muted": bool(status.get("muted")),
            "rssi": status.get("rssi"), "battery": status.get("battery"),
            "volume": (config.get("audio") or {}).get("volume", status.get("volume")),
            "agents": AGENT_OPTIONS.get(access, access)}


class Bridge:
    """Keeps Home Assistant in step with the speakers while it's enabled; reconnects by itself."""

    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx
        self.status = "off"  # shown on the Server page
        self._task: asyncio.Task | None = None
        self._client: Client | None = None
        self._published: dict[str, set[str]] = {}  # speaker -> discovery topics sent
        self._names: dict[str, tuple[str, str]] = {}  # speaker -> (name, firmware) its discovery was sent with

    def settings(self) -> HomeAssistantSettings:
        raw = self.ctx.db.get_setting(SETTINGS_KEY)
        try:
            s = HomeAssistantSettings.model_validate(raw) if raw else HomeAssistantSettings()
        except ValidationError:
            s = HomeAssistantSettings()
        if not raw and self.ctx.settings.mqtt_url:  # from the environment until the admin UI saves its own
            s = HomeAssistantSettings(enabled=True, broker=self.ctx.settings.mqtt_url)
        return s

    def save(self, s: HomeAssistantSettings) -> None:
        self.ctx.db.set_setting(SETTINGS_KEY, s.model_dump())

    def start(self) -> None:
        self.stop()
        if self.settings().enabled and self.settings().broker:
            self._task = asyncio.create_task(self._run())
        else:
            self.status = "off"

    def stop(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None

    async def restart(self) -> None:
        task = self._task
        self.stop()
        if task:
            with suppress(asyncio.CancelledError, Exception):
                await task
        self.start()

    async def _run(self) -> None:
        attempt = 0
        events = self.ctx.hub.subscribe()
        try:
            while True:
                s = self.settings()
                client = Client(Broker.parse(s.broker), f"harkenn-{int(time.time())}")
                try:
                    self.status = "connecting"
                    await client.connect(will=(f"{BASE}/status", b"offline", True))
                    self._client = client
                    self._published = {}
                    await client.publish(f"{BASE}/status", "online", retain=True)
                    await client.subscribe(f"{BASE}/+/+/set", f"{s.discovery_prefix}/status")
                    await self.publish_all()
                    self.status = "connected"
                    attempt = 0
                    log.info("connected to MQTT at %s", Broker.parse(s.broker).host)
                    while not events.empty():
                        events.get_nowait()
                    tasks = {asyncio.create_task(client.run(self._on_message)), asyncio.create_task(self._watch(events))}
                    try:
                        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                        for t in done:
                            t.result()  # raises what ended it
                    finally:
                        for t in tasks:
                            t.cancel()
                        await asyncio.gather(*tasks, return_exceptions=True)
                except asyncio.CancelledError:
                    with suppress(Exception):
                        await client.publish(f"{BASE}/status", "offline", retain=True)
                    raise
                except Exception as e:
                    self.status = f"error: {e or type(e).__name__}"
                    log.warning("MQTT: %s", e or type(e).__name__)
                finally:
                    self._client = None
                    await client.close()
                await asyncio.sleep(RETRY_S[min(attempt, len(RETRY_S) - 1)])
                attempt += 1
        finally:
            self.ctx.hub.unsubscribe(events)
            self.status = "off"

    async def _watch(self, events: asyncio.Queue) -> None:
        """Speakers changed (status, activity, config, adopted, forgotten): tell Home Assistant."""
        while True:
            event = await events.get()
            if event.get("type") in ("device", "activity") and event.get("id"):
                await self.publish_device(event["id"])
            elif event.get("type") == "adoption":  # maybe a new speaker
                for device in self.ctx.db.list_devices():
                    if device["id"] not in self._published:
                        await self.publish_device(device["id"], device)

    async def publish_all(self) -> None:
        for device in self.ctx.db.list_devices():
            await self.publish_device(device["id"], device)

    async def publish_device(self, device_id: str, device: dict[str, Any] | None = None) -> None:
        client = self._client
        if client is None:
            return
        device = device or self.ctx.db.get_device(device_id)
        prefix = self.settings().discovery_prefix
        if device is None:  # forgotten: remove its entities from Home Assistant
            for topic in self._published.pop(device_id, set()):
                await client.publish(topic, b"", retain=True)
            await client.publish(f"{BASE}/{device_id}/state", b"", retain=True)
            return
        configs = entities(device, prefix, self.ctx.public_url())
        sent = self._published.get(device_id)
        if sent is None or set(configs) != sent or self._renamed(device):
            for topic in (sent or set()) - set(configs):
                await client.publish(topic, b"", retain=True)
            for topic, cfg in configs.items():
                await client.publish(topic, json.dumps(cfg), retain=True)
            self._published[device_id] = set(configs)
        online = self.ctx.hub.online(device_id)
        await client.publish(f"{BASE}/{device_id}/state",
                             json.dumps(state(device, online, self.ctx.hub.activity(device_id))), retain=True)

    def _renamed(self, device: dict[str, Any]) -> bool:
        key = (device["name"], device.get("firmware") or "")
        changed = self._names.get(device["id"]) != key
        self._names[device["id"]] = key
        return changed

    async def _on_message(self, topic: str, payload: bytes) -> None:
        if topic == f"{self.settings().discovery_prefix}/status":
            if payload == b"online":  # Home Assistant (re)started: it needs the discovery messages again
                self._published = {}
                await self.publish_all()
            return
        parts = topic.split("/")
        if len(parts) != 4 or parts[0] != BASE or parts[3] != "set":
            return
        try:
            await self.command(parts[1], parts[2], payload.decode(errors="replace").strip())
        except Exception as e:
            log.warning("Home Assistant command %s failed: %s", topic, e)

    async def command(self, device_id: str, what: str, value: str) -> None:
        """A command from Home Assistant."""
        from .context import push_config
        from .models import DeviceConfig

        c = self.ctx
        device = c.db.get_device(device_id)
        if device is None:
            return
        if what == "volume":
            volume = max(0, min(100, round(float(value))))
            config = DeviceConfig.model_validate(device["config"])
            config.audio.volume = volume
            c.db.update_device(device_id, name=device["name"], config=config.model_dump())
            await push_config(c, device_id)
        elif what == "agents":
            access = next((k for k, v in AGENT_OPTIONS.items() if v == value or k == value), None)
            if access is None:
                return
            config = DeviceConfig.model_validate(device["config"])
            config.agents.access = access
            c.db.update_device(device_id, name=device["name"], config=config.model_dump())
        elif what == "say":
            if value:
                await c.voice.say(device_id, value[:500])
            return
        elif what in ("identify", "restart"):
            await c.hub.send(device_id, {"type": "command", "command": "identify" if what == "identify" else "reboot",
                                         "args": {}})
            return
        else:
            return
        c.hub.publish({"type": "device", "id": device_id})
