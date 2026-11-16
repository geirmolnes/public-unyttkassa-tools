"""Uvicorn entrypoint for kladd."""

import socket
from pathlib import Path

import uvicorn

_KLADD_SRC = str(Path(__file__).resolve().parent.parent)

DEFAULT_PORT = 8000
MAX_PORT = DEFAULT_PORT + 20


def _find_free_port() -> int:
    """Return DEFAULT_PORT if available, otherwise first free port above it."""
    for port in range(DEFAULT_PORT, MAX_PORT):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    return DEFAULT_PORT


def main() -> None:
    port = _find_free_port()
    if port != DEFAULT_PORT:
        print(f"Port {DEFAULT_PORT} in use, using {port}")
    uvicorn.run(
        "kladd.frontend.app:app",
        host="127.0.0.1",
        port=port,
        reload=True,
        reload_dirs=[_KLADD_SRC],
    )


if __name__ == "__main__":
    main()
