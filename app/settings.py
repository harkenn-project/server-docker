"""Runtime settings, read from HARKENN_* environment variables."""
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
    update_branch: str = "releases"
    voice_warmup: bool = False  # load the voice models in the background at startup
    mdns: bool = False  # announce the server on the local network so speakers find it (discovery.py)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "harkenn.db"

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


def env(name: str, default: str = "") -> str:
    """HARKENN_<name>, or the default."""
    return os.environ.get(f"HARKENN_{name}") or default


def load_settings() -> Settings:
    # Both optional: the admin password is normally set in the web UI, and the session secret is generated and
    # stored in the database on first start.
    public_url = env("PUBLIC_URL", "http://harkenn.local:8000").rstrip("/")
    prebuilt = env("PREBUILT_CHARACTERS")
    return Settings(
        data_dir=Path(env("DATA_DIR", "./data")).resolve(),
        admin_password=env("ADMIN_PASSWORD"),
        session_secret=env("SESSION_SECRET"),
        public_url=public_url,
        secure_cookies=env("SECURE_COOKIES", "true" if public_url.startswith("https") else "false") == "true",
        adoption_ttl_s=int(env("ADOPTION_TTL_S", "600")),
        max_pending_adoptions=int(env("MAX_PENDING_ADOPTIONS", "20")),
        log_retention_per_device=int(env("LOG_RETENTION", "5000")),
        max_recording_bytes=int(env("MAX_RECORDING_BYTES", str(10 * 1024 * 1024))),
        max_upload_bytes=int(env("MAX_UPLOAD_BYTES", str(8 * 1024 * 1024))),
        prebuilt_characters=Path(prebuilt) if prebuilt else None,
        update_repo=env("UPDATE_REPO", "harkenn/harkenn-speakerfirmware").strip("/"),
        update_gitlab_url=env("UPDATE_GITLAB_URL", "https://gitlab.com").rstrip("/"),
        update_branch=env("UPDATE_BRANCH", "releases"),
        voice_warmup=env("VOICE_WARMUP", "true") == "true",
        mdns=env("MDNS", "true") == "true",
    )
