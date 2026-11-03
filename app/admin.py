"""Admin UI pages and the JSON API behind them."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import io
import json
import re
import secrets
import shutil
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, Response, StreamingResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
import numpy as np
from PIL import Image
from starlette.concurrency import run_in_threadpool

from . import __version__, about, groups, kani, local_access, relay, timezones
from .assets import STATES
from .auth import check_password, password_configured, set_password, validate_new_password
from .context import (PASSWORD_MASK, PUBLIC_URL_KEY, Context, default_config, device_payload, device_theme, push_all,
                      push_config, get_ctx, mask_secrets, restore_secrets)
from .device_api import hash_token
from .firmware import MAX_FIRMWARE_BYTES, update_args
from .homeassistant import SETTINGS_KEY as HA_SETTINGS_KEY
from .homeassistant import HomeAssistantSettings
from .mcp_server import Tokens
from . import screens
from .sounds import SLOT_LABELS, SLOTS, decode_audio, decode_upload, trim
from .voice import voiceprint
from .updates import CHANNEL_LABELS
from .wakewords import WAKE_WORDS
from .models import (AgentTokenIn, PersonUpdate, ScreenOverlayIn, ScreenSpecIn, ApproveIn, ChannelIn, PersonIn, CharacterIn, PlaySoundIn, SoundPackIn, SoundPackUpdate, CommandIn, DeviceConfig, GroupIn, GroupMembersIn, GroupUpdateIn, DeviceUpdateIn, FirmwareUpdateIn, SayIn, SpeakerPasswordIn,
                     ServerSettingsIn, VoiceSettings, VoiceSettingsIn, WeatherPlaceIn)
from .themes import LAYOUTS, THEMES
from .voice.engines import LANGUAGES, PIPER_VOICES, WHISPER_MODELS

MAX_ADOPTION_ATTEMPTS = 5

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _static_url(name: str) -> str:
    """A link to a static file that changes with its content, so browsers never keep an old script after an update."""
    digest = hashlib.sha256((Path(__file__).parent / "static" / name).read_bytes()).hexdigest()[:10]
    return f"/static/{name}?v={digest}"


templates.env.globals["static_url"] = {name: _static_url(name) for name in ("admin.css", "admin.js")}
pages = APIRouter(prefix="/admin", include_in_schema=False)


def is_admin(request: Request) -> bool:
    return request.session.get("admin") is True


def require_admin_api(request: Request) -> None:
    if not is_admin(request):
        raise HTTPException(401, "log in first")
    # Browsers can't add custom headers cross-site without a CORS preflight, which we never allow.
    if request.method not in ("GET", "HEAD") and request.headers.get("x-requested-with") != "harkenn":
        raise HTTPException(403, "missing X-Requested-With header")


api = APIRouter(prefix="/admin/api", dependencies=[Depends(require_admin_api)], include_in_schema=False)


def catalog(c: Context) -> dict[str, Any]:
    return {
        "themes": THEMES,
        "layouts": LAYOUTS,
        "states": list(STATES),
        "characters": [{"id": m["id"], "name": m["name"], "version": m["version"], "states": list(m["states"])}
                       for m in c.characters.list()],
        "sound_packs": [{"id": p["id"], "name": p["name"]} for p in c.sounds.list()],
        "wake_words": c.wakewords.catalog(),
        "relay": c.settings.relay_url,
        "timezones": timezones.names(),
        "groups": [{"id": g["id"], "name": g["name"]} for g in groups.all_groups(c.db)],
        "default_group": groups.default_id(c.db),
    }


def device_summary(c: Context, d: dict[str, Any]) -> dict[str, Any]:
    group = groups.of_device(c.db, d)
    return {k: d[k] for k in ("id", "name", "hardware", "firmware", "adopted_at", "last_seen", "last_ip",
                              "status", "config_version")} | {"online": c.hub.online(d["id"]),
                                                              "group": {"id": group["id"], "name": group["name"]},
                                                              "overrides": d.get("overrides") or [],
                                                              "character": d["config"].get("character"),
                                                              "local_access": local_access.summary(c, d["id"]),
                                                              "via": c.hub.via(d["id"]),
                                                              "timers": c.board.items.get(d["id"]),
                                                              "notifications": c.board.notes.get(d["id"], []),
                                                              "relay_ready": bool(relay.credentials(c, d["id"])["ready"])
                                                              if relay.enabled(c, d) else False}


def adoption_summary(c: Context, r: dict[str, Any], now: float) -> dict[str, Any]:
    known = c.db.get_device(r["device_id"])
    return {"id": r["id"], "device_id": r["device_id"], "hardware": r["hardware"], "firmware": r["firmware"],
            "ip": r["ip"], "created_at": r["created_at"], "expires_in": int(r["expires_at"] - now),
            "attempts": r["attempts"], "known_as": known["name"] if known else None}


def _page(request: Request, name: str, **context: Any) -> Response:
    return templates.TemplateResponse(request, name, {"admin": is_admin(request), **context})


def _login_redirect(request: Request) -> RedirectResponse:
    if not password_configured(get_ctx(request)):
        return RedirectResponse("/admin/setup", status_code=303)
    return RedirectResponse(f"/admin/login?next={request.url.path}", status_code=303)


async def _admin_password_entered(c: Context, password: str) -> None:
    """Keep the speakers' copy of the admin password (local_access.py) in step with the one just entered."""
    if await run_in_threadpool(local_access.remember_admin_password, c, password):
        await push_all(c)


# pages: authentication

@pages.get("/setup")
async def setup_page(request: Request) -> Response:
    if password_configured(get_ctx(request)):
        return RedirectResponse("/admin/login", status_code=303)
    return _page(request, "setup.html", error=None)


@pages.post("/setup")
async def setup(request: Request, password: str = Form(...), confirm: str = Form(...)) -> Response:
    c = get_ctx(request)
    if password_configured(c):
        return RedirectResponse("/admin/login", status_code=303)
    error = validate_new_password(password, confirm)
    if error:
        resp = _page(request, "setup.html", error=error)
        resp.status_code = 400
        return resp
    await run_in_threadpool(set_password, c, password)
    await _admin_password_entered(c, password)
    request.session["admin"] = True
    return RedirectResponse("/admin", status_code=303)


@pages.get("/login")
async def login_page(request: Request, next: str = "/admin") -> Response:
    if not password_configured(get_ctx(request)):
        return RedirectResponse("/admin/setup", status_code=303)
    return _page(request, "login.html", next=next, error=None)


