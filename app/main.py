"""Application factory. Run with: uvicorn --factory app.main:create_app"""
from __future__ import annotations

import logging
import secrets
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from . import __version__
from .admin import api as admin_api
from .admin import pages as admin_pages
from .assets import CharacterStore
from .context import Context
from .db import Database
from .device_api import assets_router
from .device_api import router as device_router
from .hub import Hub
from .settings import Settings, load_settings
from .starter import ensure_default_character

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    for d in (settings.data_dir, settings.recordings_dir):
        d.mkdir(parents=True, exist_ok=True)
    characters = CharacterStore(settings.characters_dir, settings.cache_dir)
    ensure_default_character(characters)

    db = Database(settings.db_path)
    session_secret = settings.session_secret or db.get_setting("session_secret")
    if not session_secret:
        session_secret = secrets.token_urlsafe(48)
        db.set_setting("session_secret", session_secret)

    app = FastAPI(title="Kotodama", version=__version__, docs_url="/api/docs", redoc_url=None)
    app.state.ctx = Context(settings=settings, db=db, hub=Hub(), characters=characters)
    app.add_middleware(SessionMiddleware, secret_key=session_secret, session_cookie="kotodama_session",
                       same_site="lax", https_only=settings.secure_cookies, max_age=7 * 86400)
    app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
    app.include_router(device_router)
    app.include_router(assets_router)
    app.include_router(admin_pages)
    app.include_router(admin_api)

    @app.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        return RedirectResponse("/admin")

    return app
