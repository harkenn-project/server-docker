"""Firmware releases from Git.

Speakers ask once per boot (``update_check`` over the WebSocket). Kotodama then looks at the latest release of a
GitLab project (``KOTODAMA_UPDATE_REPO``, by default this project) for a ``firmware.bin`` asset, reusing a check
younger than CACHE_S, downloads a new release and checks it like a manual upload (firmware.py). If it is newer than
the speaker's firmware, the speaker shows "Update available" with Install / Dismiss. The admin UI can also check on
demand. Releases are made by the Jenkins job in jenkins/release-firmware.xml whenever FW_VERSION in
firmware/platformio.ini changes.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import urllib.error
import urllib.request
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from starlette.concurrency import run_in_threadpool

from .firmware import MAX_FIRMWARE_BYTES, update_args

if TYPE_CHECKING:
    from .context import Context

log = logging.getLogger("kotodama.updates")

STATE_KEY = "firmware_updates"
CACHE_S = 600  # speakers booting together share one check
ASSET_NAME = "firmware.bin"


def version_key(version: str) -> tuple[int, ...]:
    """0.4.10 > 0.4.9; anything after a '-' or '+' (pre-release, build) is ignored."""
    core = re.split(r"[-+]", version.strip().lstrip("vV"), maxsplit=1)[0]
    return tuple(int(p) for p in re.findall(r"\d+", core)[:3])


def is_newer(candidate: str, current: str | None) -> bool:
    if not current:
        return True
    return version_key(candidate) > version_key(current)


def _get(url: str, limit: int) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "kotodama-update-check"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = resp.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"{url}: response too large")
    return data


class UpdateChecker:
    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx
        s = ctx.settings
        self.repo = s.update_repo
        self.api = f"{s.update_gitlab_url.rstrip('/')}/api/v4/projects/{quote(self.repo, safe='')}"
        self._lock = asyncio.Lock()

    # network, replaced in tests

    def fetch_latest_release(self) -> dict[str, Any] | None:
        try:
            return json.loads(_get(f"{self.api}/releases/permalink/latest", 1_000_000))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None  # no releases yet
            raise

    def fetch_asset(self, url: str) -> bytes:
        return _get(url, MAX_FIRMWARE_BYTES)

    # state

    @property
    def enabled(self) -> bool:
        return bool(self.repo)

    def state(self) -> dict[str, Any]:
        st = self.ctx.db.get_setting(STATE_KEY) or {}
        latest = self.ctx.firmware.get(st["latest_id"]) if st.get("latest_id") else None
        return {"enabled": self.enabled, "repo": self.repo, "checked_at": st.get("checked_at"),
                "error": st.get("error"), "release": st.get("release"), "latest": latest}

    def latest(self) -> dict[str, Any] | None:
        return self.state()["latest"]

    def _check_sync(self) -> dict[str, Any] | None:
        """Fetch the latest release and store its firmware. Returns the firmware, or None if there is no release."""
        release = self.fetch_latest_release()
        if release is None:
            return None
        links = (release.get("assets") or {}).get("links") or []
        link = next((ln for ln in links if str(ln.get("name", "")).endswith(ASSET_NAME)), None)
        if link is None:
            raise ValueError(f"release {release.get('tag_name')} has no {ASSET_NAME}")
        url = link.get("direct_asset_url") or link["url"]
        tag = str(release.get("tag_name", ""))
        known = next((fw for fw in self.ctx.firmware.list() if fw.get("release") == tag), None)
        if known:
            return known
        fw = self.ctx.firmware.add(self.fetch_asset(url))
        if version_key(fw["version"]) != version_key(tag):
            log.warning("release %s contains firmware %s", tag, fw["version"])
        return self.ctx.firmware.tag(fw["id"], release=tag)

    async def check(self, max_age_s: float = 0) -> dict[str, Any]:
        """Check the repository unless the last check is younger than max_age_s. Returns state()."""
        if not self.enabled:
            return self.state()
        async with self._lock:
            previous = self.ctx.db.get_setting(STATE_KEY) or {}
            if max_age_s and time.time() - (previous.get("checked_at") or 0) < max_age_s:
                return self.state()
            st: dict[str, Any] = {"checked_at": time.time(), "latest_id": previous.get("latest_id")}
            try:
                fw = await run_in_threadpool(self._check_sync)
                st.update(latest_id=fw["id"] if fw else None, release=fw.get("release") if fw else None, error=None)
            except Exception as e:  # network, GitLab or a bad image: keep the previous latest
                log.warning("firmware update check failed: %s", e)
                st["error"] = str(e)[:300]
                st["release"] = previous.get("release")
            self.ctx.db.set_setting(STATE_KEY, st)
        if st.get("latest_id") != previous.get("latest_id"):
            log.info("latest firmware release: %s", st.get("release"))
        self.ctx.hub.publish({"type": "firmware"})
        return self.state()

    # offers

    def offer_for(self, device: dict[str, Any]) -> dict[str, Any] | None:
        """The update_available message for a speaker, or None if it is up to date."""
        fw = self.latest()
        if fw is None or (device.get("hardware") and fw["hardware"] != device["hardware"]):
            return None
        if not is_newer(fw["version"], device.get("firmware")):
            return None
        return {"type": "update_available", "firmware": update_args(self.ctx.settings.public_url, fw)}

    async def handle_boot_check(self, device_id: str) -> bool:
        """A speaker that just started asks for updates: check (cached) and offer. Returns True if offered."""
        await self.check(max_age_s=CACHE_S)
        device = self.ctx.db.get_device(device_id)
        msg = self.offer_for(device) if device else None
        return bool(msg) and await self.ctx.hub.send(device_id, msg)
