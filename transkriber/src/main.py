"""FastAPI app for unytt-transkriber.

Endpoints:
  GET  /                  -> upload UI + recent transcripts sidebar
  GET  /t/{id}            -> saved transcript view
  POST /upload            -> accept audio, create DB row, kick off job, return job_id
  GET  /status/{id}       -> poll job state (?full=1 to include the transcript)
  GET  /audio/{id}        -> stream audio back (for playback)
  GET  /clip/{id}         -> cut [start,end] from the audio, serve as download
  GET  /t/{id}/export     -> download transcript as txt/srt/vtt/docx
  POST /t/{id}/speakers   -> rename a diarized speaker
  POST /t/{id}/delete     -> delete transcript + audio file
  GET  /api/stats         -> usage metadata (API-key auth, for admin dashboard)
  GET  /healthz           -> liveness probe for Dokku

State lives in a SQLite DB on the same Dokku storage mount as the audio files,
so history survives redeploys and restarts.
"""

from __future__ import annotations

import asyncio
import io
import math
import os
import secrets
import tempfile
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from . import db
from .auth import AuthMiddleware, get_user_email
from .session_token import session_settings
from .transcribe import (
    Transcript,
    _probe_duration,
    _run_tool,
    clean_fillers,
    compress_audio,
    mark_questions,
    needs_compression,
    probe_bitrate,
    transcribe_file,
)

# Stamped on every row; kept as a column so the admin dashboard stays
# accurate across historical rows transcribed by the old Gemini backend.
PROVIDER = "elevenlabs"

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR", BASE_DIR.parent / "uploads"))
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# 2 GB fits ~1.9 h of 48 kHz/24-bit stereo WAV straight off a recorder.
# Lossless uploads are compressed after upload (see _compress_if_needed), so
# the cap only bounds the transient upload, not what we keep on disk. nginx's
# client-max-body-size on prod must stay above this (currently 2100m).
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "2048"))
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024


def _size_label(mb: int) -> str:
    """Human label for a MB cap: whole gigabytes as 'N GB', else 'N MB'."""
    if mb >= 1024 and mb % 1024 == 0:
        return f"{mb // 1024} GB"
    return f"{mb} MB"


MAX_UPLOAD_LABEL = _size_label(MAX_UPLOAD_MB)

# Audio formats we accept; all supported by ElevenLabs Scribe.
ALLOWED_EXTENSIONS = {".wav", ".mp3", ".aiff", ".aac", ".ogg", ".flac", ".m4a", ".mp4"}

db.init_db()

# Strong references to in-flight background tasks. asyncio's event loop only
# holds WEAK references to tasks, so without this set a long transcription
# job could be garbage-collected mid-flight and silently vanish.
_background_tasks: set[asyncio.Task] = set()


