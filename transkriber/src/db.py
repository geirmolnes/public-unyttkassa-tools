"""SQLite persistence for transcripts.

The DB lives on the same Dokku storage mount as the audio files so both
survive container restarts and redeploys. One row per transcript; segments
are stored as a JSON blob rather than in a separate table because we
always read/write them as a whole document.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from .transcribe import Transcript, _strip_enum_prefix

DB_PATH = Path(os.environ.get("TRANSKRIBER_DB", "uploads/transkriber.db"))


_SCHEMA = """
CREATE TABLE IF NOT EXISTS transcripts (
    id               TEXT PRIMARY KEY,
    user_email       TEXT NOT NULL,
    filename         TEXT NOT NULL,
    audio_path       TEXT NOT NULL,
    duration_seconds REAL,
    language         TEXT,
    segments_json    TEXT,
    status           TEXT NOT NULL,
    error            TEXT,
    created_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_transcripts_user_created
    ON transcripts(user_email, created_at DESC);
"""


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _connect() as conn:
        conn.executescript(_SCHEMA)

        # Migration: question-detection moved to a second async pass, so we
        # need a flag per row that tells the UI when italics are ready.
        cols = {r[1] for r in conn.execute("PRAGMA table_info(transcripts)").fetchall()}
        if "questions_done" not in cols:
            conn.execute(
                "ALTER TABLE transcripts ADD COLUMN questions_done INTEGER DEFAULT 0"
            )
            # Rows that finished under the old code already had questions
            # flagged inside save_result, so mark them complete now.
            conn.execute("UPDATE transcripts SET questions_done = 1 WHERE status = 'done'")
        # (chunks_done/chunks_total from the old chunked-Gemini backend are no
        # longer created; existing DBs keep the columns, now unused.)
        # cleaned_done tracks whether the filler-removal pass has completed.
        # Default 1 ("no cleanup needed/expected") so Gemini rows and
        # pre-cleanup rows don't make the UI poll forever; the ElevenLabs
        # path in main.py flips it to 0 at job start and back to 1 when
        # cleanup succeeds or gives up.
        if "cleaned_done" not in cols:
            conn.execute(
                "ALTER TABLE transcripts ADD COLUMN cleaned_done INTEGER DEFAULT 1"
            )
        # provider is the transcription backend that ran the job — "gemini"
        # or "elevenlabs". Stored per-row so the admin dashboard shows
        # historical accuracy even if the app-wide toggle flips later.
        if "provider" not in cols:
            conn.execute("ALTER TABLE transcripts ADD COLUMN provider TEXT")
        # started_at / finished_at let the admin dashboard show wall-time
        # per job (how long transcription took, vs. tape duration).
        # Pre-migration rows stay NULL — wall-time isn't recoverable.
        if "started_at" not in cols:
            conn.execute("ALTER TABLE transcripts ADD COLUMN started_at TEXT")
        if "finished_at" not in cols:
            conn.execute("ALTER TABLE transcripts ADD COLUMN finished_at TEXT")
        # requeue_count guards the restart-recovery path: jobs interrupted by
        # a deploy are re-dispatched at startup, but only a bounded number of
        # times so a job that crashes the process can't crash-loop forever.
        if "requeue_count" not in cols:
            conn.execute("ALTER TABLE transcripts ADD COLUMN requeue_count INTEGER DEFAULT 0")
        # audio_purged=1 means the retention sweep deleted the audio file but
        # kept the transcript text. Lets the UI explain the missing player
        # instead of looking broken.
        if "audio_purged" not in cols:
            conn.execute("ALTER TABLE transcripts ADD COLUMN audio_purged INTEGER DEFAULT 0")
        # speaker_names_json maps diarization ids ("speaker_0") to
        # user-chosen display names. Per-transcript, not per-segment, so a
        # rename is one small UPDATE instead of rewriting segments_json.
        if "speaker_names_json" not in cols:
            conn.execute("ALTER TABLE transcripts ADD COLUMN speaker_names_json TEXT")

        # Data migration, gated on user_version so it runs exactly once: an
        # older filler-cleanup pass leaked the numbered-input index ("0: ",
        # "1: ") into cleaned_text. The write-time guard fixes new rows; this
        # backfills rows already on disk.
        if conn.execute("PRAGMA user_version").fetchone()[0] < 1:
            _strip_leaked_prefixes(conn)
            conn.execute("PRAGMA user_version = 1")


def _strip_leaked_prefixes(conn: sqlite3.Connection) -> None:
    """Strip leaked "N:" enumeration prefixes from stored segment text.

    Idempotent and safe: real replikker start with "– ", so a leading
    digit-then-delimiter can only be a leaked line index, never real content.
    Touches both text and cleaned_text defensively though only cleaned_text
    was ever affected in practice.
    """
    rows = conn.execute(
        "SELECT id, segments_json FROM transcripts WHERE segments_json IS NOT NULL"
    ).fetchall()
    fixed = 0
    for row in rows:
        try:
            segments = json.loads(row["segments_json"])
        except (TypeError, json.JSONDecodeError):
            continue
        changed = False
        for seg in segments:
            for field in ("text", "cleaned_text"):
                val = seg.get(field)
                if isinstance(val, str):
                    stripped = _strip_enum_prefix(val)
                    if stripped != val:
                        seg[field] = stripped
                        changed = True
        if changed:
            conn.execute(
                "UPDATE transcripts SET segments_json = ? WHERE id = ?",
                (json.dumps(segments), row["id"]),
            )
            fixed += 1
    if fixed:
        print(
            f"[transkriber] migration: stripped leaked index prefixes from {fixed} row(s)",
            flush=True,
        )


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(DB_PATH, isolation_level=None)  # autocommit
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
    finally:
        conn.close()


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    if d.get("segments_json"):
        segments = json.loads(d.pop("segments_json"))
        d["transcript"] = {"language": d.get("language"), "segments": segments}
    else:
        d.pop("segments_json", None)
        d["transcript"] = None
    d["speaker_names"] = json.loads(d.pop("speaker_names_json", None) or "{}")
    return d


# ------------------------------------------------------------------------------
# CRUD.
# ------------------------------------------------------------------------------


def create_job(
    job_id: str,
    user_email: str,
    filename: str,
    audio_path: str,
    provider: str | None = None,
) -> None:
    """Insert a queued job. `provider` is stamped at creation so a requeue
    after a restart keeps the user's per-upload choice even if the app-wide
    default has changed in the meantime."""
    with _connect() as conn:
        conn.execute(
            """INSERT INTO transcripts
               (id, user_email, filename, audio_path, provider, status, created_at)
               VALUES (?, ?, ?, ?, ?, 'queued', ?)""",
            (job_id, user_email, filename, audio_path, provider, _now()),
        )


def mark_running(job_id: str) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE transcripts SET status = 'running', started_at = ? WHERE id = ?",
            (_now(), job_id),
        )


def set_audio_path(job_id: str, audio_path: str) -> None:
    """Point the row at a replacement audio file (e.g. the compressed copy)."""
    with _connect() as conn:
        conn.execute(
            "UPDATE transcripts SET audio_path = ? WHERE id = ?",
            (audio_path, job_id),
        )


def set_duration(job_id: str, seconds: float) -> None:
    """Record the audio duration once ffprobe has measured it."""
    with _connect() as conn:
        conn.execute(
            "UPDATE transcripts SET duration_seconds = ? WHERE id = ?",
            (seconds, job_id),
        )


def save_result(job_id: str, transcript: Transcript) -> None:
    """Save the transcribed segments WITHOUT the is_question flags.

    Those come in afterwards via save_questions so the user sees the transcript
    as soon as transcription is finished, with italics appearing a few seconds
    later when the second LLM pass completes.
    """
    segments_json = json.dumps([s.model_dump() for s in transcript.segments])
    with _connect() as conn:
        conn.execute(
            """UPDATE transcripts
               SET status = 'done',
                   language = ?,
                   segments_json = ?,
                   error = NULL,
                   questions_done = 0,
                   finished_at = ?
               WHERE id = ?""",
            (transcript.language, segments_json, _now(), job_id),
        )


def save_questions(job_id: str, transcript: Transcript) -> None:
    """Overwrite segments with the question-flagged version and mark done.

    Called from the question-detection background task. If detection fails
    we still flip questions_done to 1 so the UI stops polling — the transcript
    just shows without italics.
    """
    segments_json = json.dumps([s.model_dump() for s in transcript.segments])
    with _connect() as conn:
        conn.execute(
            """UPDATE transcripts
               SET segments_json = ?,
                   questions_done = 1
               WHERE id = ?""",
            (segments_json, job_id),
        )


def mark_questions_done(job_id: str) -> None:
    """Flip questions_done without touching segments — for failure paths."""
    with _connect() as conn:
        conn.execute("UPDATE transcripts SET questions_done = 1 WHERE id = ?", (job_id,))


def mark_cleanup_pending(job_id: str) -> None:
    """Set cleaned_done=0 at job start so the UI knows to poll for the
    Renset/Rå toggle becoming available."""
    with _connect() as conn:
        conn.execute("UPDATE transcripts SET cleaned_done = 0 WHERE id = ?", (job_id,))


def save_cleaned(job_id: str, transcript) -> None:
    """Overwrite segments with cleaned_text populated and mark cleaned_done.

    Called from the filler-cleanup background task. If cleanup failed the
    passed-in transcript will have cleaned_text=None on all segments and
    the UI will simply not show the toggle.
    """
    segments_json = json.dumps([s.model_dump() for s in transcript.segments])
    with _connect() as conn:
        conn.execute(
            """UPDATE transcripts
               SET segments_json = ?,
                   cleaned_done = 1
               WHERE id = ?""",
            (segments_json, job_id),
        )


def mark_cleanup_done(job_id: str) -> None:
    """Flip cleaned_done=1 without touching segments — for failure paths."""
    with _connect() as conn:
        conn.execute("UPDATE transcripts SET cleaned_done = 1 WHERE id = ?", (job_id,))


def list_interrupted_jobs() -> list[dict]:
    """Rows left 'running' or 'queued' by a previous process.

    When the container restarts (deploy, crash, OOM), the asyncio task doing
    the transcription dies but the DB row still says running. On startup,
    main.py either re-dispatches these (audio is still on disk) or fails
    them when the retry budget is spent.
    """
    with _connect() as conn:
        rows = conn.execute(
            """SELECT id, audio_path, requeue_count
               FROM transcripts
               WHERE status IN ('running', 'queued')"""
        ).fetchall()
    return [dict(r) for r in rows]


def requeue_job(job_id: str) -> None:
    """Put an interrupted job back in 'queued' and burn one retry."""
    with _connect() as conn:
        conn.execute(
            """UPDATE transcripts
               SET status = 'queued',
                   error = NULL,
                   requeue_count = requeue_count + 1
               WHERE id = ?""",
            (job_id,),
        )


def fail_orphaned_job(job_id: str) -> None:
    """Fail an interrupted job that can't be requeued (budget spent / audio gone)."""
    with _connect() as conn:
        conn.execute(
            """UPDATE transcripts
               SET status = 'error',
                   error = 'Avbrutt av serveromstart — last opp filen på nytt.',
                   questions_done = 1,
                   cleaned_done = 1,
                   finished_at = ?
               WHERE id = ?""",
            (_now(), job_id),
        )


def list_purgeable_audio(cutoff_iso: str) -> list[dict]:
    """Finished rows older than the cutoff whose audio hasn't been purged yet.

    String comparison is safe because every created_at is written by _now()
    in the same ISO-8601 UTC format. Running/queued rows are excluded so the
    sweep can never delete audio out from under an active job.
    """
    with _connect() as conn:
        rows = conn.execute(
            """SELECT id, audio_path FROM transcripts
               WHERE created_at < ?
                 AND audio_purged = 0
                 AND status IN ('done', 'error')""",
            (cutoff_iso,),
        ).fetchall()
    return [dict(r) for r in rows]


def mark_audio_purged(job_id: str) -> None:
    with _connect() as conn:
        conn.execute("UPDATE transcripts SET audio_purged = 1 WHERE id = ?", (job_id,))


def set_speaker_name(job_id: str, user_email: str, speaker_id: str, name: str) -> bool:
    """Set (or clear, with empty name) the display name for one speaker id.

    Returns False when the transcript doesn't exist or belongs to someone else.
    """
    with _connect() as conn:
        row = conn.execute(
            "SELECT speaker_names_json FROM transcripts WHERE id = ? AND user_email = ?",
            (job_id, user_email),
        ).fetchone()
        if not row:
            return False
        names = json.loads(row["speaker_names_json"] or "{}")
        if name:
            names[speaker_id] = name
        else:
            names.pop(speaker_id, None)
        conn.execute(
            "UPDATE transcripts SET speaker_names_json = ? WHERE id = ? AND user_email = ?",
            (json.dumps(names), job_id, user_email),
        )
    return True


def save_error(job_id: str, error: str) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE transcripts SET status = 'error', error = ?, finished_at = ? WHERE id = ?",
            (error, _now(), job_id),
        )