@pages.post("/login")
async def login(request: Request, password: str = Form(...), next: str = Form("/admin")) -> Response:
    c = get_ctx(request)
    if await run_in_threadpool(check_password, c, password):
        await _admin_password_entered(c, password)
        request.session["admin"] = True
        target = next if next.startswith("/admin") and not next.startswith("//") else "/admin"
        return RedirectResponse(target, status_code=303)
    await asyncio.sleep(1)
    resp = _page(request, "login.html", next=next, error="Wrong password")
    resp.status_code = 401
    return resp


@pages.get("/password")
async def password_page(request: Request) -> Response:
    if not is_admin(request):
        return _login_redirect(request)
    return _page(request, "password.html", error=None, saved=False)


@pages.post("/password")
async def change_password(request: Request, current: str = Form(...), password: str = Form(...),
                          confirm: str = Form(...)) -> Response:
    if not is_admin(request):
        return _login_redirect(request)
    c = get_ctx(request)
    if not await run_in_threadpool(check_password, c, current):
        await asyncio.sleep(1)
        error = "Current password is wrong"
    else:
        error = validate_new_password(password, confirm)
    if error:
        resp = _page(request, "password.html", error=error, saved=False)
        resp.status_code = 400
        return resp
    await run_in_threadpool(set_password, c, password)
    await _admin_password_entered(c, password)
    return _page(request, "password.html", error=None, saved=True)


# pages


@pages.post("/logout")
async def logout(request: Request) -> Response:
    request.session.clear()
    return RedirectResponse("/admin/login", status_code=303)


@pages.get("")
async def dashboard(request: Request) -> Response:
    if not is_admin(request):
        return _login_redirect(request)
    return _page(request, "dashboard.html", catalog=catalog(get_ctx(request)))


@pages.get("/devices/{device_id}")
async def device_page(device_id: str, request: Request) -> Response:
    if not is_admin(request):
        return _login_redirect(request)
    c = get_ctx(request)
    device = c.db.get_device(device_id)
    if device is None:
        raise HTTPException(404, "unknown device")
    builds = [b for b in c.firmware.list() if not device["hardware"] or b["hardware"] == device["hardware"]]
    group = groups.of_device(c.db, device)
    return _page(request, "device.html", device=device, catalog=catalog(c), builds=builds, group=group,
                 form_data={"name": device["name"], "config": mask_secrets(_full_config(device)),
                            "group": group["id"], "overrides": device.get("overrides") or [],
                            "connected_ssid": (device.get("status") or {}).get("ssid") or "",
                            "groups": {g["id"]: {"name": g["name"], "config": mask_secrets(g["config"])}
                                       for g in groups.all_groups(c.db)}})


@pages.get("/defaults")
async def defaults_page(request: Request) -> Response:
    return RedirectResponse("/admin/groups", status_code=301)  # groups took over from the defaults


@pages.get("/groups")
async def groups_page(request: Request) -> Response:
    if not is_admin(request):
        return _login_redirect(request)
    return _page(request, "groups.html", catalog=catalog(get_ctx(request)))


@pages.get("/groups/{group_id}")
async def group_page(group_id: str, request: Request) -> Response:
    if not is_admin(request):
        return _login_redirect(request)
    c = get_ctx(request)
    group = groups.get(c.db, group_id)
    if group is None:
        raise HTTPException(404, "unknown group")
    return _page(request, "group.html", group=group, catalog=catalog(c),
                 is_default=group_id == groups.default_id(c.db),
                 form_data={"name": group["name"], "config": mask_secrets(group["config"])})


@pages.get("/characters")
async def characters_page(request: Request) -> Response:
    if not is_admin(request):
        return _login_redirect(request)
    c = get_ctx(request)
    return _page(request, "characters.html", characters=c.characters.list(), states=STATES)


@pages.get("/firmware")
async def firmware_page(request: Request) -> Response:
    if not is_admin(request):
        return _login_redirect(request)
    c = get_ctx(request)
    return _page(request, "firmware.html", builds=c.firmware.list(), devices=c.db.list_devices(),
                 updates=c.updates.state(), channels=CHANNEL_LABELS)


@pages.get("/voice")
async def voice_page(request: Request) -> Response:
    if not is_admin(request):
        return _login_redirect(request)
    c = get_ctx(request)
    return _page(request, "voice.html", voice=_voice_out(c), whisper_models=WHISPER_MODELS, piper_voices=PIPER_VOICES,
                 languages=LANGUAGES,
                 devices=c.db.list_devices())


@pages.get("/server")
async def server_page(request: Request) -> Response:
    if not is_admin(request):
        return _login_redirect(request)
    return _page(request, "server.html", server=_server_out(get_ctx(request)))


@pages.get("/agents")
async def agents_page(request: Request) -> Response:
    if not is_admin(request):
        return _login_redirect(request)
    return _page(request, "agents.html", agents=_agents_out(get_ctx(request)))


@pages.get("/sounds")
async def sounds_page(request: Request) -> Response:
    if not is_admin(request):
        return _login_redirect(request)
    c = get_ctx(request)
    return _page(request, "sounds.html", sounds=_sounds_out(c), piper_voices=PIPER_VOICES, devices=c.db.list_devices())


@pages.get("/people")
async def people_page(request: Request) -> Response:
    if not is_admin(request):
        return _login_redirect(request)
    c = get_ctx(request)
    return _page(request, "people.html", people=_people_out(c), devices=c.db.list_devices())


@pages.get("/screen")
async def screen_page(request: Request) -> Response:
    if not is_admin(request):
        return _login_redirect(request)
    c = get_ctx(request)
    return _page(request, "screen.html", overlays=c.screens.custom(), devices=c.db.list_devices(),
                 enabled=c.voice.settings().screen, prompt=c.screens.prompt())


@pages.get("/about")
async def about_page(request: Request) -> Response:
    """Links, support and credits. Open to everyone: attributions shouldn't need a password."""
    return _page(request, "about.html", docs=await run_in_threadpool(about.documents), version=__version__,
                 server_repo=about.SERVER_REPO, firmware_repo=about.FIRMWARE_REPO, support_url=about.SUPPORT_URL)