def _spawn(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


# Jobs interrupted by a restart get re-dispatched this many times before we
# give up and fail them — bounds the damage if a specific file reliably
# crashes the process.
MAX_REQUEUES = 2

# Audio files older than this are deleted by the retention sweep (transcript
# text is kept). Disabled by default (0) — opt in by setting the env var
# when disk pressure actually becomes a problem.
AUDIO_RETENTION_DAYS = int(os.environ.get("AUDIO_RETENTION_DAYS", "0"))
_RETENTION_SWEEP_INTERVAL_SECONDS = 6 * 3600


def _requeue_interrupted_jobs() -> None:
    """Resume jobs killed by a restart instead of telling the user to re-upload.

    The audio file is still on disk, so re-dispatching _run_job recovers the
    job invisibly. Jobs over the retry budget (or whose audio is gone) fail
    with the old re-upload message.
    """
    for row in db.list_interrupted_jobs():
        job_id = row["id"]
        audio = Path(row["audio_path"])
        if row["requeue_count"] >= MAX_REQUEUES or not audio.exists():
            db.fail_orphaned_job(job_id)
            _say(f"job={job_id} orphaned and not requeueable; marked error")
            continue
        db.requeue_job(job_id)
        _spawn(_run_job(job_id, audio))
        _say(f"job={job_id} requeued after restart (attempt {row['requeue_count'] + 1})")


def _purge_old_audio() -> int:
    """Delete audio files past retention; keep the transcript rows."""
    if AUDIO_RETENTION_DAYS <= 0:
        return 0
    cutoff = datetime.now(UTC) - timedelta(days=AUDIO_RETENTION_DAYS)
    purged = 0
    for row in db.list_purgeable_audio(cutoff.isoformat(timespec="seconds")):
        Path(row["audio_path"]).unlink(missing_ok=True)
        db.mark_audio_purged(row["id"])
        purged += 1
    return purged


async def _retention_loop() -> None:
    while True:
        try:
            purged = _purge_old_audio()
            if purged:
                _say(f"retention sweep purged {purged} audio file(s)")
        except Exception as exc:
            _say(f"retention sweep FAILED (non-fatal): {type(exc).__name__}: {exc}")
        await asyncio.sleep(_RETENTION_SWEEP_INTERVAL_SECONDS)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    _requeue_interrupted_jobs()
    retention_task = _spawn(_retention_loop())
    yield
    retention_task.cancel()


session_settings()  # bad or missing SESSION_* config fails the deploy, not each request
app = FastAPI(title="unytt transkriber", lifespan=_lifespan)
app.add_middleware(AuthMiddleware)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


@app.middleware("http")
async def _reject_oversized_uploads(request: Request, call_next):
    """Reject too-large uploads from the Content-Length header, BEFORE the body.

    The in-endpoint size cap can't protect the server by itself: FastAPI has
    already spooled the entire multipart body to a temp file by the time the
    endpoint runs. Checking the declared length here stops the request before
    the body is consumed. (The endpoint cap still backstops requests that lie
    about or omit Content-Length.) 1 MB of slack covers multipart overhead.
    """
    # Raw ASGI path, not request.url.path, which is rebuilt from the Host header.
    if request.method == "POST" and request.scope["path"] == "/upload":
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > MAX_UPLOAD_BYTES + 1024 * 1024:
            return JSONResponse(
                {"detail": f"Filen er større enn {MAX_UPLOAD_LABEL}"}, status_code=413
            )
    return await call_next(request)

# Dokku sets GIT_REV on every deploy. Appending it to static URLs forces
# browsers to refetch CSS/JS after each release instead of serving stale
# cached assets — which is what hid the Renset/Rå toggle from existing
# tabs after the cleanup feature deployed.
_ASSET_VERSION = os.environ.get("GIT_REV", "dev")[:12]
templates.env.globals["asset_version"] = _ASSET_VERSION
# Exposed to the upload page so the client can reject oversized files
# instantly instead of uploading gigabytes just to get a 413 back.
templates.env.globals["max_upload_mb"] = MAX_UPLOAD_MB
templates.env.globals["max_upload_label"] = MAX_UPLOAD_LABEL
# The login service owns the session cookie, so logging out is its job too.
templates.env.globals["logout_url"] = os.environ.get(
    "LOGOUT_URL", "https://example.org/logout"
)


def _say(msg: str) -> None:
    """Print straight to stdout so Dokku's log stream captures it.

    We use print() instead of Python's logging module because uvicorn's
    default log config doesn't attach a handler to custom loggers — our
    _log.info() calls were being silently dropped.
    """
    print(f"[transkriber] {msg}", flush=True)


# Hard ceiling so a hung Scribe call can never leave a job stuck in
# 'running'. Scribe runs at ≈5-10x realtime; 45 min covers ~3 h tapes even
# at 4x. Bump TRANSCRIBE_TIMEOUT if tapes outgrow it.
_JOB_TIMEOUT_SECONDS = int(os.environ.get("TRANSCRIBE_TIMEOUT", "2700"))


async def _compress_if_needed(job_id: str, audio_path: Path) -> Path:
    """Swap a lossless/PCM upload for a compact MP3; returns the path to use.

    Fails soft: if probing or encoding breaks, the job carries on with the
    original file (Scribe accepts it, it's just bigger to ship and store).
    """
    try:
        bitrate = await probe_bitrate(audio_path)
        if not needs_compression(bitrate):
            return audio_path
        compressed = await compress_audio(audio_path)
    except Exception as exc:
        _say(f"job={job_id} compression failed (non-fatal, keeping original): {exc}")
        return audio_path
    # Repoint the row before deleting the original, so a crash in between
    # leaves an orphan file at worst, never a row pointing at nothing.
    db.set_audio_path(job_id, str(compressed))
    if compressed != audio_path:
        audio_path.unlink(missing_ok=True)
    _say(
        f"job={job_id} compressed {bitrate // 1000} kbps -> {compressed.name} "
        f"({compressed.stat().st_size} bytes)"
    )
    return compressed


async def _run_job(job_id: str, audio_path: Path) -> None:
    _say(f"job={job_id} STARTED file={audio_path.name} size={audio_path.stat().st_size}")
    db.mark_running(job_id)
    _say(f"job={job_id} marked running in DB")
    audio_path = await _compress_if_needed(job_id, audio_path)
    try:
        duration = await _probe_duration(audio_path)
        db.set_duration(job_id, duration)
    except Exception as exc:
        _say(f"job={job_id} duration probe failed (non-fatal): {exc}")
    db.mark_cleanup_pending(job_id)
    try:
        transcript = await asyncio.wait_for(
            transcribe_file(audio_path),
            timeout=_JOB_TIMEOUT_SECONDS,
        )
        # Save + mark status=done NOW so the user can read the transcript
        # immediately. Question-detection runs in a separate background task
        # that patches segments back in when it finishes; the frontend polls
        # questions_done and reloads to show italics. The cleanup pass runs
        # after question detection so it inherits is_question flags.
        db.save_result(job_id, transcript)
        _say(
            f"job={job_id} DONE language={transcript.language} "
            f"segments={len(transcript.segments)}"
        )
        _spawn(_run_post_passes(job_id, transcript))
    except TimeoutError:
        msg = f"Transkripsjon tok lengre enn {_JOB_TIMEOUT_SECONDS}s og ble avbrutt."
        db.save_error(job_id, msg)
        _say(f"job={job_id} TIMEOUT after {_JOB_TIMEOUT_SECONDS}s")
    except Exception as exc:
        import traceback

        tb = traceback.format_exc()
        db.save_error(job_id, f"{type(exc).__name__}: {exc}")
        _say(f"job={job_id} FAILED {type(exc).__name__}: {exc}\n{tb}")


async def _run_post_passes(job_id: str, transcript: Transcript) -> None:
    """Run the question-detection pass, then filler cleanup.

    Both passes update `segments_json` in-place, so we run them sequentially
    to avoid a last-write-wins race. Each pass fails soft and flips its own
    done-flag in the DB so the UI's polling terminates even on error.
    """
    flagged = await _run_question_pass(job_id, transcript)
    await _run_cleanup_pass(job_id, flagged)


async def _run_question_pass(job_id: str, transcript: Transcript) -> Transcript:
    """Flag journalist questions and persist.

    Returns the (possibly-flagged) transcript so the next pass can run on
    the post-flag state rather than re-reading from DB.
    """
    _say(f"job={job_id} question-pass STARTED ({len(transcript.segments)} segments)")
    try:
        flagged = await mark_questions(transcript)
        db.save_questions(job_id, flagged)
        marked = sum(1 for s in flagged.segments if s.is_question)
        _say(f"job={job_id} question-pass DONE flagged={marked}/{len(flagged.segments)}")
        return flagged
    except Exception as exc:
        import traceback

        db.mark_questions_done(job_id)
        _say(
            f"job={job_id} question-pass FAILED (transcript stays unflagged) "
            f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"
        )
        return transcript


async def _run_cleanup_pass(job_id: str, transcript: Transcript) -> None:
    """Third-stage background task: populate cleaned_text on each segment.

    Fails soft — on error the cleaned_done flag is flipped so the UI stops
    polling and the Renset/Rå toggle simply never appears.
    """
    _say(f"job={job_id} cleanup-pass STARTED ({len(transcript.segments)} segments)")
    try:
        cleaned = await clean_fillers(transcript)
        db.save_cleaned(job_id, cleaned)
        populated = sum(1 for s in cleaned.segments if s.cleaned_text is not None)
        _say(
            f"job={job_id} cleanup-pass DONE "
            f"cleaned_populated={populated}/{len(cleaned.segments)}"
        )
    except Exception as exc:
        import traceback

        db.mark_cleanup_done(job_id)
        _say(
            f"job={job_id} cleanup-pass FAILED (segments stay raw) "
            f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"
        )


# ------------------------------------------------------------------------------
# Pages.
# ------------------------------------------------------------------------------


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/")
def index(request: Request, user_email: str = Depends(get_user_email)):
    recent = db.list_for_user(user_email, limit=50)
    return templates.TemplateResponse(
        request, "index.html", {"recent": recent, "user_email": user_email}
    )


@app.get("/t/{job_id}")
def view_transcript(
    request: Request,
    job_id: str,
    user_email: str = Depends(get_user_email),
):
    record = db.get(job_id, user_email)
    if not record:
        raise HTTPException(404, "Transkripsjon ikke funnet")
    recent = db.list_for_user(user_email, limit=50)
    # The retention sweep deletes old audio but keeps the text. The template
    # hides the player and explains, instead of rendering a broken <audio>.
    audio_available = (
        not record.get("audio_purged") and Path(record["audio_path"]).exists()
    )
    return templates.TemplateResponse(
        request,
        "transcript.html",
        {
            "record": record,
            "recent": recent,
            "user_email": user_email,
            "active_id": job_id,
            "audio_available": audio_available,
        },
    )


# ------------------------------------------------------------------------------
# API.
# ------------------------------------------------------------------------------


@app.post("/upload")
async def upload(
    file: UploadFile = File(...),
    user_email: str = Depends(get_user_email),
) -> JSONResponse:
    if not file.filename:
        raise HTTPException(400, "Missing filename")
    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            400,
            f"Ustøttet format '{ext}'. Tillatt: {', '.join(sorted(ALLOWED_EXTENSIONS))}",
        )

    job_id = uuid.uuid4().hex
    dest = UPLOAD_DIR / f"{job_id}{ext}"

    # Stream the upload to disk with a size cap so a huge POST can't exhaust RAM/disk.
    written = 0
    with dest.open("wb") as out:
        while chunk := await file.read(1024 * 1024):
            written += len(chunk)
            if written > MAX_UPLOAD_BYTES:
                out.close()
                dest.unlink(missing_ok=True)
                raise HTTPException(413, f"Filen er større enn {MAX_UPLOAD_LABEL}")
            out.write(chunk)

    db.create_job(job_id, user_email, file.filename, str(dest), PROVIDER)
    _say(f"job={job_id} upload complete; dispatching background task")
    _spawn(_run_job(job_id, dest))
    return JSONResponse({"job_id": job_id})


