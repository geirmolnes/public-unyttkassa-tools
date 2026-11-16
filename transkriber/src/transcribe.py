"""Transcription pipeline.

Transcription runs on ElevenLabs Scribe: one HTTP call for any length of
audio, word-level timestamps, built-in diarization. Words are grouped into
speaker-turn segments here.

Two Gemini-powered text post-passes then enrich the transcript: question
detection (italics in the UI) and filler cleanup (the Renset/Rå toggle).
They only see text, never audio, so their cost is negligible.
"""

from __future__ import annotations

from src.usage_sdk import install as _install_usage
_install_usage()


import asyncio
import mimetypes
import os
import re
import subprocess
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict
from pydantic_ai import Agent

from .prompt import CLEANUP_PROMPT, QUESTION_PROMPT


def _say(msg: str) -> None:
    """stdout breadcrumb — uvicorn/Dokku captures stdout, logging was being dropped."""
    print(f"[transkriber.transcribe] {msg}", flush=True)

# Gemini model for the question-detection pass (text in, indices out).
MODEL_ID = os.environ.get("GEMINI_MODEL", "google-gla:gemini-3.5-flash")
# Separate model for the filler-cleanup pass. Flash Lite is ~30x faster for
# this workload (simple rewrite with strict output shape) and has more
# rate-limit headroom than the full Flash models. Override with
# CLEANUP_MODEL if needed.
CLEANUP_MODEL_ID = os.environ.get(
    "CLEANUP_MODEL", "google-gla:gemini-3.1-flash-lite"
)

# Shared settings for the post-pass agents: deterministic output, thinking
# off. The Gemini 3.x models think by default; these passes gain nothing
# from it and the thinking tokens only add latency and cost.
_GEMINI_SETTINGS = {
    "temperature": 0.0,
    "google_thinking_config": {"thinking_budget": 0},
}

# ElevenLabs Scribe config.
ELEVENLABS_ENDPOINT = "https://api.elevenlabs.io/v1/speech-to-text"
ELEVENLABS_MODEL_ID = os.environ.get("ELEVENLABS_MODEL_ID", "scribe_v2")
# Scribe returns nothing until it has processed the whole file, so this read
# timeout must cover processing of the longest tape: 45 min handles ~3 h of
# audio even at a slow 4x realtime. The outer _JOB_TIMEOUT_SECONDS caps us too.
ELEVENLABS_TIMEOUT_SECONDS = 2700.0
# Gap between words that forces a new segment even when the speaker doesn't
# change. Catches cases where diarization collapses to one speaker but the
# audio has clear turn-taking pauses. Tuned up from 1.5s because thinking
# pauses inside a single speaker's turn were being split into two replikker.
ELEVENLABS_PAUSE_BREAK_SECONDS = 2.5
# Soft cap for single-speaker monologue segments so the UI stays readable.
ELEVENLABS_MAX_SEGMENT_SECONDS = 45.0
# ElevenLabs returns ISO 639-3 codes; our DB/UI expect 639-1 to match Gemini.
_ELEVENLABS_LANG_MAP = {"nor": "no", "eng": "en", "swe": "sv", "dan": "da"}

# Budget for one question-detection LLM call; keeps a hung call from
# stalling the post-pass forever (the pass itself fails soft per batch).
QUESTION_PASS_TIMEOUT_SECONDS = 180


class Segment(BaseModel):
    """One block of speech.

    `is_question` is set by the post-processing pass (see _mark_questions)
    and used by the UI to italicise journalist questions. `cleaned_text` is
    populated by the filler-removal pass; when present, the UI offers a
    toggle between raw and cleaned views. `extra="ignore"` lets us silently
    drop legacy fields when reading DB rows written by older versions of
    the pipeline.
    """

    model_config = ConfigDict(extra="ignore")

    start_seconds: float
    end_seconds: float
    text: str
    is_question: bool = False
    cleaned_text: str | None = None
    # Diarization id ("speaker_0", ...) from Scribe; None on legacy rows
    # transcribed by the old Gemini backend. Display names live in the DB
    # per transcript (speaker_names_json) so users can rename without
    # rewriting every segment.
    speaker: str | None = None


class Transcript(BaseModel):
    model_config = ConfigDict(extra="ignore")

    language: str
    segments: list[Segment]


class _QuestionIndices(BaseModel):
    """Output shape for the question-detection pass."""

    indices: list[int]