def get(job_id: str, user_email: str) -> dict | None:
    """Fetch a transcript if it belongs to the caller."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM transcripts WHERE id = ? AND user_email = ?",
            (job_id, user_email),
        ).fetchone()
    return _row_to_dict(row) if row else None


def get_status(job_id: str, user_email: str) -> dict | None:
    """Lightweight status row for polling — skips segments_json entirely.

    The UI polls every few seconds; loading and JSON-parsing a multi-hundred-KB
    transcript on each poll is pure waste when the caller only wants flags.
    """
    with _connect() as conn:
        row = conn.execute(
            """SELECT id, status, filename, error, questions_done, cleaned_done
               FROM transcripts WHERE id = ? AND user_email = ?""",
            (job_id, user_email),
        ).fetchone()
    return dict(row) if row else None


def list_for_user(user_email: str, limit: int = 50) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            """SELECT id, filename, language, status, created_at
               FROM transcripts
               WHERE user_email = ?
               ORDER BY created_at DESC
               LIMIT ?""",
            (user_email, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def get_stats(recent_limit: int = 100) -> dict:
    """Aggregate usage stats for the admin dashboard.

    Scoped at the tool level (not per-user) because the dashboard is
    single-user-dev-only. Returns totals, a per-provider breakdown, and
    the most recent N runs with enough metadata for the admin to spot
    anomalies at a glance. No transcript content is exposed — the
    /api/stats endpoint is not intended to leak bodies, only metadata.
    """
    # transcribe_seconds is computed via SQLite julianday math so the client
    # doesn't have to parse timestamps. NULL when either bound is missing
    # (queued, running, pre-migration rows).
    _wall = (
        "CASE WHEN started_at IS NOT NULL AND finished_at IS NOT NULL "
        "THEN (julianday(finished_at) - julianday(started_at)) * 86400.0 "
        "ELSE NULL END"
    )
    with _connect() as conn:
        totals_row = conn.execute(
            f"""SELECT
                   COUNT(*) AS total,
                   SUM(CASE WHEN status = 'done'    THEN 1 ELSE 0 END) AS done,
                   SUM(CASE WHEN status = 'error'   THEN 1 ELSE 0 END) AS errored,
                   SUM(CASE WHEN status = 'running' THEN 1 ELSE 0 END) AS running,
                   SUM(CASE WHEN status = 'queued'  THEN 1 ELSE 0 END) AS queued,
                   COUNT(DISTINCT user_email) AS users,
                   SUM(COALESCE(duration_seconds, 0)) AS total_audio_seconds,
                   SUM(COALESCE({_wall}, 0)) AS total_transcribe_seconds
               FROM transcripts"""
        ).fetchone()
        totals = dict(totals_row) if totals_row else {}

        by_provider_rows = conn.execute(
            """SELECT COALESCE(provider, 'unknown') AS provider,
                      COUNT(*) AS count,
                      SUM(COALESCE(duration_seconds, 0)) AS audio_seconds
               FROM transcripts
               GROUP BY provider
               ORDER BY count DESC"""
        ).fetchall()
        by_provider = [dict(r) for r in by_provider_rows]

        runs_rows = conn.execute(
            f"""SELECT id, user_email, filename, status, error,
                      language, provider,
                      duration_seconds, created_at,
                      started_at, finished_at,
                      {_wall} AS transcribe_seconds,
                      questions_done, cleaned_done
               FROM transcripts
               ORDER BY created_at DESC
               LIMIT ?""",
            (recent_limit,),
        ).fetchall()
        runs = [dict(r) for r in runs_rows]

    # Float sums vs integer counts.
    _float_keys = {"total_audio_seconds", "total_transcribe_seconds"}
    totals_clean: dict[str, float | int] = {}
    for k, v in totals.items():
        if v is None:
            totals_clean[k] = 0
        elif k in _float_keys:
            totals_clean[k] = float(v)
        else:
            totals_clean[k] = int(v)

    return {"totals": totals_clean, "by_provider": by_provider, "runs": runs}


def delete(job_id: str, user_email: str) -> str | None:
    """Delete a transcript row; return the audio_path so the caller can unlink it."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT audio_path FROM transcripts WHERE id = ? AND user_email = ?",
            (job_id, user_email),
        ).fetchone()
        if not row:
            return None
        conn.execute(
            "DELETE FROM transcripts WHERE id = ? AND user_email = ?",
            (job_id, user_email),
        )
    return row["audio_path"]