@app.get("/status/{job_id}")
def job_status(
    job_id: str,
    full: bool = False,
    user_email: str = Depends(get_user_email),
) -> JSONResponse:
    """Job state for polling. The transcript body is only included with
    ?full=1 — pollers hit this every few seconds, and shipping (and parsing)
    hundreds of KB of segments per poll is pure waste until the caller
    actually wants them."""
    record = (db.get if full else db.get_status)(job_id, user_email)
    if not record:
        raise HTTPException(404, "Job not found")
    payload: dict = {
        "id": record["id"],
        "status": record["status"],
        "filename": record["filename"],
        "questions_done": bool(record.get("questions_done", 0)),
        "cleaned_done": bool(record.get("cleaned_done", 1)),
    }
    if full and record["status"] == "done" and record["transcript"]:
        payload["transcript"] = record["transcript"]
        payload["speaker_names"] = record.get("speaker_names", {})
    if record["status"] == "error":
        payload["error"] = record["error"]
    return JSONResponse(payload)


@app.get("/audio/{job_id}")
def audio(job_id: str, user_email: str = Depends(get_user_email)) -> FileResponse:
    record = db.get(job_id, user_email)
    if not record:
        raise HTTPException(404, "Job not found")
    audio_path = Path(record["audio_path"])
    if not audio_path.exists():
        raise HTTPException(404, "Lydfilen finnes ikke lenger på serveren")
    # Name it after the file actually served: a compressed WAV upload is an
    # .mp3 now, and the extension drives the Content-Type browsers rely on.
    served_name = Path(record["filename"]).stem + audio_path.suffix
    return FileResponse(audio_path, filename=served_name)