class _CleanedTexts(BaseModel):
    """Output shape for the filler-removal pass.

    One string per input segment, same length and order. Using a flat list
    rather than index+text pairs means the model can't silently drop or
    re-order segments — we validate length on return.
    """

    texts: list[str]


_question_agent = Agent(
    MODEL_ID,
    output_type=_QuestionIndices,
    system_prompt=QUESTION_PROMPT,
    model_settings=_GEMINI_SETTINGS,
)

_cleanup_agent = Agent(
    CLEANUP_MODEL_ID,
    output_type=_CleanedTexts,
    system_prompt=CLEANUP_PROMPT,
    model_settings=_GEMINI_SETTINGS,
)

# Separate timeout for the cleanup pass — matches the question pass budget.
CLEANUP_PASS_TIMEOUT_SECONDS = 180


def _media_type_for(path: Path) -> str:
    """Map file extension to the MIME type sent with the Scribe upload."""
    ext = path.suffix.lower()
    overrides = {
        ".m4a": "audio/mp4",
        ".mp4": "audio/mp4",
        ".mp3": "audio/mpeg",
        ".wav": "audio/wav",
        ".flac": "audio/flac",
        ".ogg": "audio/ogg",
        ".aac": "audio/aac",
        ".aiff": "audio/aiff",
    }
    return overrides.get(ext) or mimetypes.guess_type(path.name)[0] or "audio/mpeg"


# ------------------------------------------------------------------------------
# ffprobe/ffmpeg helpers. We use subprocess.run (args list form, no shell)
# dispatched via asyncio.to_thread so we don't block the event loop. Also
# used by main.py for duration stamping and clip extraction.
# ------------------------------------------------------------------------------


def _run_tool(args: list[str], timeout: float) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(args, check=False, capture_output=True, timeout=timeout)


async def _probe_duration(path: Path) -> float:
    """Return the audio file's duration in seconds via ffprobe."""
    result = await asyncio.to_thread(_run_tool, [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "csv=p=0",
        str(path),
    ], 30.0)
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {result.stderr.decode().strip()}")
    return float(result.stdout.decode().strip())


# Above this bitrate an upload is lossless/PCM (WAV, AIFF, FLAC, ALAC): ~10x
# bigger than speech needs. Those get re-encoded once after upload. Ordinary
# compressed recordings (mp3/m4a/ogg top out at 320 kbps) are left untouched,
# so their clips stay lossless copies of what the journalist uploaded.
COMPRESS_ABOVE_BPS = 320_000
# Mono 128 kbps MP3: transparent enough for speech clips, ~86 MB per 1.5 h.
COMPRESSED_BITRATE = "128k"
COMPRESS_TIMEOUT_SECONDS = 1800.0


def needs_compression(bitrate_bps: int) -> bool:
    """True when the upload is lossless/PCM-sized and worth re-encoding."""
    return bitrate_bps > COMPRESS_ABOVE_BPS


async def probe_bitrate(path: Path) -> int:
    """Return the container's overall bitrate in bits/s via ffprobe."""
    result = await asyncio.to_thread(_run_tool, [
        "ffprobe", "-v", "error",
        "-show_entries", "format=bit_rate",
        "-of", "csv=p=0",
        str(path),
    ], 30.0)
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {result.stderr.decode().strip()}")
    return int(result.stdout.decode().strip())


async def compress_audio(src: Path) -> Path:
    """Re-encode src to mono MP3 at COMPRESSED_BITRATE; returns the new path.

    Encodes to a temp name first so a crash mid-encode never leaves a
    half-written file under the final name (a requeued job just re-encodes).
    """
    final = src.with_suffix(".mp3")
    tmp = src.with_name(f"{src.stem}.tmp.mp3")
    result = await asyncio.to_thread(_run_tool, [
        "ffmpeg", "-y", "-v", "error",
        "-i", str(src),
        "-vn", "-ac", "1",
        "-c:a", "libmp3lame", "-b:a", COMPRESSED_BITRATE,
        str(tmp),
    ], COMPRESS_TIMEOUT_SECONDS)
    if result.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"ffmpeg compress failed: {result.stderr.decode().strip()[-300:]}")
    tmp.replace(final)
    return final


# Post-passes run in batches so one bad LLM response (token-limit truncation,
# malformed output) only loses that batch instead of the whole pass. 50
# segments is comfortably inside output-token limits while still giving the
# model enough conversational context to tell questions from answers.
POST_PASS_BATCH_SIZE = 50


