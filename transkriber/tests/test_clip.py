"""Tests for the clip-extraction endpoint (GET /clip/{job_id}).

Range validation runs before ffmpeg is invoked, so the 400/404 cases get away
with a fake-bytes audio file. The actual cutting tests generate real audio
with ffmpeg's sine generator and are skipped when ffmpeg isn't installed.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from .conftest import TEST_EMAIL

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="ffmpeg not installed"
)


def _insert_job(
    tmp_path: Path,
    *,
    job_id: str = "clipjob",
    real_audio: bool = False,
    seconds: float = 4.0,
    ext: str = ".wav",
    duration_in_db: float | None = None,
) -> Path:
    """Create a DB row + audio file on disk; returns the audio path."""
    import src.db as db

    audio = tmp_path / f"source{ext}"
    if real_audio:
        subprocess.run(
            [
                "ffmpeg", "-y", "-v", "error",
                "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
                "-ac", "1", "-ar", "8000",
                *(["-c:a", "aac"] if ext == ".mp4" else []),
                str(audio),
            ],
            check=True,
            capture_output=True,
        )
    else:
        audio.write_bytes(b"not-really-audio")
    db.create_job(job_id, TEST_EMAIL, f"interview{ext}", str(audio))
    if duration_in_db is not None:
        db.set_duration(job_id, duration_in_db)
    return audio


def _probe(path: Path) -> float:
    out = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "csv=p=0",
            str(path),
        ],
        check=True,
        capture_output=True,
    )
    return float(out.stdout.strip())


# ------------------------------------------------------------------------------
# Auth / existence.
# ------------------------------------------------------------------------------


def test_clip_unknown_job_returns_404(client):
    resp = client.get("/clip/nope", params={"start": 0, "end": 1})
    assert resp.status_code == 404


def test_clip_other_users_job_returns_404(client, tmp_path):
    import src.db as db

    audio = tmp_path / "x.wav"
    audio.write_bytes(b"x")
    db.create_job("theirs", "stranger@example.com", "x.wav", str(audio))

    resp = client.get("/clip/theirs", params={"start": 0, "end": 1})
    assert resp.status_code == 404


def test_clip_missing_audio_file_returns_404(client, tmp_path):
    import src.db as db

    db.create_job("gone", TEST_EMAIL, "x.wav", str(tmp_path / "deleted.wav"))

    resp = client.get("/clip/gone", params={"start": 0, "end": 1})
    assert resp.status_code == 404


# ------------------------------------------------------------------------------
# Range validation. Boundaries: end<=start edge, the 600 s cap (above), and
# non-finite floats. All rejected before ffmpeg runs.
# ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "start,end,detail_substr",
    [
        (5.0, 5.0, "Sluttid"),  # end == start — the edge of `end <= start`
        (5.0, 4.9, "Sluttid"),
        (0.0, 600.001, "Maks"),  # just above the 600 s cap
        (float("nan"), 5.0, "Ugyldig"),
        (0.0, float("inf"), "Ugyldig"),
    ],
    ids=["end-equals-start", "end-before-start", "above-cap", "nan-start", "inf-end"],
)
def test_clip_rejects_invalid_ranges(client, tmp_path, start, end, detail_substr):
    _insert_job(tmp_path)

    resp = client.get("/clip/clipjob", params={"start": start, "end": end})
    assert resp.status_code == 400
    assert detail_substr in resp.json()["detail"]


# ------------------------------------------------------------------------------
# Cutting. Real ffmpeg, real audio; output duration verified with ffprobe.
# ------------------------------------------------------------------------------


@needs_ffmpeg
def test_clip_cuts_requested_range(client, tmp_path):
    _insert_job(tmp_path, real_audio=True, seconds=4.0)

    resp = client.get("/clip/clipjob", params={"start": 1.0, "end": 2.0})
    assert resp.status_code == 200
    assert 'filename="interview_00.01-00.02.wav"' in resp.headers["content-disposition"]

    out = tmp_path / "out.wav"
    out.write_bytes(resp.content)
    assert _probe(out) == pytest.approx(1.0, abs=0.15)


@needs_ffmpeg
def test_clip_negative_start_clamps_to_zero(client, tmp_path):
    _insert_job(tmp_path, real_audio=True, seconds=4.0)

    resp = client.get("/clip/clipjob", params={"start": -5.0, "end": 2.0})
    assert resp.status_code == 200
    assert 'filename="interview_00.00-00.02.wav"' in resp.headers["content-disposition"]

    out = tmp_path / "out.wav"
    out.write_bytes(resp.content)
    assert _probe(out) == pytest.approx(2.0, abs=0.15)


@needs_ffmpeg
def test_clip_end_clamps_to_db_duration(client, tmp_path):
    _insert_job(tmp_path, real_audio=True, seconds=4.0, duration_in_db=4.0)

    resp = client.get("/clip/clipjob", params={"start": 3.0, "end": 9999.0})
    assert resp.status_code == 200
    assert 'filename="interview_00.03-00.04.wav"' in resp.headers["content-disposition"]

    out = tmp_path / "out.wav"
    out.write_bytes(resp.content)
    assert _probe(out) == pytest.approx(1.0, abs=0.2)


@needs_ffmpeg
def test_clip_length_exactly_at_cap_is_allowed(client, tmp_path):
    """600 s is the max clip length; exactly at the cap must pass validation.

    The source is only 4 s — ffmpeg stops at EOF — so a 200 with ~4 s of audio
    proves the request got past the cap check and through the cutter.
    """
    _insert_job(tmp_path, real_audio=True, seconds=4.0)

    resp = client.get("/clip/clipjob", params={"start": 0.0, "end": 600.0})
    assert resp.status_code == 200

    out = tmp_path / "out.wav"
    out.write_bytes(resp.content)
    assert _probe(out) == pytest.approx(4.0, abs=0.2)


@needs_ffmpeg
def test_clip_smallest_range_one_second(client, tmp_path):
    """1 s is the smallest range the UI can request (MIN_LEN client-side)."""
    _insert_job(tmp_path, real_audio=True, seconds=4.0)

    resp = client.get("/clip/clipjob", params={"start": 1.0, "end": 2.0})
    assert resp.status_code == 200
    assert len(resp.content) > 0


@needs_ffmpeg
def test_clip_mp4_source_downloads_as_m4a(client, tmp_path):
    """.mp4 may carry video, so clips remux to an audio-only .m4a."""
    _insert_job(tmp_path, real_audio=True, seconds=4.0, ext=".mp4")

    resp = client.get("/clip/clipjob", params={"start": 0.0, "end": 1.0})
    assert resp.status_code == 200
    assert 'filename="interview_00.00-00.01.m4a"' in resp.headers["content-disposition"]


# ------------------------------------------------------------------------------
# Filename stamps. Boundaries: minute rollover at 60, hour rollover at 3600,
# fractional truncation.
# ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "seconds,expected",
    [
        (0, "00.00"),
        (59, "00.59"),
        (60, "01.00"),
        (3599, "59.59"),
        (3600, "1.00.00"),
        (3661.9, "1.01.01"),
    ],
)
def test_clip_stamp(seconds, expected):
    from src.main import _clip_stamp

    assert _clip_stamp(seconds) == expected