@pages.get("/events")
async def events(request: Request) -> Response:
    if not is_admin(request):
        raise HTTPException(401)
    hub = get_ctx(request).hub
    queue = hub.subscribe()

    async def stream():
        try:
            yield "retry: 3000\n\n"
            while not await request.is_disconnected():
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                    yield f"data: {json.dumps(event)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            hub.unsubscribe(queue)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@pages.get("/recordings/{recording_id}")
async def download_recording(recording_id: str, request: Request) -> Response:
    if not is_admin(request):
        raise HTTPException(401)
    c = get_ctx(request)
    rec = c.db.get_recording(recording_id)
    if rec is None:
        raise HTTPException(404)
    return FileResponse(c.settings.recordings_dir / rec["filename"], media_type=rec["content_type"],
                        filename=Path(rec["filename"]).name)


# JSON API: overview and adoption

@api.get("/overview")
async def overview(request: Request) -> dict[str, Any]:
    c = get_ctx(request)
    now = time.time()
    return {"now": now,
            "devices": [device_summary(c, d) for d in c.db.list_devices()],
            "pending": [adoption_summary(c, r, now) for r in c.db.pending_adoptions(now)]}


@api.post("/adoptions/{request_id}/approve")
async def approve_adoption(request_id: str, body: ApproveIn, request: Request) -> dict[str, Any]:
    c = get_ctx(request)
    now = time.time()
    req = c.db.get_adoption(request_id)
    if req is None or req["state"] != "pending" or req["expires_at"] <= now:
        raise HTTPException(410, "this adoption request has expired; the speaker will show a new code")
    if not hmac.compare_digest(body.code, req["code"]):
        attempts = c.db.add_adoption_attempt(request_id)
        if attempts >= MAX_ADOPTION_ATTEMPTS:
            c.db.set_adoption_state(request_id, "rejected")
            c.hub.publish({"type": "adoption", "device_id": req["device_id"]})
            raise HTTPException(403, "too many wrong codes; request rejected, the speaker will show a new code")
        raise HTTPException(403, f"wrong code ({MAX_ADOPTION_ATTEMPTS - attempts} tries left)")

    existing = c.db.get_device(req["device_id"])
    group = groups.get(c.db, body.group) if body.group else None
    if body.group and group is None:
        raise HTTPException(422, f"unknown group '{body.group}'")
    group = group or (None if existing else groups.get(c.db, groups.default_id(c.db)))
    config = existing["config"] if existing else (group["config"] if group else default_config(c).model_dump())
    name = body.name.strip() or (existing["name"] if existing else f"Speaker {req['device_id'][-4:]}")
    token = secrets.token_urlsafe(32)
    c.db.adopt_device(device_id=req["device_id"], name=name, hardware=req["hardware"], firmware=req["firmware"],
                      token_hash=hash_token(token), config=config, now=now)
    if group and not existing:  # a new speaker follows its group in everything
        c.db.set_device_group(req["device_id"], group["id"], [])
    elif group:  # one adopted again, into another group: it keeps its own settings
        _move(c, req["device_id"], group["id"])
    c.db.set_adoption_state(request_id, "approved", token=token)
    # A new speaker password every adoption: shown this once, only its hashes are kept (local_access.py)
    speaker_password = local_access.generate_password()
    await run_in_threadpool(local_access.set_speaker_password, c, req["device_id"], speaker_password)
    await c.hub.kick(req["device_id"], reason="re-adopted")
    c.hub.publish({"type": "adoption", "device_id": req["device_id"]})
    return {"device_id": req["device_id"], "name": name, "speaker_password": speaker_password}


@api.post("/adoptions/{request_id}/reject", status_code=204)
async def reject_adoption(request_id: str, request: Request) -> Response:
    c = get_ctx(request)
    req = c.db.get_adoption(request_id)
    if req is None:
        raise HTTPException(404)
    c.db.set_adoption_state(request_id, "rejected")
    c.hub.publish({"type": "adoption", "device_id": req["device_id"]})
    return Response(status_code=204)


# JSON API: devices

def _full_config(device: dict[str, Any]) -> dict[str, Any]:
    """The stored config with defaults filled in for settings added since it was saved."""
    return DeviceConfig.model_validate(device["config"]).model_dump()


def _device_or_404(c: Context, device_id: str) -> dict[str, Any]:
    device = c.db.get_device(device_id)
    if device is None:
        raise HTTPException(404, "unknown device")
    return device


def _check_character(c: Context, config: DeviceConfig) -> None:
    if c.characters.get(config.character) is None:
        raise HTTPException(422, f"unknown character '{config.character}'")
    if c.sounds.get(config.audio.sound_pack) is None:
        raise HTTPException(422, f"unknown sound pack '{config.audio.sound_pack}'")


@api.get("/devices/{device_id}")
async def get_device(device_id: str, request: Request) -> dict[str, Any]:
    c = get_ctx(request)
    device = _device_or_404(c, device_id)
    return device_summary(c, device) | {"config": mask_secrets(_full_config(device))}


@api.put("/devices/{device_id}")
async def update_device(device_id: str, body: DeviceUpdateIn, request: Request) -> dict[str, Any]:
    c = get_ctx(request)
    device = _device_or_404(c, device_id)
    try:
        config = restore_secrets(body.config, device["config"])
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    _check_character(c, config)
    if body.group and groups.get(c.db, body.group) is None:
        raise HTTPException(422, f"unknown group '{body.group}'")
    version = groups.save_device(c.db, device_id, config.model_dump(), name=body.name.strip(), group_id=body.group,
                                 overrides=body.overrides)
    pushed = await push_config(c, device_id)
    c.hub.publish({"type": "device", "id": device_id})
    return {"version": version, "pushed": pushed}


@api.delete("/devices/{device_id}", status_code=204)
async def forget_device(device_id: str, request: Request) -> Response:
    c = get_ctx(request)
    _device_or_404(c, device_id)
    await _forget(c, device_id)
    return Response(status_code=204)


async def _forget(c: Context, device_id: str) -> None:
    c.db.delete_device(device_id)
    local_access.forget(c, device_id)
    relay.forget(c, device_id)
    c.board.forget(device_id)
    shutil.rmtree(c.settings.recordings_dir / device_id, ignore_errors=True)
    await c.hub.kick(device_id)
    c.hub.publish({"type": "device", "id": device_id})


@api.post("/devices/{device_id}/factory-reset")
async def factory_reset(device_id: str, request: Request) -> dict[str, bool]:
    """Wipe the speaker (Wi-Fi, adoption, passwords, settings) and forget it here. It has to be online."""
    c = get_ctx(request)
    _device_or_404(c, device_id)
    if not await c.hub.send(device_id, {"type": "command", "command": "factory_reset", "args": {}}):
        raise HTTPException(409, "speaker is offline: hold its button 11 s to reset it, or forget it here")
    await asyncio.sleep(0.5)  # let the command reach it before the socket is closed
    await _forget(c, device_id)
    return {"sent": True}


