"""Environment configuration helpers."""

import logging
import os

log = logging.getLogger(__name__)

DEV_FLAG = "KLADD_DEV"


def is_dev() -> bool:
    return os.environ.get(DEV_FLAG) == "1"


def is_prod() -> bool:
    """True inside a Dokku container."""
    # Dokku sets DOKKU_APP_TYPE in every container; DOKKU_APP_NAME is not set there.
    return bool(os.environ.get("DOKKU_APP_TYPE") or os.environ.get("DOKKU_APP_NAME"))


def require_env(name: str, *, dev_default: str) -> str:
    """Return env var, or fall back to dev_default when KLADD_DEV=1 outside Dokku.

    Raises RuntimeError when the var is missing (or is the dev default on Dokku), so
    misconfigured deploys fail at startup rather than silently weakening security.
    """
    val = os.environ.get(name)
    if val and not (is_prod() and val == dev_default):
        return val
    if is_dev() and not is_prod():
        log.warning("%s not set; using insecure dev default (KLADD_DEV=1)", name)
        return dev_default
    raise RuntimeError(
        f"{name} is required (KLADD_DEV=1 allows a dev default locally, never on Dokku)"
    )


def ensure_google_api_key() -> None:
    """Map GEMINI_API_KEY to GOOGLE_API_KEY if needed."""
    if os.environ.get("GOOGLE_API_KEY"):
        return
    gemini = os.environ.get("GEMINI_API_KEY")
    if gemini:
        os.environ["GOOGLE_API_KEY"] = gemini
        return
    raise RuntimeError("Sett GEMINI_API_KEY eller GOOGLE_API_KEY.")
