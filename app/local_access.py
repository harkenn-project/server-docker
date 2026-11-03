"""Break-glass access to a speaker's own web page (firmware src/web.cpp), and what it needs from here.

A speaker accepts two passwords on its web page: the admin password of this server, and its own speaker password.
Neither is stored in the clear:

- The admin password reaches speakers as a separate salted PBKDF2 hash with fewer rounds than the one this server
  logs in with (auth.py), so a speaker can check it in about a second. It's made whenever the admin password is
  entered: at login, setup and change.
- The speaker password is random, made at adoption, or set by the admin. Speakers only get its PBKDF2 hash and the
  WPA2 key for their hotspot (itself PBKDF2 of the password, salted with the hotspot's name), so joining the hotspot
  needs the speaker password too. Whoever can log in here can see it, so it's a way into a speaker that can't reach
  Harkenn: this server keeps it encrypted (AES-GCM) with a key in its own file next to the database, KEY_FILE.

After too many wrong passwords a speaker locks its page (see the firmware); "unlock" here clears that.
"""
from __future__ import annotations

import base64
import hashlib
import os
import secrets
from typing import TYPE_CHECKING, Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .auth import verify_password

if TYPE_CHECKING:
    from .context import Context

SPEAKER_ITERATIONS = 10_000  # PBKDF2-SHA256 rounds a speaker can check in about a second
ADMIN_KEY = "admin_password_speaker_hash"  # settings table: the admin password, hashed for speakers
DEVICES_KEY = "local_access"  # settings table: {device_id: {"hash", "psk", "secret", "unlock"}}
KEY_FILE = "speaker-passwords.key"  # in the data dir: encrypts the speaker passwords ("secret")
ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"  # no 0/o, 1/l/i
MIN_LENGTH = 8  # WPA2's minimum
MAX_LENGTH = 63  # ...and maximum


def speaker_hash(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, SPEAKER_ITERATIONS)
    return "pbkdf2_sha256${}${}${}".format(
        SPEAKER_ITERATIONS, base64.b64encode(salt).decode(), base64.b64encode(digest).decode())


def hotspot_name(device_id: str) -> str:
    """The speaker's hotspot (firmware ap_name()): Harkenn- and the last four hex digits of its MAC."""
    return "Harkenn-" + device_id[8:].upper()


def wpa_key(password: str, ssid: str) -> str:
    """The WPA2 pre-shared key for this password on this network, as 64 hex digits (IEEE 802.11i)."""
    return hashlib.pbkdf2_hmac("sha1", password.encode(), ssid.encode(), 4096, 32).hex()


def generate_password() -> str:
    return "-".join("".join(secrets.choice(ALPHABET) for _ in range(4)) for _ in range(3))


def validate_password(password: str) -> str | None:
    """Returns an error message, or None if the password can be a speaker password."""
    if not MIN_LENGTH <= len(password) <= MAX_LENGTH:
        return f"Use {MIN_LENGTH} to {MAX_LENGTH} characters (it's also the speaker's hotspot password)"
    if not password.isascii() or not password.isprintable():
        return "Use letters, digits and punctuation only"
    return None


def remember_admin_password(ctx: Context, password: str) -> bool:
    """Make the speakers' copy of the admin password unless it's already up to date. Returns True if it changed."""
    stored = ctx.db.get_setting(ADMIN_KEY)
    if stored and stored.get("of") == ctx.db.get_setting("admin_password_hash") and _matches(stored["hash"], password):
        return False
    # "of" ties it to the server's own hash, so a password change from elsewhere (the environment) shows up
    ctx.db.set_setting(ADMIN_KEY, {"hash": speaker_hash(password), "of": ctx.db.get_setting("admin_password_hash")})
    return True


def _matches(stored: str, password: str) -> bool:
    return stored.split("$")[1:2] == [str(SPEAKER_ITERATIONS)] and verify_password(password, stored)


def _devices(ctx: Context) -> dict[str, Any]:
    return ctx.db.get_setting(DEVICES_KEY) or {}


def device_state(ctx: Context, device_id: str) -> dict[str, Any]:
    return _devices(ctx).get(device_id, {})


def _cipher(ctx: Context) -> AESGCM:
    path = ctx.settings.data_dir / KEY_FILE
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(AESGCM.generate_key(bit_length=256))
    return AESGCM(path.read_bytes())


def set_speaker_password(ctx: Context, device_id: str, password: str) -> None:
    devices = _devices(ctx)
    entry = devices.setdefault(device_id, {})
    entry["hash"] = speaker_hash(password)
    entry["psk"] = wpa_key(password, hotspot_name(device_id))
    nonce = secrets.token_bytes(12)
    sealed = _cipher(ctx).encrypt(nonce, password.encode(), device_id.encode())  # bound to this speaker
    entry["secret"] = base64.b64encode(nonce + sealed).decode()
    ctx.db.set_setting(DEVICES_KEY, devices)


def speaker_password(ctx: Context, device_id: str) -> str | None:
    """The speaker password, for the admin UI. None if there's none, or it can't be decrypted (made before passwords
    were kept, or the key file was lost): make a new one then."""
    secret = device_state(ctx, device_id).get("secret")
    if not secret:
        return None
    raw = base64.b64decode(secret)
    try:
        return _cipher(ctx).decrypt(raw[:12], raw[12:], device_id.encode()).decode()
    except (InvalidTag, ValueError):
        return None


def unlock(ctx: Context, device_id: str) -> None:
    """Clear the speaker's lockout: it compares this counter with the last one it saw."""
    devices = _devices(ctx)
    entry = devices.setdefault(device_id, {})
    entry["unlock"] = entry.get("unlock", 0) + 1
    ctx.db.set_setting(DEVICES_KEY, devices)


def forget(ctx: Context, device_id: str) -> None:
    devices = _devices(ctx)
    if devices.pop(device_id, None) is not None:
        ctx.db.set_setting(DEVICES_KEY, devices)


def payload(ctx: Context, device_id: str) -> dict[str, Any]:
    """The "local_access" part of a speaker's config message."""
    admin = ctx.db.get_setting(ADMIN_KEY) or {}
    entry = device_state(ctx, device_id)
    return {"admin_hash": admin.get("hash", ""), "speaker_hash": entry.get("hash", ""),
            "hotspot_psk": entry.get("psk", ""), "unlock": entry.get("unlock", 0)}


def summary(ctx: Context, device_id: str) -> dict[str, Any]:
    """For the admin UI: never the hashes."""
    entry = device_state(ctx, device_id)
    return {"speaker_password": bool(entry.get("hash")), "viewable": bool(entry.get("secret")),
            "admin_password": bool((ctx.db.get_setting(ADMIN_KEY) or {}).get("hash"))}
