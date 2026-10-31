"""Runtime settings, read from KOTODAMA_* environment variables."""
from __future__ import annotations

import logging
import os
import secrets
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("kotodama")


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    admin_password: str
    session_secret: str
    public_url: str
    secure_cookies: bool = True
    adoption_ttl_s: int = 600
    max_pending_adoptions: int = 20
    log_retention_per_device: int = 5000
    max_recording_bytes: int = 10 * 1024 * 1024
    max_upload_bytes: int = 8 * 1024 * 1024

    @property
    def db_path(self) -> Path:
        return self.data_dir / "kotodama.db"

    @property
    def characters_dir(self) -> Path:
        return self.data_dir / "characters"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @property
    def recordings_dir(self) -> Path:
        return self.data_dir / "recordings"


def load_settings() -> Settings:
    env = os.environ.get
    admin_password = env("KOTODAMA_ADMIN_PASSWORD", "")
    if not admin_password:
        admin_password = secrets.token_urlsafe(12)
        log.warning("KOTODAMA_ADMIN_PASSWORD is not set; generated admin password for this run: %s", admin_password)
    session_secret = env("KOTODAMA_SESSION_SECRET", "")
    if not session_secret:
        session_secret = secrets.token_urlsafe(32)
        log.warning("KOTODAMA_SESSION_SECRET is not set; admin sessions will not survive a restart")
    public_url = env("KOTODAMA_PUBLIC_URL", "https://kotodama.mylife.ninja").rstrip("/")
    return Settings(
        data_dir=Path(env("KOTODAMA_DATA_DIR", "./data")).resolve(),
        admin_password=admin_password,
        session_secret=session_secret,
        public_url=public_url,
        secure_cookies=env("KOTODAMA_SECURE_COOKIES", "true" if public_url.startswith("https") else "false") == "true",
        adoption_ttl_s=int(env("KOTODAMA_ADOPTION_TTL_S", "600")),
        max_pending_adoptions=int(env("KOTODAMA_MAX_PENDING_ADOPTIONS", "20")),
        log_retention_per_device=int(env("KOTODAMA_LOG_RETENTION", "5000")),
        max_recording_bytes=int(env("KOTODAMA_MAX_RECORDING_BYTES", str(10 * 1024 * 1024))),
        max_upload_bytes=int(env("KOTODAMA_MAX_UPLOAD_BYTES", str(8 * 1024 * 1024))),
    )