# Hard cap on clip length so the endpoint can't be used to re-download whole
# recordings (and so a typo'd range can't tie up ffmpeg for minutes).
MAX_CLIP_SECONDS = 600

# Clips keep the source container so stream-copy stays lossless. .mp4 is the
# exception: it may carry a video track, which we strip, so the audio-only
# output needs an .m4a container instead.
_CLIP_EXT_OVERRIDES = {".mp4": ".m4a"}


def _clip_stamp(seconds: float) -> str:
    """Positional stamp for clip filenames: 02.13 or 1.02.13 past the hour."""
    s = int(seconds)
    h, m, sec = s // 3600, (s % 3600) // 60, s % 60
    return f"{h}.{m:02d}.{sec:02d}" if h else f"{m:02d}.{sec:02d}"


async def _extract_clip(src: Path, out_path: Path, start: float, length: float) -> None:
    """Cut [start, start+length] from src into out_path, raising on failure.

    Tries a lossless stream-copy first (-ss before -i = fast input seek; cut
    lands on a packet boundary, ~25 ms granularity — inaudible). Falls back to
    an mp3 re-encode for codec/container combos where copy fails. The fallback
    REPLACES out_path's suffix, so callers must re-read it after this returns.
    """
    base = [
        "ffmpeg", "-y", "-v", "error",
        "-ss", f"{start:.3f}",
        "-i", str(src),
        "-t", f"{length:.3f}",
        "-vn",  # drop video/cover art so .mp4 sources come out audio-only
    ]
    result = await asyncio.to_thread(
        _run_tool, [*base, "-c:a", "copy", str(out_path)], 120.0
    )
    if result.returncode == 0 and out_path.stat().st_size > 0:
        return

    out_path.unlink(missing_ok=True)
    result = await asyncio.to_thread(
        _run_tool,
        [*base, "-c:a", "libmp3lame", "-q:a", "2", str(out_path.with_suffix(".mp3"))],
        120.0,
    )
    if result.returncode != 0:
        out_path.with_suffix(".mp3").unlink(missing_ok=True)
        raise RuntimeError(f"ffmpeg clip failed: {result.stderr.decode().strip()[-300:]}")


