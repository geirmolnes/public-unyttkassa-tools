"""Content-free Gemini telemetry, vendored into pilot apps. Standard library only.

Set AI_USAGE_TOKEN, AI_USAGE_OUTBOX (persistent path), AI_USAGE_BILLING=paid.
Delivery is idempotent; failed sends remain in the local SQLite outbox.
"""
from __future__ import annotations

import atexit
import json
import logging
import os
import sqlite3
import threading
import time
import urllib.request
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

_lock = threading.Lock()
_db_init_lock = threading.Lock()
_retry_after = 0.0
log = logging.getLogger("ai_usage")


def _get(obj, snake, camel=None, default=None):
    if isinstance(obj, dict):
        return obj.get(camel or snake, obj.get(snake, default))
    return getattr(obj, snake, default)


def _connect():
    path = Path(os.environ["AI_USAGE_OUTBOX"])
    path.parent.mkdir(parents=True, exist_ok=True)
    # WAL initialization races on a brand-new file. Serialize it within the
    # process and retry SQLite's immediate journal-mode lock across processes.
    with _db_init_lock:
        for attempt in range(20):
            db = sqlite3.connect(path, timeout=5)
            try:
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("CREATE TABLE IF NOT EXISTS outbox (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
                return db
            except sqlite3.OperationalError as exc:
                db.close()
                if "locked" not in str(exc).lower() or attempt == 19:
                    raise
                time.sleep(0.05)



def record(response=None, *, model, kind, operation="generate", status="ok", modality="text"):
    """Record each provider response once, before parsing/validating its content."""
    if not os.getenv("AI_USAGE_TOKEN") or not os.getenv("AI_USAGE_OUTBOX"):
        return
    try:
        meta = _get(response, "usage_metadata", "usageMetadata")
        def count(snake, camel, default=None):
            value = _get(meta, snake, camel, default)
            return value if isinstance(value, int) and not isinstance(value, bool) else default
        candidates = _get(response, "candidates", default=[]) or []
        feedback = _get(response, "prompt_feedback", "promptFeedback")
        blocked = _get(feedback, "block_reason", "blockReason")
        extras = any(_get(c, "grounding_metadata", "groundingMetadata") for c in candidates)
        for candidate in candidates:
            reason = _get(candidate, "finish_reason", "finishReason")
            if reason in {"SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "RECITATION"}:
                blocked = True
        event = dict(event_id=str(uuid4()), occurred_at=datetime.now(UTC).isoformat(),
                     model=model, kind=kind, operation=operation,
                     status="blocked" if blocked else status,
                     input_tokens=count("prompt_token_count", "promptTokenCount"),
                     output_tokens=count("candidates_token_count", "candidatesTokenCount", 0 if meta else None),
                     cached_tokens=count("cached_content_token_count", "cachedContentTokenCount", 0),
                     thinking_tokens=count("thoughts_token_count", "thoughtsTokenCount", 0),
                     total_tokens=count("total_token_count", "totalTokenCount"),
                     tool_tokens=count("tool_use_prompt_token_count", "toolUsePromptTokenCount", 0),
                     billing=os.getenv("AI_USAGE_BILLING", "unknown"), mode="standard",
                     modality=modality, extras=bool(extras))
        enqueue(event)
    except Exception:
        log.warning("AI usage could not be persisted; model operation continues")


def enqueue(event):
    with closing(_connect()) as db, db:
        db.execute("INSERT OR IGNORE INTO outbox VALUES (?, ?)", (event["event_id"], json.dumps(event)))
    flush()


def flush():
    """Bound each flush; leave unacknowledged rows intact on all errors."""
    global _retry_after
    if not os.getenv("AI_USAGE_TOKEN") or not os.getenv("AI_USAGE_OUTBOX"):
        return
    if time.monotonic() < _retry_after or not _lock.acquire(blocking=False):
        return
    try:
        with closing(_connect()) as db:
            rows = db.execute("SELECT id, payload FROM outbox ORDER BY rowid LIMIT 100").fetchall()
            if not rows:
                return
            body = json.dumps({"events": [json.loads(r[1]) for r in rows]}).encode()
            req = urllib.request.Request("https://example.org/api/ai-usage/events", data=body,
                headers={"Authorization": "Bearer " + os.environ["AI_USAGE_TOKEN"],
                         "Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=2) as res:
                ack = json.load(res)
            if ack.get("accepted") != len(rows):
                raise ValueError("incomplete acknowledgment")
            with db:
                db.executemany("DELETE FROM outbox WHERE id = ?", [(r[0],) for r in rows])
    except Exception:
        _retry_after = time.monotonic() + 60
        log.warning("AI usage delivery pending; retained in persistent outbox")
    finally:
        _lock.release()


atexit.register(flush)


def tracked_generate(generate, **kwargs):
    """Billig Mat's image/text extraction calls, inside existing retry scopes."""
    try:
        response = generate(**kwargs)
    except Exception:
        record(model=kwargs["model"], kind="cron", status="error", modality="image")
        raise
    record(response, model=kwargs["model"], kind="cron", modality="image")
    return response
