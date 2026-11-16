"""FastAPI app configuration."""

import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from kladd import config
from kladd.env import ensure_google_api_key

from .api_routes import api_router
from .auth import AuthMiddleware
from .routes import router
from .session_token import session_settings

_BASE = Path(__file__).parent


def _preload_libs() -> None:
    """Import heavy libs in background so first draft is fast."""
    import kladd.drafter  # noqa: F401
    import kladd.sources  # noqa: F401
    import kladd.storage  # noqa: F401


@asynccontextmanager
async def _lifespan(app: FastAPI):
    ensure_google_api_key()
    threading.Thread(target=_preload_libs, daemon=True).start()
    yield


session_settings()  # fail the deploy on missing/bad SESSION_* config
app = FastAPI(title=config.APP_NAME, lifespan=_lifespan)
app.mount("/static", StaticFiles(directory=_BASE / "static"), name="static")
app.include_router(router)
app.include_router(api_router)
app.add_middleware(AuthMiddleware)

templates = Jinja2Templates(directory=_BASE / "templates")
templates.env.globals["app_name"] = config.APP_NAME
templates.env.globals["tagline"] = config.TAGLINE
templates.env.globals["is_manus"] = config.IS_MANUS
