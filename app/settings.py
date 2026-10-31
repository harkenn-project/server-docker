"""Runtime settings, read from KOTODAMA_* environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    public_url: str
    admin_password: str = ""  # bootstrap/recovery only; see auth.py
    session_secret: str = ""  # generated and stored in the database when empty
    secure_cookies: bool = True
    adoption_ttl_s: int = 600
    max_pending_adoptions: int = 20
    log_retention_per_device: int = 5000
    max_recording_bytes: int = 10 * 1024 * 1024
    max_upload_bytes: int = 8 * 1024 * 1024
    prebuilt_characters: Path | None = None  # built-in characters drawn at image build time (see starter.py)
    update_repo: str = ""  # public GitLab project holding latest.json + firmware (updates.py); empty = no Git checks
    update_gitlab_url: str = "https://gitlab.com"
    update_branch: str = "main"

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

    @property
    def firmware_dir(self) -> Path:
        return self.data_dir / "firmware"


def load_settings() -> Settings:
    env = os.environ.get
    # Both optional: the admin password is normally set in the web UI, and the session secret is generated and
    # stored in the database on first start.
    admin_password = env("KOTODAMA_ADMIN_PASSWORD", "")
    session_secret = env("KOTODAMA_SESSION_SECRET", "")
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
        prebuilt_characters=Path(p) if (p := env("KOTODAMA_PREBUILT_CHARACTERS")) else None,
        update_repo=env("KOTODAMA_UPDATE_REPO", "ninjalife/kotodama-firmware").strip("/"),
        update_gitlab_url=env("KOTODAMA_UPDATE_GITLAB_URL", "https://gitlab.com").rstrip("/"),
        update_branch=env("KOTODAMA_UPDATE_BRANCH", "main"),
    )