@api.post("/devices/{device_id}/speaker-password")
async def set_speaker_password(device_id: str, body: SpeakerPasswordIn, request: Request) -> dict[str, Any]:
    """Set the speaker's own password for its web page and hotspot; empty makes a random one, returned this once."""
    c = get_ctx(request)
    _device_or_404(c, device_id)
    password = body.password or local_access.generate_password()
    error = local_access.validate_password(password)
    if error:
        raise HTTPException(422, error)
    await run_in_threadpool(local_access.set_speaker_password, c, device_id, password)
    pushed = await push_config(c, device_id)
    c.hub.publish({"type": "device", "id": device_id})
    return {"password": None if body.password else password, "pushed": pushed}


@api.get("/devices/{device_id}/speaker-password")
async def show_speaker_password(device_id: str, request: Request) -> Response:
    """The speaker password, for getting into a speaker that can't reach Harkenn."""
    c = get_ctx(request)
    _device_or_404(c, device_id)
    password = local_access.speaker_password(c, device_id)
    if password is None:
        raise HTTPException(404, "no speaker password to show: make a new one")
    return Response(json.dumps({"password": password}), media_type="application/json",
                    headers={"Cache-Control": "no-store"})


@api.post("/devices/{device_id}/timers/cancel")
async def cancel_timer(device_id: str, body: dict[str, Any], request: Request) -> dict[str, Any]:
    """Cancel a timer or alarm on the speaker: {"what": id, label, "all", "timers" or "alarms"}."""
    c = get_ctx(request)
    _device_or_404(c, device_id)
    problem = await c.voice.timer_action(device_id, {"cancel": str(body.get("what", ""))})
    if problem:
        raise HTTPException(409, problem)
    return {"sent": True}


@api.post("/devices/{device_id}/notifications/clear", status_code=204)
async def clear_notifications(device_id: str, request: Request) -> Response:
    c = get_ctx(request)
    _device_or_404(c, device_id)
    c.board.seen(device_id)
    await c.hub.send(device_id, {"type": "notifications_clear"})
    c.hub.publish({"type": "device", "id": device_id})
    return Response(status_code=204)


@api.post("/devices/{device_id}/unlock")
async def unlock_speaker(device_id: str, request: Request) -> dict[str, bool]:
    """Clear the lockout on the speaker's web page; it takes effect when the speaker next gets its config."""
    c = get_ctx(request)
    _device_or_404(c, device_id)
    local_access.unlock(c, device_id)
    return {"pushed": await push_config(c, device_id)}


@api.post("/devices/{device_id}/command")
async def send_command(device_id: str, body: CommandIn, request: Request) -> dict[str, bool]:
    c = get_ctx(request)
    _device_or_404(c, device_id)
    if body.command == "refresh_config":
        sent = await push_config(c, device_id)
    else:
        sent = await c.hub.send(device_id, {"type": "command", "command": body.command, "args": body.args})
    if not sent:
        raise HTTPException(409, "speaker is offline")
    return {"sent": True}


@api.post("/devices/{device_id}/update")
async def update_firmware(device_id: str, body: FirmwareUpdateIn, request: Request) -> dict[str, Any]:
    c = get_ctx(request)
    device = _device_or_404(c, device_id)
    fw = c.firmware.get(body.firmware)
    if fw is None:
        raise HTTPException(404, "unknown firmware")
    if device["hardware"] and fw["hardware"] != device["hardware"]:
        raise HTTPException(422, f"this firmware is for {fw['hardware']}, the speaker is {device['hardware']}")
    args = update_args(c.hub.base(device_id) or c.public_url(), fw)
    if not await c.hub.send(device_id, {"type": "command", "command": "update_firmware", "args": args}):
        raise HTTPException(409, "speaker is offline")
    return {"sent": True, "version": fw["version"]}


@api.post("/devices/{device_id}/say")
async def say(device_id: str, body: SayIn, request: Request) -> dict[str, bool]:
    c = get_ctx(request)
    _device_or_404(c, device_id)
    if not c.hub.online(device_id):
        raise HTTPException(409, "speaker is offline")
    try:
        await c.voice.say(device_id, body.text, body.voice)
    except Exception as e:  # engine missing, voice download failed, Wyoming service down, ...
        raise HTTPException(502, f"text-to-speech failed: {e}") from e
    return {"sent": True}


@api.get("/devices/{device_id}/logs")
async def device_logs(device_id: str, request: Request, limit: int = 200, before: int | None = None) -> list[dict]:
    c = get_ctx(request)
    _device_or_404(c, device_id)
    return c.db.get_logs(device_id, min(max(limit, 1), 1000), before)


@api.get("/devices/{device_id}/recordings")
async def device_recordings(device_id: str, request: Request) -> list[dict]:
    c = get_ctx(request)
    _device_or_404(c, device_id)
    return c.db.list_recordings(device_id)


# JSON API: defaults and characters

def _group_out(c: Context, g: dict[str, Any]) -> dict[str, Any]:
    members = [d for d in c.db.list_devices() if groups.of_device(c.db, d)["id"] == g["id"]]
    return {"id": g["id"], "name": g["name"], "default": g["id"] == groups.default_id(c.db),
            "config": mask_secrets(g["config"]),
            "members": [{"id": d["id"], "name": d["name"], "online": c.hub.online(d["id"]),
                         "overrides": len(d.get("overrides") or [])} for d in members]}


def _move(c: Context, device_id: str, group_id: str) -> None:
    """Into another group, keeping its own settings and networks."""
    device = c.db.get_device(device_id)
    overrides = device.get("overrides") or []
    own_wifi = [n for n in device["config"]["wifi"] if f"wifi:{n['ssid']}" in overrides]
    groups.save_device(c.db, device_id, {**device["config"], "wifi": own_wifi}, group_id=group_id,
                       overrides=overrides)


async def _push_each(c: Context, device_ids: list[str]) -> int:
    pushed = 0
    for device_id in device_ids:
        pushed += await push_config(c, device_id)
        c.hub.publish({"type": "device", "id": device_id})
    return pushed


@api.get("/groups")
async def list_groups(request: Request) -> list[dict[str, Any]]:
    c = get_ctx(request)
    return [_group_out(c, g) for g in groups.all_groups(c.db)]


