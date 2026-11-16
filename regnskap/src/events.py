"""What happened, kept in the database so it survives deploys and can be read back over HTTP (/api/diagnostics).

Lifecycle steps are recorded with record(); every WARNING+ log line from src.* lands here too (LogHandler), with
its traceback and the company-year being read (set with `about`).
"""

import json
import logging
import os
import socket
import sys
import traceback
from collections.abc import Callable
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from typing import Any

from starlette.requests import Request
from starlette.responses import Response

from src import db

RELEASE = (os.environ.get("GIT_REV") or "dev")[:12]
# One per running process: a deploy runs the old and new container side by side for a while
PROCESS = f"{RELEASE}:{socket.gethostname()}:{os.getpid()}"
KEEP_DAYS = 60
MAX_DETAIL = 20_000

# (orgnr, year) of the read this task works on; copied into tasks it spawns
current: ContextVar[tuple[str | None, int | None]] = ContextVar("current", default=(None, None))


def about(orgnr: str | None, year: int | None = None) -> None:
    """Tag what this task records or logs from here on with the company-year."""
    current.set((orgnr, year))


def record(
    kind: str,
    message: str = "",
    *,
    level: str = "info",
    orgnr: str | None = None,
    year: int | None = None,
    detail: Any = None,
    exc: BaseException | None = None,
) -> None:
    """Store one event; never raises (a failed write goes to stderr)."""
    o, y = current.get()
    if exc is not None:
        detail = {**(detail or {}), "traceback": "".join(traceback.format_exception(exc))}
    text = (
        detail
        if isinstance(detail, str) or detail is None
        else json.dumps(detail, ensure_ascii=False, default=str)
    )
    try:
        db._run(
            "INSERT INTO events (at, release, process, level, kind, orgnr, year, message, detail) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                db.now_iso(),
                RELEASE,
                PROCESS,
                level,
                kind,
                orgnr if orgnr is not None else o,
                year if year is not None else y,
                message[:2000],
                text[:MAX_DETAIL] if text else None,
            ),
        )
    except Exception as e:  # the log of last resort
        print(f"events.record failed ({kind}: {message[:200]}): {e!r}", file=sys.stderr)


def recent(
    orgnr: str | None = None,
    year: int | None = None,
    since: str | None = None,
    level: str | None = None,
    kind: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Newest first. level 'warning' includes errors; kind matches a prefix ('read' → read.*)."""
    where, params = [], []
    if orgnr:
        where.append("orgnr = ?")
        params.append(orgnr)
    if year:
        where.append("year = ?")
        params.append(year)
    if since:
        where.append("at >= ?")
        params.append(since)
    if level == "warning":
        where.append("level IN ('warning', 'error')")
    elif level:
        where.append("level = ?")
        params.append(level)
    if kind:
        where.append("(kind = ? OR kind LIKE ?)")
        params += [kind, f"{kind}.%"]
    sql = "SELECT * FROM events" + (f" WHERE {' AND '.join(where)}" if where else "")
    rows = db._all(sql + " ORDER BY id DESC LIMIT ?", (*params, limit))
    for r in rows:
        if r["detail"] and r["detail"][:1] in "{[":
            try:
                r["detail"] = json.loads(r["detail"])
            except ValueError:
                pass
    return rows


def prune() -> None:
    cutoff = (datetime.now(UTC) - timedelta(days=KEEP_DAYS)).isoformat()
    db._run("DELETE FROM events WHERE at < ?", (cutoff,))


class LogHandler(logging.Handler):
    """WARNING+ log records → events, kind 'log.<logger>'."""

    def emit(self, rec: logging.LogRecord) -> None:
        exc = rec.exc_info[1] if rec.exc_info else None
        record(
            f"log.{rec.name.removeprefix('src.')}",
            rec.getMessage(),
            level="error" if rec.levelno >= logging.ERROR else "warning",
            exc=exc,
        )


async def server_errors(request: Request, call_next: Callable) -> Response:
    """HTTP middleware: record unhandled exceptions (with traceback) and 5xx responses."""
    what = f"{request.method} {request.url.path}"
    try:
        resp = await call_next(request)
    except Exception as e:
        record("http.exception", what, level="error", exc=e)
        raise
    if resp.status_code >= 500:
        record(
            "http.5xx",
            f"{what} → {resp.status_code}",
            level="error",
            detail={"query": request.url.query},
        )
    return resp


def install_log_handler() -> None:
    """Send src.* warnings and errors to the events table, and keep them on stderr for `dokku logs`."""
    log = logging.getLogger("src")
    if any(isinstance(h, LogHandler) for h in log.handlers):
        return
    log.addHandler(LogHandler(logging.WARNING))
    err = logging.StreamHandler(sys.stderr)
    err.setLevel(logging.WARNING)
    err.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    log.addHandler(err)
