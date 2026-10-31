"""Admin UI pages and the JSON API behind them."""
from __future__ import annotations

import asyncio
import hmac
import json
import secrets
import shutil
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, Response, StreamingResponse
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from .assets import STATES
from .auth import check_password, password_configured, set_password, validate_new_password
from .context import (PASSWORD_MASK, PUBLIC_URL_KEY, Context, default_config, device_payload, get_ctx, mask_secrets,
                      restore_secrets)
from .device_api import hash_token
from .firmware import MAX_FIRMWARE_BYTES, update_args
from .models import (ApproveIn, CharacterIn, CommandIn, DefaultsIn, DeviceConfig, DeviceUpdateIn, FirmwareUpdateIn, SayIn,
                     ServerSettingsIn, VoiceSettings)
from .themes import LAYOUTS, THEMES
from .voice.engines import LANGUAGES, PIPER_VOICES, WHISPER_MODELS

MAX_ADOPTION_ATTEMPTS = 5

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
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
    }


def device_summary(c: Context, d: dict[str, Any]) -> dict[str, Any]:
    return {k: d[k] for k in ("id", "name", "hardware", "firmware", "adopted_at", "last_seen", "last_ip",
                              "status", "config_version")} | {"online": c.hub.online(d["id"]),
                                                              "character": d["config"].get("character")}


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
    request.session["admin"] = True
    return RedirectResponse("/admin", status_code=303)


@pages.get("/login")
async def login_page(request: Request, next: str = "/admin") -> Response:
    if not password_configured(get_ctx(request)):
        return RedirectResponse("/admin/setup", status_code=303)
    return _page(request, "login.html", next=next, error=None)


@pages.post("/login")
async def login(request: Request, password: str = Form(...), next: str = Form("/admin")) -> Response:
    if await run_in_threadpool(check_password, get_ctx(request), password):
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
    return _page(request, "dashboard.html")


@pages.get("/devices/{device_id}")
async def device_page(device_id: str, request: Request) -> Response:
    if not is_admin(request):
        return _login_redirect(request)
    c = get_ctx(request)
    device = c.db.get_device(device_id)
    if device is None:
        raise HTTPException(404, "unknown device")
    builds = [b for b in c.firmware.list() if not device["hardware"] or b["hardware"] == device["hardware"]]
    return _page(request, "device.html", device=device, catalog=catalog(c), builds=builds,
                 form_data={"name": device["name"], "config": mask_secrets(_full_config(device))})


@pages.get("/defaults")
async def defaults_page(request: Request) -> Response:
    if not is_admin(request):
        return _login_redirect(request)
    c = get_ctx(request)
    return _page(request, "defaults.html", catalog=catalog(c),
                 form_data={"config": mask_secrets(default_config(c).model_dump())})


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
                 updates=c.updates.state())


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
    config = existing["config"] if existing else default_config(c).model_dump()
    name = body.name.strip() or (existing["name"] if existing else f"Speaker {req['device_id'][-4:]}")
    token = secrets.token_urlsafe(32)
    c.db.adopt_device(device_id=req["device_id"], name=name, hardware=req["hardware"], firmware=req["firmware"],
                      token_hash=hash_token(token), config=config, now=now)
    c.db.set_adoption_state(request_id, "approved", token=token)
    await c.hub.kick(req["device_id"], reason="re-adopted")
    c.hub.publish({"type": "adoption", "device_id": req["device_id"]})
    return {"device_id": req["device_id"], "name": name}


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


async def push_config(c: Context, device_id: str) -> bool:
    device = c.db.get_device(device_id)
    return bool(device) and await c.hub.send(device_id, device_payload(c, device))


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
    version = c.db.update_device(device_id, name=body.name.strip(), config=config.model_dump())
    pushed = await push_config(c, device_id)
    c.hub.publish({"type": "device", "id": device_id})
    return {"version": version, "pushed": pushed}


@api.delete("/devices/{device_id}", status_code=204)
async def forget_device(device_id: str, request: Request) -> Response:
    c = get_ctx(request)
    _device_or_404(c, device_id)
    c.db.delete_device(device_id)
    shutil.rmtree(c.settings.recordings_dir / device_id, ignore_errors=True)
    await c.hub.kick(device_id)
    c.hub.publish({"type": "device", "id": device_id})
    return Response(status_code=204)


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

@api.get("/defaults")
async def get_defaults(request: Request) -> dict[str, Any]:
    return {"config": mask_secrets(default_config(get_ctx(request)).model_dump())}


@api.put("/defaults")
async def put_defaults(body: DefaultsIn, request: Request) -> dict[str, Any]:
    c = get_ctx(request)
    try:
        config = restore_secrets(body.config, default_config(c).model_dump())
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    _check_character(c, config)
    c.db.set_setting("default_config", config.model_dump())
    return {"saved": True}


@api.get("/catalog")
async def get_catalog(request: Request) -> dict[str, Any]:
    return catalog(get_ctx(request))


def _server_out(c: Context) -> dict[str, Any]:
    return {"public_url": c.db.get_setting(PUBLIC_URL_KEY) or "", "default_public_url": c.settings.public_url,
            "effective_public_url": c.public_url(), "mdns": c.settings.mdns}


@api.get("/server")
async def get_server(request: Request) -> dict[str, Any]:
    return _server_out(get_ctx(request))


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
    return out


@api.get("/voice")
async def get_voice(request: Request) -> dict[str, Any]:
    return _voice_out(get_ctx(request))


@api.put("/voice")
async def put_voice(body: VoiceSettings, request: Request) -> dict[str, Any]:
    c = get_ctx(request)
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
