"""Endpoints used by the speakers. See docs/PROTOCOL.md."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
import time
from contextlib import suppress
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from . import __version__
from .assets import STATES
from .context import Context, base_url, device_payload, get_ctx
from .models import AdoptionRequestIn, LogBatch, LogLine, StatusIn
from .themes import ALL_CHARACTER_SIZES

log = logging.getLogger("harkenn.device")

router = APIRouter(prefix="/api/v1", tags=["device"])
assets_router = APIRouter(tags=["assets"])

HEX = r"^[0-9a-fA-F]{6}$"
POLL_INTERVAL_S = 3
PING_INTERVAL_S = 25
_background: set[asyncio.Task] = set()  # keeps fire-and-forget tasks alive until they finish
RECORDING_TYPES = {"audio/wav": "wav", "audio/x-wav": "wav", "audio/ogg": "ogg", "audio/opus": "opus",
                   "audio/l16": "pcm", "application/octet-stream": "bin"}


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _bearer(conn: Request | WebSocket) -> str:
    auth = conn.headers.get("authorization", "")
    return auth[7:].strip() if auth[:7].lower() == "bearer " else ""


def current_device(request: Request) -> dict[str, Any]:
    token = _bearer(request)
    device = get_ctx(request).db.get_device_by_token(hash_token(token)) if token else None
    if device is None:
        raise HTTPException(401, "unknown or revoked device token", headers={"WWW-Authenticate": "Bearer"})
    return device


def _client_ip(conn: Request | WebSocket) -> str | None:
    return conn.client.host if conn.client else None


# discovery and adoption

@router.get("/ping")
async def ping() -> dict[str, Any]:
    return {"ok": True, "server": "harkenn", "version": __version__, "time": time.time()}


def _adoption_out(req: dict[str, Any], now: float) -> dict[str, Any]:
    return {"request_id": req["id"], "code": req["code"], "expires_in": int(req["expires_at"] - now),
            "poll_interval_s": POLL_INTERVAL_S}


@router.post("/adoption/request")
async def request_adoption(body: AdoptionRequestIn, request: Request) -> dict[str, Any]:
    c = get_ctx(request)
    now = time.time()
    c.db.purge_adoptions(now - 86400)
    existing = c.db.pending_adoption_for(body.device_id, now)
    if existing:
        return _adoption_out(existing, now)
    pending = c.db.pending_adoptions(now)
    if len(pending) >= c.settings.max_pending_adoptions:
        raise HTTPException(429, "too many speakers waiting for adoption")
    taken = {r["code"] for r in pending}
    code = secrets.choice([f"{n:03d}" for n in range(1000) if f"{n:03d}" not in taken])
    req = {"id": secrets.token_urlsafe(16), "device_id": body.device_id, "code": code,
           "hardware": body.hardware, "firmware": body.firmware, "ip": _client_ip(request),
           "created_at": now, "expires_at": now + c.settings.adoption_ttl_s}
    c.db.create_adoption(req)
    log.info("adoption requested by %s from %s", body.device_id, req["ip"])
    c.hub.publish({"type": "adoption", "device_id": body.device_id})
    return _adoption_out(req, now)


@router.get("/adoption/{request_id}")
async def adoption_status(request_id: str, request: Request) -> dict[str, Any]:
    c = get_ctx(request)
    req = c.db.get_adoption(request_id)
    if req is None:
        raise HTTPException(404, "unknown adoption request")
    if req["state"] == "approved":
        token = c.db.collect_adoption_token(request_id)
        if token:
            return {"state": "approved", "token": token, "device_id": req["device_id"]}
        return {"state": "collected"}
    if req["state"] == "pending" and req["expires_at"] <= time.time():
        return {"state": "expired"}
    return {"state": req["state"]}


# authenticated device endpoints

def _store_logs(c: Context, device_id: str, lines: list[LogLine]) -> None:
    now = time.time()
    rows = [(line.ts if line.ts and line.ts > 1e9 else now, line.level[:1].upper() or "I", line.tag, line.message)
            for line in lines]
    if not rows:
        return
    c.db.add_logs(device_id, rows, c.settings.log_retention_per_device)
    c.hub.publish({"type": "log", "id": device_id,
                   "lines": [{"ts": ts, "level": lv, "tag": tag, "message": msg} for ts, lv, tag, msg in rows]})


def _store_status(c: Context, device_id: str, status: StatusIn, ip: str | None) -> None:
    now = time.time()
    data = status.model_dump(exclude_none=True)
    data["received_at"] = now
    c.db.touch_device(device_id, now=now, ip=data.get("ip") or ip, firmware=status.firmware, status=data)
    c.hub.publish({"type": "device", "id": device_id})


@router.get("/device/config")
async def get_config(request: Request, response: Response, device: dict = Depends(current_device)) -> Any:
    c = get_ctx(request)
    etag = f'"{device["config_version"]}"'
    if request.headers.get("if-none-match") in (etag, str(device["config_version"])):
        return Response(status_code=304, headers={"ETag": etag})
    response.headers["ETag"] = etag
    return device_payload(c, device, base_url(request))


@router.post("/device/status", status_code=204)
async def post_status(status: StatusIn, request: Request, device: dict = Depends(current_device)) -> Response:
    _store_status(get_ctx(request), device["id"], status, _client_ip(request))
    return Response(status_code=204)


@router.post("/device/logs", status_code=204)
async def post_logs(batch: LogBatch, request: Request, device: dict = Depends(current_device)) -> Response:
    _store_logs(get_ctx(request), device["id"], batch.lines)
    return Response(status_code=204)


@router.post("/device/recordings", status_code=201)
async def post_recording(request: Request, device: dict = Depends(current_device),
                         kind: str = Query("utterance", pattern=r"^[a-z_]{1,24}$")) -> dict[str, str]:
    c = get_ctx(request)
    content_type = request.headers.get("content-type", "application/octet-stream").split(";")[0].strip().lower()
    ext = RECORDING_TYPES.get(content_type)
    if ext is None:
        raise HTTPException(415, f"unsupported content type {content_type}")
    data = bytearray()
    async for chunk in request.stream():
        data += chunk
        if len(data) > c.settings.max_recording_bytes:
            raise HTTPException(413, "recording too large")
    now = time.time()
    rid = secrets.token_hex(8)
    rel = f"{device['id']}/{time.strftime('%Y%m%d-%H%M%S', time.gmtime(now))}_{rid}.{ext}"
    path = c.settings.recordings_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    c.db.add_recording({"id": rid, "device_id": device["id"], "created_at": now, "kind": kind,
                        "filename": rel, "content_type": content_type, "size": len(data)})
    c.hub.publish({"type": "recording", "id": device["id"], "recording_id": rid})
    return {"id": rid}


@router.get("/device/firmware/{fid}.bin")
async def get_firmware(fid: str, request: Request, device: dict = Depends(current_device)) -> FileResponse:
    path = get_ctx(request).firmware.path(fid)
    if path is None:
        raise HTTPException(404, "unknown firmware")
    return FileResponse(path, media_type="application/octet-stream")


# WebSocket

async def _handle_message(c: Context, device_id: str, msg: dict[str, Any], ip: str | None) -> None:
    kind = msg.get("type")
    if kind == "status":
        _store_status(c, device_id, StatusIn.model_validate(msg.get("status") or {}), ip)
    elif kind == "log":
        _store_logs(c, device_id, LogBatch.model_validate(msg).lines)
    elif kind == "event":
        name = str(msg.get("name", ""))[:64]
        data = json.dumps(msg.get("data"))[:900] if msg.get("data") is not None else ""
        _store_logs(c, device_id, [LogLine(level="I", tag="event", message=f"{name} {data}".strip())])
        c.hub.publish({"type": "event", "id": device_id, "name": name})
    elif kind == "settings":
        # changed on the speaker itself (touch screen): keep the stored config in step without pushing it back
        volume = msg.get("volume")
        if isinstance(volume, int) and not isinstance(volume, bool) and 0 <= volume <= 100:
            if c.db.note_device_setting(device_id, "audio", "volume", volume):
                c.hub.publish({"type": "device", "id": device_id})
    elif kind == "update_check":  # sent once after the speaker boots; may hit GitLab, so don't block the socket
        task = asyncio.create_task(c.updates.handle_boot_check(device_id))
        _background.add(task)
        task.add_done_callback(_background.discard)
    elif kind in ("voice_start", "voice_end", "voice_cancel", "speak_stop"):
        await c.voice.handle(device_id, msg)
    elif kind == "config_request":
        device = c.db.get_device(device_id)
        if device:
            await c.hub.send(device_id, device_payload(c, device))
    elif kind == "pong":
        c.db.touch_device(device_id, now=time.time())


async def _keepalive(c: Context, device_id: str) -> None:
    while True:
        await asyncio.sleep(PING_INTERVAL_S)
        await c.hub.send(device_id, {"type": "ping"})


@router.websocket("/device/ws")
async def device_ws(ws: WebSocket) -> None:
    c = get_ctx(ws)
    token = _bearer(ws)
    device = c.db.get_device_by_token(hash_token(token)) if token else None
    if device is None:
        await ws.close(code=4401, reason="unknown or revoked device token")
        return
    await ws.accept()
    device_id, ip = device["id"], _client_ip(ws)
    base = base_url(ws)
    await c.hub.attach(device_id, ws, base)
    c.db.touch_device(device_id, now=time.time(), ip=ip)
    c.hub.publish({"type": "device", "id": device_id})
    log.info("device %s connected from %s", device_id, ip)
    keepalive = asyncio.create_task(_keepalive(c, device_id))
    try:
        await c.hub.send(device_id, device_payload(c, device, base))
        while True:
            frame = await ws.receive()
            if frame["type"] == "websocket.disconnect":
                raise WebSocketDisconnect(frame.get("code", 1000))
            if frame.get("bytes") is not None:  # microphone audio for the current utterance
                c.voice.audio(device_id, frame["bytes"])
                continue
            msg = json.loads(frame.get("text") or "null")
            if not isinstance(msg, dict):
                continue
            try:
                await _handle_message(c, device_id, msg, ip)
            except ValidationError as e:
                log.warning("bad %s message from %s: %s", msg.get("type"), device_id, e.errors()[:3])
    except WebSocketDisconnect:
        pass
    except ValueError as e:  # malformed JSON
        log.warning("closing %s: %s", device_id, e)
        with suppress(Exception):
            await ws.close(code=1003)
    finally:
        keepalive.cancel()
        if c.hub.detach(device_id, ws):
            c.voice.disconnected(device_id)
            c.db.touch_device(device_id, now=time.time())
            c.hub.publish({"type": "device", "id": device_id})
            log.info("device %s disconnected", device_id)


# character assets (public, read-only)

@router.get("/characters/{cid}/manifest")
async def character_manifest(cid: str, request: Request, size: list[int] = Query([240]),
                             bg: str = Query("000000", pattern=HEX)) -> dict[str, Any]:
    c = get_ctx(request)
    sizes = sorted({s for s in size if s in ALL_CHARACTER_SIZES}) or [240]
    manifest = c.characters.manifest(cid, sizes, base_url(request), bg)
    if manifest is None:
        raise HTTPException(404, "unknown character")
    return manifest


def _asset(path, media_type: str, versioned: bool) -> FileResponse:
    if path is None:
        raise HTTPException(404)
    cache = "public, max-age=31536000, immutable" if versioned else "no-cache"
    return FileResponse(path, media_type=media_type, headers={"Cache-Control": cache})


@assets_router.get("/assets/characters/{cid}/{state}.gif")
async def character_preview(cid: str, state: str, request: Request, size: int = 240,
                            v: int | None = None) -> FileResponse:
    """Transparent GIF preview, for the admin UI."""
    if state not in STATES or size not in ALL_CHARACTER_SIZES:
        raise HTTPException(404)
    path = await run_in_threadpool(get_ctx(request).characters.preview_gif, cid, state, size)
    return _asset(path, "image/gif", v is not None)


@assets_router.get("/assets/characters/{cid}/{state}.kani")
async def character_animation(cid: str, state: str, request: Request, size: int = 240, v: int | None = None,
                              bg: str = Query(pattern=HEX)) -> FileResponse:
    """Speaker animation (see kani.py), flattened onto the speaker's background colour."""
    if state not in STATES or size not in ALL_CHARACTER_SIZES:
        raise HTTPException(404)
    try:
        path = await run_in_threadpool(get_ctx(request).characters.device_animation, cid, state, size, bg)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    return _asset(path, "application/octet-stream", v is not None)