@app.get("/clip/{job_id}")
async def clip(
    job_id: str,
    start: float,
    end: float,
    user_email: str = Depends(get_user_email),
) -> FileResponse:
    """Cut [start, end] out of the original audio and serve it as a download."""
    record = db.get(job_id, user_email)
    if not record:
        raise HTTPException(404, "Job not found")
    src = Path(record["audio_path"])
    if not src.exists():
        raise HTTPException(404, "Lydfilen finnes ikke lenger på serveren")

    if not (math.isfinite(start) and math.isfinite(end)):
        raise HTTPException(400, "Ugyldig tidsintervall")
    start = max(0.0, start)
    duration = record.get("duration_seconds")
    if duration:
        end = min(end, float(duration))
    if end <= start:
        raise HTTPException(400, "Sluttid må være etter starttid")
    if end - start > MAX_CLIP_SECONDS:
        raise HTTPException(400, f"Maks klippelengde er {MAX_CLIP_SECONDS // 60} minutter")

    ext = src.suffix.lower()
    out_ext = _CLIP_EXT_OVERRIDES.get(ext, ext)
    tmp = tempfile.NamedTemporaryFile(suffix=out_ext, delete=False)
    tmp.close()
    out_path = Path(tmp.name)

    try:
        await _extract_clip(src, out_path, start, end - start)
    except Exception as exc:
        out_path.unlink(missing_ok=True)
        _say(f"job={job_id} clip FAILED start={start} end={end}: {exc}")
        raise HTTPException(500, "Klipping feilet") from exc

    # The copy->mp3 fallback may have switched the suffix; pick whichever exists.
    if not out_path.exists():
        out_path = out_path.with_suffix(".mp3")

    stem = Path(record["filename"]).stem or "klipp"
    download_name = f"{stem}_{_clip_stamp(start)}-{_clip_stamp(end)}{out_path.suffix}"
    _say(f"job={job_id} clip served start={start:.2f} end={end:.2f} name={download_name}")
    return FileResponse(
        out_path,
        filename=download_name,
        # Temp file is deleted right after the response body has been sent.
        background=BackgroundTask(out_path.unlink, missing_ok=True),
    )


