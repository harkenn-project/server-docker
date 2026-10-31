"""Admin password: set in the web UI and stored hashed in the database.

HARKENN_ADMIN_PASSWORD, if set, only applies while no password has been set in the UI. It is a bootstrap and
recovery path, not the normal way to configure the password.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

from .context import Context

SETTING = "admin_password_hash"
ITERATIONS = 600_000
MIN_LENGTH = 10


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, ITERATIONS)
    return "pbkdf2_sha256${}${}${}".format(
        ITERATIONS, base64.b64encode(salt).decode(), base64.b64encode(digest).decode())


def verify_password(password: str, stored: str) -> bool:
    try:
        algorithm, iterations, salt, digest = stored.split("$")
    except ValueError:
        return False
    if algorithm != "pbkdf2_sha256":
        return False
    candidate = hashlib.pbkdf2_hmac("sha256", password.encode(), base64.b64decode(salt), int(iterations))
    return hmac.compare_digest(candidate, base64.b64decode(digest))


def password_configured(ctx: Context) -> bool:
    return bool(ctx.db.get_setting(SETTING) or ctx.settings.admin_password)


def check_password(ctx: Context, password: str) -> bool:
    stored = ctx.db.get_setting(SETTING)
    if stored:
        return verify_password(password, stored)
    if ctx.settings.admin_password:
        return hmac.compare_digest(password.encode(), ctx.settings.admin_password.encode())
    return False


def set_password(ctx: Context, password: str) -> None:
    ctx.db.set_setting(SETTING, hash_password(password))


def validate_new_password(password: str, confirm: str) -> str | None:
    """Returns an error message, or None if the password is acceptable."""
    if len(password) < MIN_LENGTH:
        return f"Use at least {MIN_LENGTH} characters"
    if password != confirm:
        return "The passwords don't match"
    return None
