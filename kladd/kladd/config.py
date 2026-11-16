"""Runtime mode selection: one process serves one product (sitat | manus)."""

import os

_VALID = {"sitat", "manus"}

MODE = os.environ.get("KLADD_MODE", "sitat")
if MODE not in _VALID:
    raise RuntimeError(f"KLADD_MODE must be one of {_VALID}, got {MODE!r}")

IS_MANUS = MODE == "manus"
APP_NAME = "manuskladd" if IS_MANUS else "sitatkladd"
TAGLINE = (
    "Journalistiske artikler fra råmateriale"
    if IS_MANUS
    else "Korte sitatsaker fra kildemateriale"
)