@api.post("/groups", status_code=201)
async def create_group(body: GroupIn, request: Request) -> dict[str, Any]:
    c = get_ctx(request)
    try:
        return _group_out(c, groups.create(c.db, body.name, body.copy_from))
    except ValueError as e:
        raise HTTPException(422, str(e)) from e


@api.get("/groups/{group_id}")
async def get_group(group_id: str, request: Request) -> dict[str, Any]:
    c = get_ctx(request)
    group = groups.get(c.db, group_id)
    if group is None:
        raise HTTPException(404, "unknown group")
    return _group_out(c, group)


@api.put("/groups/{group_id}")
async def update_group(group_id: str, body: GroupUpdateIn, request: Request) -> dict[str, Any]:
    """New settings for a group: every speaker in it gets them, except the settings it has its own."""
    c = get_ctx(request)
    group = groups.get(c.db, group_id)
    if group is None:
        raise HTTPException(404, "unknown group")
    try:
        config = restore_secrets(body.config, group["config"])
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    _check_character(c, config)
    changed = groups.save(c.db, group_id, name=body.name, config=config.model_dump())
    return {"saved": True, "speakers": len(changed), "pushed": await _push_each(c, changed)}


@api.delete("/groups/{group_id}", status_code=204)
async def delete_group(group_id: str, request: Request) -> Response:
    c = get_ctx(request)
    try:
        changed = groups.delete(c.db, group_id)
    except KeyError:
        raise HTTPException(404, "unknown group") from None
    except ValueError as e:
        raise HTTPException(409, str(e)) from e
    await _push_each(c, changed)
    return Response(status_code=204)


@api.post("/groups/{group_id}/default")
async def make_default_group(group_id: str, request: Request) -> dict[str, bool]:
    c = get_ctx(request)
    try:
        groups.set_default(c.db, group_id)
    except KeyError:
        raise HTTPException(404, "unknown group") from None
    return {"saved": True}


@api.post("/groups/{group_id}/members")
async def move_to_group(group_id: str, body: GroupMembersIn, request: Request) -> dict[str, int]:
    """Move speakers into a group. They keep the settings they have their own and follow the group for the rest."""
    c = get_ctx(request)
    if groups.get(c.db, group_id) is None:
        raise HTTPException(404, "unknown group")
    moved = []
    for device_id in dict.fromkeys(body.devices):
        if c.db.get_device(device_id) is None:
            raise HTTPException(404, f"unknown speaker {device_id}")
        _move(c, device_id, group_id)
        moved.append(device_id)
    return {"moved": len(moved), "pushed": await _push_each(c, moved)}


@api.get("/catalog")
async def get_catalog(request: Request) -> dict[str, Any]:
    return catalog(get_ctx(request))


def _server_out(c: Context) -> dict[str, Any]:
    return {"public_url": c.db.get_setting(PUBLIC_URL_KEY) or "", "default_public_url": c.settings.public_url,
            "effective_public_url": c.public_url(), "mdns": c.settings.mdns}


def _agents_out(c: Context) -> dict[str, Any]:
    tokens = [{k: v for k, v in t.items() if k != "hash"} for t in Tokens(c).list()]
    return {"mcp_url": f"{c.public_url()}/mcp", "tokens": tokens, "env_token": bool(c.settings.mcp_token)}


@api.get("/agents")
async def get_agents(request: Request) -> dict[str, Any]:
    return _agents_out(get_ctx(request))


@api.post("/agents/tokens", status_code=201)
async def create_agent_token(body: AgentTokenIn, request: Request) -> dict[str, Any]:
    """A new MCP token. The token itself is only in this response."""
    entry, token = Tokens(get_ctx(request)).create(body.name.strip())
    return {"id": entry["id"], "name": entry["name"], "token": token}


@api.delete("/agents/tokens/{tid}", status_code=204)
async def delete_agent_token(tid: str, request: Request) -> Response:
    if not Tokens(get_ctx(request)).delete(tid):
        raise HTTPException(404)
    return Response(status_code=204)


def _sounds_out(c: Context) -> dict[str, Any]:
    used: dict[str, list[str]] = {}
    for d in c.db.list_devices():
        pack = DeviceConfig.model_validate(d["config"]).audio.sound_pack
        used.setdefault(pack, []).append(d["name"])
    return {"slots": SLOT_LABELS, "packs": [dict(p, used_by=used.get(p["id"], [])) for p in c.sounds.list()]}


async def _push_pack(c: Context, pid: str) -> None:
    """Speakers using a pack that changed get their new clips."""
    for d in c.db.list_devices():
        if DeviceConfig.model_validate(d["config"]).audio.sound_pack == pid:
            await push_config(c, d["id"])


async def _speak_lines(c: Context, pid: str, voice: str, lines: dict[str, str]) -> None:
    for slot, text in lines.items():
        if slot not in SLOTS:
            continue
        pcm = b""
        if text:
            pcm = trim(await c.voice.synthesize(text, voice))
        await run_in_threadpool(c.sounds.set_clip, pid, slot, pcm)


@api.get("/sounds")
async def list_sounds(request: Request) -> dict[str, Any]:
    return _sounds_out(get_ctx(request))


@api.post("/sounds", status_code=201)
async def create_sound_pack(body: SoundPackIn, request: Request) -> dict[str, Any]:
    """A voiced pack (text per slot, spoken by a Piper voice) or a custom one (uploads, copied from a pack)."""
    c = get_ctx(request)
    if body.kind == "voiced" and body.voice not in PIPER_VOICES:
        raise HTTPException(422, "choose one of the voices")
    try:
        pack = c.sounds.create(body.name.strip(), body.kind, voice=body.voice, lines=body.lines,
                               copy_from=body.copy_from if body.kind == "custom" else "")
    except KeyError as e:
        raise HTTPException(422, f"no sound pack '{body.copy_from}'") from e
    if body.kind == "voiced":
        try:
            await _speak_lines(c, pack["id"], body.voice, pack["lines"])
        except Exception as e:  # e.g. the voice couldn't be downloaded: keep the pack, report it
            raise HTTPException(502, f"pack made, but the voice failed: {e}") from e
    return c.sounds.get(pack["id"])