def _batched(segments: list[Segment], size: int) -> list[list[Segment]]:
    return [segments[i : i + size] for i in range(0, len(segments), size)]


async def _question_indices_for_batch(batch: list[Segment], offset: int) -> set[int]:
    """Flag one batch; returns GLOBAL segment indices. Raises on failure."""
    numbered = "\n".join(f"{i}: {s.text}" for i, s in enumerate(batch))
    result = await asyncio.wait_for(
        _question_agent.run(numbered),
        timeout=QUESTION_PASS_TIMEOUT_SECONDS,
    )
    return {offset + i for i in result.output.indices if 0 <= i < len(batch)}


async def mark_questions(transcript: Transcript) -> Transcript:
    """Second LLM pass: flag which segments are journalist questions.

    Runs in batches of POST_PASS_BATCH_SIZE; each batch fails soft, so a
    single bad LLM response leaves only that batch unflagged rather than
    losing the whole pass.
    """
    if not transcript.segments:
        return transcript

    batches = _batched(transcript.segments, POST_PASS_BATCH_SIZE)
    results = await asyncio.gather(
        *[
            _question_indices_for_batch(batch, n * POST_PASS_BATCH_SIZE)
            for n, batch in enumerate(batches)
        ],
        return_exceptions=True,
    )

    flagged_indices: set[int] = set()
    failed = 0
    for res in results:
        if isinstance(res, BaseException):
            failed += 1
            _say(f"question-detection batch FAILED ({type(res).__name__}: {res})")
        else:
            flagged_indices |= res
    _say(
        f"question-detection: {len(flagged_indices)}/{len(transcript.segments)} flagged "
        f"({len(batches) - failed}/{len(batches)} batches ok)"
    )
    if failed == len(batches):
        return transcript

    flagged = [
        s.model_copy(update={"is_question": i in flagged_indices})
        for i, s in enumerate(transcript.segments)
    ]
    return Transcript(language=transcript.language, segments=flagged)


# The cleanup input is a numbered list ("0: – text"); occasionally the model
# echoes the "0: " index back into the cleaned string. Strip a leading
# enumeration prefix defensively — real replikker start with "– ", never a
# bare digit, so this can never eat legitimate content.
_ENUM_PREFIX = re.compile(r"^\s*\d+\s*[:.)]\s*")


def _strip_enum_prefix(text: str) -> str:
    return _ENUM_PREFIX.sub("", text, count=1)


async def _cleaned_texts_for_batch(batch: list[Segment]) -> list[str]:
    """Clean one batch; returns exactly len(batch) texts. Raises on failure."""
    numbered = "\n".join(f"{i}: {s.text}" for i, s in enumerate(batch))
    result = await asyncio.wait_for(
        _cleanup_agent.run(numbered),
        timeout=CLEANUP_PASS_TIMEOUT_SECONDS,
    )
    if len(result.output.texts) != len(batch):
        raise ValueError(
            f"length mismatch: got {len(result.output.texts)} texts for {len(batch)} segments"
        )
    return [_strip_enum_prefix(t) for t in result.output.texts]


async def clean_fillers(transcript: Transcript) -> Transcript:
    """Third LLM pass: populate each segment's cleaned_text with fillers removed.

    Defensive — the prompt errs toward keeping text rather than dropping it.
    Runs in batches of POST_PASS_BATCH_SIZE; a batch that errors or returns a
    length mismatch leaves cleaned_text=None on just its own segments (the UI
    falls back to raw text for those) instead of discarding the whole pass.
    """
    if not transcript.segments:
        return transcript

    batches = _batched(transcript.segments, POST_PASS_BATCH_SIZE)
    results = await asyncio.gather(
        *[_cleaned_texts_for_batch(batch) for batch in batches],
        return_exceptions=True,
    )

    patched: list[Segment] = []
    cleaned_count = 0
    failed = 0
    for batch, res in zip(batches, results, strict=True):
        if isinstance(res, BaseException):
            failed += 1
            _say(f"filler-cleanup batch FAILED ({type(res).__name__}: {res}); batch stays raw")
            patched.extend(batch)
        else:
            cleaned_count += len(batch)
            patched.extend(
                s.model_copy(update={"cleaned_text": text})
                for s, text in zip(batch, res, strict=True)
            )
    _say(
        f"filler-cleanup: cleaned {cleaned_count}/{len(transcript.segments)} segments "
        f"({len(batches) - failed}/{len(batches)} batches ok)"
    )
    if failed == len(batches):
        return transcript
    return Transcript(language=transcript.language, segments=patched)