@app.post("/t/{job_id}/delete")
def delete_transcript(
    job_id: str, user_email: str = Depends(get_user_email)
) -> RedirectResponse:
    audio_path = db.delete(job_id, user_email)
    if audio_path:
        Path(audio_path).unlink(missing_ok=True)
    return RedirectResponse("/", status_code=303)


# ------------------------------------------------------------------------------
# Export. Same segment data, four containers: plain text for pasting, srt/vtt
# for video subtitling workflows, docx for the newsroom's Word-shaped world.
# ------------------------------------------------------------------------------

_EXPORT_MEDIA_TYPES = {
    "txt": "text/plain; charset=utf-8",
    "srt": "application/x-subrip",
    "vtt": "text/vtt",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


def _subtitle_stamp(seconds: float, sep: str) -> str:
    """SRT/VTT timestamp: HH:MM:SS,mmm (srt) or HH:MM:SS.mmm (vtt)."""
    ms = max(0, round(seconds * 1000))
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


def _attachment_headers(filename: str) -> dict[str, str]:
    """Content-Disposition that survives Norwegian filenames (æøå).

    HTTP headers are latin-1; non-ASCII goes in the RFC 5987 filename* form
    with an ASCII-stripped fallback for ancient clients.
    """
    ascii_name = filename.encode("ascii", "ignore").decode() or "transkripsjon"
    return {
        "Content-Disposition": (
            f'attachment; filename="{ascii_name}"; '
            f"filename*=UTF-8''{quote(filename)}"
        )
    }


def _export_docx(segments: list[dict], texts: list[str], title: str) -> bytes:
    from docx import Document

    doc = Document()
    doc.add_heading(title, level=1)
    for seg, text in zip(segments, texts, strict=True):
        para = doc.add_paragraph()
        run = para.add_run(text)
        run.italic = bool(seg.get("is_question"))
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


@app.get("/t/{job_id}/export")
def export_transcript(
    job_id: str,
    format: str = "txt",
    mode: str = "cleaned",
    user_email: str = Depends(get_user_email),
) -> Response:
    if format not in _EXPORT_MEDIA_TYPES:
        raise HTTPException(400, f"Ukjent format '{format}'")
    record = db.get(job_id, user_email)
    if not record:
        raise HTTPException(404, "Transkripsjon ikke funnet")
    if not record["transcript"]:
        raise HTTPException(404, "Ingen transkripsjon å eksportere")

    segments = record["transcript"]["segments"]
    # mode=cleaned mirrors the UI's Renset view: cleaned text where the
    # cleanup pass produced it, raw verbatim otherwise.
    texts = [
        (seg.get("cleaned_text") if mode == "cleaned" and seg.get("cleaned_text") else seg["text"])
        for seg in segments
    ]

    stem = Path(record["filename"]).stem or "transkripsjon"
    if format == "txt":
        body: bytes | str = "\n\n".join(texts) + "\n"
    elif format == "srt":
        body = "\n".join(
            f"{i}\n{_subtitle_stamp(seg['start_seconds'], ',')} --> "
            f"{_subtitle_stamp(seg['end_seconds'], ',')}\n{text}\n"
            for i, (seg, text) in enumerate(zip(segments, texts, strict=True), start=1)
        )
    elif format == "vtt":
        body = "WEBVTT\n\n" + "\n".join(
            f"{_subtitle_stamp(seg['start_seconds'], '.')} --> "
            f"{_subtitle_stamp(seg['end_seconds'], '.')}\n{text}\n"
            for seg, text in zip(segments, texts, strict=True)
        )
    else:
        body = _export_docx(segments, texts, record["filename"])

    return Response(
        body,
        media_type=_EXPORT_MEDIA_TYPES[format],
        headers=_attachment_headers(f"{stem}.{format}"),
    )


# ------------------------------------------------------------------------------
# Speaker naming. Diarized transcripts (ElevenLabs) carry a speaker id per
# segment; the user can attach display names that live in one JSON map per
# transcript.
# ------------------------------------------------------------------------------


class SpeakerRename(BaseModel):
    speaker_id: str = Field(min_length=1, max_length=64)
    # Empty name clears the custom label (UI falls back to "Taler N").
    name: str = Field(max_length=80)


@app.post("/t/{job_id}/speakers")
def rename_speaker(
    job_id: str,
    payload: SpeakerRename,
    user_email: str = Depends(get_user_email),
) -> JSONResponse:
    if not db.set_speaker_name(job_id, user_email, payload.speaker_id, payload.name.strip()):
        raise HTTPException(404, "Transkripsjon ikke funnet")
    return JSONResponse({"ok": True})


# ------------------------------------------------------------------------------
# Service-to-service API — authenticated by EVALS_API_KEY header instead of
# the session cookie so the unyttkassa admin dashboard can pull usage stats.
# Bypasses AuthMiddleware via the /api/stats prefix exemption in src/auth.py;
# the endpoint then guards its own access below.
# ------------------------------------------------------------------------------

_EXPECTED_API_KEY = os.environ.get("EVALS_API_KEY", "")


def _require_api_key(x_api_key: str | None = Header(None)) -> None:
    if not _EXPECTED_API_KEY:
        # In dev (no key set), service-to-service API is disabled. We
        # don't want to silently let unauthenticated callers through.
        raise HTTPException(503, "Service-to-service API not configured")
    # compare_digest: constant-time comparison so response timing can't be
    # used to guess the key byte by byte.
    if not x_api_key or not secrets.compare_digest(x_api_key, _EXPECTED_API_KEY):
        raise HTTPException(401, "Invalid or missing API key")


def _disk_usage() -> dict[str, int]:
    """Bytes and file count for uploaded audio (the SQLite DB is excluded)."""
    total = 0
    count = 0
    for p in UPLOAD_DIR.iterdir():
        # startswith also skips SQLite's -wal/-shm sidecar files.
        if p.is_file() and not p.name.startswith(db.DB_PATH.name):
            total += p.stat().st_size
            count += 1
    return {"audio_bytes": total, "audio_files": count}


@app.get("/api/stats", dependencies=[Depends(_require_api_key)])
def api_stats() -> JSONResponse:
    """Usage metadata for the unyttkassa admin dashboard.

    Returns totals, a per-provider breakdown, disk usage for the uploads
    mount, and the 100 most recent runs. No transcript content is exposed —
    metadata only.
    """
    stats = db.get_stats()
    stats["disk"] = _disk_usage()
    return JSONResponse(stats)