@api.put("/sounds/{pid}")
async def update_sound_pack(pid: str, body: SoundPackUpdate, request: Request) -> dict[str, Any]:
    c = get_ctx(request)
    pack = c.sounds.get(pid)
    if pack is None or pack["kind"] == "builtin":
        raise HTTPException(404)
    if body.voice is not None and body.voice not in PIPER_VOICES:
        raise HTTPException(422, "choose one of the voices")
    c.sounds.update(pid, name=body.name and body.name.strip())
    if pack["kind"] == "voiced" and (body.voice or body.lines is not None):
        voice = body.voice or pack["voice"]
        lines = {s: (body.lines or {}).get(s, pack["lines"].get(s, "")) for s in SLOTS}
        changed = lines if body.voice and body.voice != pack["voice"] else             {s: t for s, t in lines.items() if t != pack["lines"].get(s, "")}
        c.sounds.update(pid, voice=voice, lines=lines)
        await _speak_lines(c, pid, voice, changed)
    await _push_pack(c, pid)
    return c.sounds.get(pid)


@api.put("/sounds/{pid}/{slot}")
async def upload_sound(pid: str, slot: str, file: UploadFile, request: Request) -> dict[str, Any]:
    """A custom pack's clip for one slot: any common audio format, trimmed to at most 5 s."""
    c = get_ctx(request)
    pack = c.sounds.get(pid)
    if pack is None or pack["kind"] != "custom" or slot not in SLOTS:
        raise HTTPException(404)
    data = await file.read(c.settings.max_upload_bytes + 1)
    if len(data) > c.settings.max_upload_bytes:
        raise HTTPException(413, "that file is too big")
    try:
        pcm = await run_in_threadpool(decode_upload, data)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    pack = await run_in_threadpool(c.sounds.set_clip, pid, slot, pcm)
    await _push_pack(c, pid)
    return pack


@api.delete("/sounds/{pid}/{slot}")
async def silence_sound(pid: str, slot: str, request: Request) -> dict[str, Any]:
    c = get_ctx(request)
    pack = c.sounds.get(pid)
    if pack is None or pack["kind"] == "builtin" or slot not in SLOTS:
        raise HTTPException(404)
    pack = c.sounds.set_clip(pid, slot, b"")
    await _push_pack(c, pid)
    return pack


@api.delete("/sounds/{pid}", status_code=204)
async def delete_sound_pack(pid: str, request: Request) -> Response:
    c = get_ctx(request)
    if any(DeviceConfig.model_validate(d["config"]).audio.sound_pack == pid for d in c.db.list_devices()):
        raise HTTPException(409, "a speaker uses this pack: choose another one for it first")
    if not c.sounds.delete(pid):
        raise HTTPException(404)
    return Response(status_code=204)


@api.post("/devices/{device_id}/sound")
async def play_sound(device_id: str, body: PlaySoundIn, request: Request) -> dict[str, bool]:
    """Play a pack's clip on a speaker, to try it."""
    c = get_ctx(request)
    _device_or_404(c, device_id)
    pcm = await run_in_threadpool(c.sounds.clip, body.pack, body.slot)
    if pcm is None:
        raise HTTPException(404)
    if not c.hub.online(device_id):
        raise HTTPException(409, "that speaker is offline")
    if pcm:
        await c.voice.say(device_id, "", pcm=pcm)
    return {"played": bool(pcm)}


def _people_out(c: Context) -> dict[str, Any]:
    return {"available": voiceprint.available(), "sentences": voiceprint.ENROLL_SENTENCES,
            "threshold": c.voice.settings().voiceprint_threshold,
            "people": [{"id": p["id"], "name": p["name"], "phonetic": p.get("phonetic", ""),
                        "gender": p.get("gender", ""), "created_at": p["created_at"], "samples": len(p["samples"])}
                       for p in c.voice.voiceprints.people()]}


def _person_or_404(c: Context, pid: str) -> dict[str, Any]:
    p = c.voice.voiceprints.person(pid)
    if p is None:
        raise HTTPException(404)
    return p


@api.get("/people")
async def list_people(request: Request) -> dict[str, Any]:
    return _people_out(get_ctx(request))


@api.post("/people", status_code=201)
async def add_person(body: PersonIn, request: Request) -> dict[str, Any]:
    """Someone who agreed to have a voiceprint kept. Then add samples (say a few sentences)."""
    c = get_ctx(request)
    if not voiceprint.available():
        raise HTTPException(409, "voice recognition isn't available on this server")
    p = c.voice.voiceprints.add_person(body.name.strip(), body.phonetic.strip(), body.gender.strip())
    return {"id": p["id"], "name": p["name"], "phonetic": p["phonetic"], "gender": p["gender"], "samples": 0}


@api.patch("/people/{pid}")
async def update_person(pid: str, body: PersonUpdate, request: Request) -> dict[str, Any]:
    c = get_ctx(request)
    _person_or_404(c, pid)
    c.voice.voiceprints.update_person(pid, **{k: v.strip() for k, v in body.model_dump(exclude_none=True).items()})
    return _people_out(c)


@api.delete("/people/{pid}", status_code=204)
async def delete_person(pid: str, request: Request) -> Response:
    if not get_ctx(request).voice.voiceprints.delete_person(pid):
        raise HTTPException(404)
    return Response(status_code=204)


@api.delete("/people/{pid}/samples")
async def clear_person_samples(pid: str, request: Request) -> dict[str, Any]:
    c = get_ctx(request)
    _person_or_404(c, pid)
    p = c.voice.voiceprints.clear_samples(pid)
    return {"id": pid, "samples": len(p["samples"])}


async def _enroll(c: Context, pid: str, pcm: bytes) -> dict[str, Any]:
    try:
        p = await run_in_threadpool(c.voice.voiceprints.add_sample, pid, pcm)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    return {"id": pid, "samples": len(p["samples"])}


@api.post("/people/{pid}/samples")
async def add_person_sample(pid: str, file: UploadFile, request: Request) -> dict[str, Any]:
    """A recording made in the browser (any format PyAV reads)."""
    c = get_ctx(request)
    _person_or_404(c, pid)
    data = await file.read(c.settings.max_upload_bytes + 1)
    if len(data) > c.settings.max_upload_bytes:
        raise HTTPException(413, "that recording is too big")
    try:
        pcm = await run_in_threadpool(decode_audio, data)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    return await _enroll(c, pid, trim(pcm, 0.005)[: 30 * 32000] or pcm)