# ------------------------------------------------------------------------------
# ElevenLabs Scribe backend. Single-call transcription with word-level
# timestamps and built-in diarization. No chunking, no LLM for segmentation.
# ------------------------------------------------------------------------------


def _elevenlabs_words_to_segments(words: list[dict[str, Any]]) -> list[Segment]:
    """Group Scribe word-level tokens into speaker turns.

    Breaks on:
      - speaker change (diarization)
      - long silence between words (ELEVENLABS_PAUSE_BREAK_SECONDS)
      - soft time cap (ELEVENLABS_MAX_SEGMENT_SECONDS) at next word boundary,
        so a long monologue doesn't end up as one giant segment in the UI
    """
    segments: list[Segment] = []
    cur_tokens: list[str] = []
    cur_speaker: str | None = None
    cur_start = 0.0
    cur_end = 0.0

    def flush() -> None:
        if not cur_tokens:
            return
        text = "".join(cur_tokens).strip()
        if not text:
            return
        # Match the Norwegian replikk convention set by the Gemini prompt so
        # the two providers render identically in the UI.
        if not text.startswith("–"):
            text = f"– {text}"
        segments.append(
            Segment(
                start_seconds=cur_start,
                end_seconds=cur_end,
                text=text,
                is_question=False,
                speaker=cur_speaker,
            )
        )

    for w in words:
        ttype = w.get("type", "word")
        token = w.get("text", "")
        if ttype == "spacing":
            # Preserve whitespace inside the current segment; don't use
            # spacing tokens alone to start or end a segment.
            if cur_tokens:
                cur_tokens.append(token)
                end_v = w.get("end")
                if end_v is not None:
                    cur_end = float(end_v)
            continue

        start = float(w.get("start", 0.0))
        end = float(w.get("end", start))
        speaker = w.get("speaker_id")

        if cur_tokens:
            speaker_changed = speaker != cur_speaker
            long_pause = (start - cur_end) >= ELEVENLABS_PAUSE_BREAK_SECONDS
            over_cap = (end - cur_start) >= ELEVENLABS_MAX_SEGMENT_SECONDS
            if speaker_changed or long_pause or over_cap:
                flush()
                cur_tokens = []

        if not cur_tokens:
            cur_start = start
            cur_speaker = speaker

        cur_tokens.append(token)
        cur_end = end

    flush()
    return segments


async def transcribe_file(audio_path: Path) -> Transcript:
    """Transcribe any length of audio via ElevenLabs Scribe: one HTTP call.

    Scribe handles files up to several hours natively and returns word-level
    timestamps with speaker_ids. We group by speaker change / long pause
    into segments that match the shape the UI already renders.
    """
    api_key = os.environ.get("ELEVENLABS_API_KEY")
    if not api_key:
        raise RuntimeError("ELEVENLABS_API_KEY er ikke satt")

    _say(f"elevenlabs: POST {audio_path.stat().st_size} bytes model={ELEVENLABS_MODEL_ID}")

    # Pass the open file, not its bytes: httpx streams it in chunks, so a
    # multi-GB upload never has to fit in RAM.
    async with httpx.AsyncClient(timeout=ELEVENLABS_TIMEOUT_SECONDS) as client:
        with audio_path.open("rb") as audio_file:
            response = await client.post(
                ELEVENLABS_ENDPOINT,
                headers={"xi-api-key": api_key},
                files={"file": (audio_path.name, audio_file, _media_type_for(audio_path))},
                data={
                    "model_id": ELEVENLABS_MODEL_ID,
                    "diarize": "true",
                    "timestamps_granularity": "word",
                    "tag_audio_events": "false",
                },
            )
    if response.status_code != 200:
        # Surface the Scribe error body — auth/credit/format issues all land
        # here and the body is always short enough to log.
        raise RuntimeError(
            f"ElevenLabs {response.status_code}: {response.text[:500]}"
        )
    payload = response.json()

    segments = _elevenlabs_words_to_segments(payload.get("words", []))
    lang_raw = payload.get("language_code") or "none"
    language = _ELEVENLABS_LANG_MAP.get(lang_raw, lang_raw)

    _say(
        f"elevenlabs: done lang={language} "
        f"({lang_raw}) words={len(payload.get('words', []))} "
        f"segments={len(segments)} duration={payload.get('audio_duration_secs', '?')}s"
    )

    return Transcript(language=language, segments=segments)