@api.post("/people/{pid}/record/{device_id}")
async def record_person_sample(pid: str, device_id: str, request: Request) -> dict[str, Any]:
    """Record a sample on a speaker: it asks the person to read a sentence, then listens."""
    c = get_ctx(request)
    p = _person_or_404(c, pid)
    _device_or_404(c, device_id)
    if not c.hub.online(device_id):
        raise HTTPException(409, "that speaker is offline")
    sentence = voiceprint.ENROLL_SENTENCES[len(p["samples"]) % len(voiceprint.ENROLL_SENTENCES)]
    sample = c.voice.expect_sample(device_id)
    prompt = f"{voiceprint.spoken_name(p)}, after the tone, please read the sentence shown on the People page, or say anything you like."
    pcm = await c.voice.synthesize(prompt)
    await c.voice.say(device_id, prompt, listen=True, pcm=pcm)
    try:
        recorded = await asyncio.wait_for(sample, len(pcm) / 32000 + 45)
    except asyncio.TimeoutError:
        raise HTTPException(408, "nothing was recorded") from None
    if not recorded:
        raise HTTPException(422, "nothing was recorded")
    out = await _enroll(c, pid, recorded)
    return {**out, "sentence": sentence}


@api.post("/people/identify")
async def identify_voice(file: UploadFile, request: Request) -> dict[str, Any]:
    """Try recognition on a recording made in the browser."""
    c = get_ctx(request)
    data = await file.read(c.settings.max_upload_bytes + 1)
    try:
        pcm = await run_in_threadpool(decode_audio, data)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    who = await run_in_threadpool(c.voice.voiceprints.identify, pcm, c.voice.settings().voiceprint_threshold)
    return {"who": who}


def _wakewords_out(c: Context) -> dict[str, Any]:
    used: dict[str, list[str]] = {}
    for d in c.db.list_devices():
        used.setdefault(DeviceConfig.model_validate(d["config"]).audio.wake_word, []).append(d["name"])
    return {"wake_words": [{"id": wid, "name": name, "custom": wid not in WAKE_WORDS, "used_by": used.get(wid, [])}
                           for wid, name in c.wakewords.catalog().items()]}


@api.get("/wakewords")
async def list_wake_words(request: Request) -> dict[str, Any]:
    return _wakewords_out(get_ctx(request))


@api.post("/wakewords", status_code=201)
async def add_wake_word(request: Request, model: UploadFile, manifest: UploadFile | None = None,
                        name: str = Form("")) -> dict[str, Any]:
    """A custom wake word: a microWakeWord streaming model (.tflite), with its .json if there is one."""
    c = get_ctx(request)
    data = await model.read(512 * 1024 + 1)
    meta = None
    if manifest is not None:
        try:
            meta = json.loads((await manifest.read(64 * 1024)).decode())
        except ValueError as e:
            raise HTTPException(422, "that .json isn't valid") from e
    try:
        wid = c.wakewords.add_custom(data, meta, name)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    for d in c.db.list_devices():  # speakers using an updated model get it
        if DeviceConfig.model_validate(d["config"]).audio.wake_word == wid:
            await push_config(c, d["id"])
    return {"id": wid, **_wakewords_out(c)}


@api.delete("/wakewords/{wid}", status_code=204)
async def delete_wake_word(wid: str, request: Request) -> Response:
    c = get_ctx(request)
    if any(DeviceConfig.model_validate(d["config"]).audio.wake_word == wid for d in c.db.list_devices()):
        raise HTTPException(409, "a speaker uses this wake word: choose another one for it first")
    if not c.wakewords.delete_custom(wid):
        raise HTTPException(404)
    return Response(status_code=204)


@api.get("/server")
async def get_server(request: Request) -> dict[str, Any]:
    return _server_out(get_ctx(request))


def _weather_out(c: Context) -> dict[str, Any]:
    place = c.weather.place() or {}
    return {"place": place.get("name", ""), "units": place.get("units", "metric"),
            "latitude": place.get("latitude"), "longitude": place.get("longitude")}


@api.get("/weather")
async def get_weather_settings(request: Request) -> dict[str, Any]:
    c = get_ctx(request)
    out = _weather_out(c)
    out["now"] = await run_in_threadpool(c.weather.summary) if out["place"] else ""
    return out


@api.put("/weather")
async def put_weather_settings(body: WeatherPlaceIn, request: Request) -> dict[str, Any]:
    """Where the live weather is for (looked up by name with Open-Meteo); empty turns it off."""
    c = get_ctx(request)
    try:
        found = await run_in_threadpool(c.weather.set_place, body.place, body.units)
    except Exception as e:
        raise HTTPException(502, f"couldn't reach the weather service: {e}") from e
    if found is None:
        raise HTTPException(422, f'No place called "{body.place}" found')
    out = _weather_out(c)
    out["now"] = await run_in_threadpool(c.weather.summary) if out["place"] else ""
    return out


@api.put("/server")
async def put_server(body: ServerSettingsIn, request: Request) -> dict[str, Any]:
    """Change the public URL. Speakers keep the address they were adopted through and learn this one as a second
    address (it reaches them in their next config), so a wrong value can't strand them."""
    c = get_ctx(request)
    c.db.set_setting(PUBLIC_URL_KEY, body.public_url)
    await request.app.state.announcer.update(c.public_url())
    for device in c.db.list_devices():
        await push_config(c, device["id"])
    return _server_out(c)


def _voice_out(c: Context) -> dict[str, Any]:
    out = c.voice.settings().model_dump()
    if out["assistant_key"]:
        out["assistant_key"] = PASSWORD_MASK
    env = c.settings
    out["from_environment"] = {"assistant_url": env.assistant_url, "assistant_api": env.assistant_api,
                               "assistant_model": env.assistant_model, "assistant_key": bool(env.assistant_key)}
    return out


@api.get("/voice")
async def get_voice(request: Request) -> dict[str, Any]:
    return _voice_out(get_ctx(request))


@api.put("/voice")
async def put_voice(body_in: VoiceSettingsIn, request: Request) -> dict[str, Any]:
    c = get_ctx(request)
    body = VoiceSettings.model_validate(body_in.model_dump(exclude={"from_environment"}))
    if body.assistant_key == PASSWORD_MASK:
        body.assistant_key = c.voice.settings().assistant_key
    c.voice.save_settings(body)
    return _voice_out(c)


@api.get("/firmware")
async def list_firmware(request: Request) -> list[dict[str, Any]]:
    return get_ctx(request).firmware.list()


@api.post("/firmware", status_code=201)
async def upload_firmware(file: UploadFile, request: Request) -> dict[str, Any]:
    data = await file.read(MAX_FIRMWARE_BYTES + 1)
    try:
        return await run_in_threadpool(get_ctx(request).firmware.add, data)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e


@api.post("/firmware/check")
async def check_firmware_updates(request: Request) -> dict[str, Any]:
    return await get_ctx(request).updates.check()


@api.put("/firmware/channel")
async def set_firmware_channel(body: ChannelIn, request: Request) -> dict[str, Any]:
    """Follow another release channel, and check it now."""
    return await get_ctx(request).updates.set_channel(body.channel)


@api.delete("/firmware/{fid}", status_code=204)
async def delete_firmware(fid: str, request: Request) -> Response:
    if not get_ctx(request).firmware.delete(fid):
        raise HTTPException(404)
    return Response(status_code=204)


@api.post("/characters", status_code=201)
async def create_character(body: CharacterIn, request: Request) -> dict[str, Any]:
    try:
        return get_ctx(request).characters.create(body.id, body.name.strip())
    except FileExistsError as e:
        raise HTTPException(409, str(e)) from e


@api.delete("/characters/{cid}", status_code=204)
async def delete_character(cid: str, request: Request) -> Response:
    c = get_ctx(request)
    if cid == "default":
        raise HTTPException(400, "the default character can't be deleted")
    users = [d["name"] for d in c.db.list_devices() if d["config"].get("character") == cid]
    if users:
        raise HTTPException(409, f"still used by: {', '.join(users)}")
    if c.characters.get(cid) is None:
        raise HTTPException(404)
    c.characters.delete(cid)
    return Response(status_code=204)


@api.post("/characters/{cid}/states/{state}")
async def upload_state(cid: str, state: str, file: UploadFile, request: Request) -> dict[str, Any]:
    c = get_ctx(request)
    if state not in STATES:
        raise HTTPException(404, "unknown state")
    if c.characters.get(cid) is None:
        raise HTTPException(404, "unknown character")
    data = await file.read(c.settings.max_upload_bytes + 1)
    if len(data) > c.settings.max_upload_bytes:
        raise HTTPException(413, "file too large")
    try:
        meta = await run_in_threadpool(c.characters.import_state, cid, state, data)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    for device in c.db.list_devices():
        if device["config"].get("character") == cid:
            await push_config(c, device["id"])
    return meta


# screen: the assistant's pictures and buttons (screens.py)

@api.get("/screen")
async def get_screen(request: Request) -> dict[str, Any]:
    c = get_ctx(request)
    return {"overlays": c.screens.custom(), "prompt": c.screens.prompt()}


@api.put("/screen/overlays/{name}")
async def put_screen_overlay(name: str, body: ScreenOverlayIn, request: Request) -> dict[str, Any]:
    if not re.fullmatch(r"[a-z0-9][a-z0-9_]{0,31}", name):
        raise HTTPException(422, "names are lowercase letters, digits and _")
    try:
        get_ctx(request).screens.save_custom(name, body.model_dump())
    except screens.ScreenError as e:
        raise HTTPException(422, str(e)) from e
    return await get_screen(request)


@api.delete("/screen/overlays/{name}", status_code=204)
async def delete_screen_overlay(name: str, request: Request) -> Response:
    if not get_ctx(request).screens.delete_custom(name):
        raise HTTPException(404, "unknown overlay")
    return Response(status_code=204)


@api.post("/screen/preview")
async def preview_screen(body: ScreenSpecIn, request: Request, device: str = "") -> dict[str, Any]:
    """Draws a screen as a speaker would show it: a PNG of its first frame, its tappable areas, and any problems."""
    c = get_ctx(request)
    d = c.db.get_device(device) if device else None
    theme = device_theme(d) if d else device_theme({"config": default_config(c).model_dump()})
    try:
        screen, warnings = await run_in_threadpool(c.screens.render, body.spec, theme)
    except screens.ScreenError as e:
        raise HTTPException(422, str(e)) from e
    first = kani.decode(screen.kani)[0]
    rgb = np.stack([(first >> 11 & 0x1F) * 255 // 31, (first >> 5 & 0x3F) * 255 // 63, (first & 0x1F) * 255 // 31],
                   axis=-1).astype(np.uint8).reshape(screens.SIZE, screens.SIZE, 3)
    buf = io.BytesIO()
    Image.fromarray(rgb).save(buf, "PNG")
    return {"png": base64.b64encode(buf.getvalue()).decode(), "regions": screen.regions, "warnings": warnings,
            "bytes": len(screen.kani)}


@api.post("/devices/{device_id}/screen")
async def show_screen(device_id: str, body: ScreenSpecIn, request: Request) -> dict[str, Any]:
    c = get_ctx(request)
    _device_or_404(c, device_id)
    if not c.hub.online(device_id):
        raise HTTPException(409, "speaker is offline")
    return {"sent": True, "warnings": await c.voice.show_screen(device_id, body.spec)}


# Home Assistant (homeassistant.py)

def _mask_broker(url: str) -> str:
    """The broker address without its password, for the browser."""
    u = urlsplit(url)
    if not u.password:
        return url
    netloc = f"{u.username}:{PASSWORD_MASK}@{u.hostname}" + (f":{u.port}" if u.port else "")
    return urlunsplit(u._replace(netloc=netloc))


def _homeassistant_out(c: Context) -> dict[str, Any]:
    s = c.homeassistant.settings()
    return {**s.model_dump(), "broker": _mask_broker(s.broker), "status": c.homeassistant.status,
            "from_environment": bool(c.settings.mqtt_url) and not c.db.get_setting(HA_SETTINGS_KEY)}


@api.get("/homeassistant")
async def get_homeassistant(request: Request) -> dict[str, Any]:
    return _homeassistant_out(get_ctx(request))


@api.put("/homeassistant")
async def put_homeassistant(body: dict[str, Any], request: Request) -> dict[str, Any]:
    c = get_ctx(request)
    body = {k: v for k, v in body.items() if k in ("enabled", "broker", "discovery_prefix")}
    if PASSWORD_MASK in str(body.get("broker", "")):  # unchanged: keep the stored password
        old = urlsplit(c.homeassistant.settings().broker)
        new = urlsplit(body["broker"])
        if new.hostname == old.hostname and new.username == old.username:
            body["broker"] = urlunsplit(new._replace(netloc=old.netloc))
    try:
        s = HomeAssistantSettings.model_validate(body)
    except ValidationError as e:
        raise HTTPException(422, "; ".join(err["msg"].removeprefix("Value error, ") for err in e.errors())) from e
    if s.enabled and not s.broker:
        raise HTTPException(422, "enter the MQTT broker's address")
    c.homeassistant.save(s)
    await c.homeassistant.restart()
    for _ in range(30):  # a moment for the first connection, so the page can show how it went
        if c.homeassistant.status != "connecting":
            break
        await asyncio.sleep(0.1)
    return _homeassistant_out(c)
