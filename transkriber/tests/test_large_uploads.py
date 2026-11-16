"""Long-interview uploads: the size cap, and compressing lossless uploads.

Regression for a 1.5 h WAV interview rejected by the old 500 MB cap. The
end-to-end tests generate real audio with ffmpeg and stub only the paid
ElevenLabs/Gemini calls; they're skipped when ffmpeg isn't installed.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

import pytest

from .conftest import TEST_EMAIL, _signed_cookie

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="ffmpeg not installed"
)


# ------------------------------------------------------------------------------
# Size cap.
# ------------------------------------------------------------------------------


def test_default_cap_fits_long_recorder_wav(monkeypatch):
    """1.5 h of 48 kHz / 24-bit stereo WAV (a pro recorder's top setting) is
    1 555 200 000 bytes. The old 500 MB cap rejected it."""
    import importlib

    import src.main as main_module

    monkeypatch.delenv("MAX_UPLOAD_MB", raising=False)
    importlib.reload(main_module)
    assert main_module.MAX_UPLOAD_BYTES >= 48_000 * 3 * 2 * 5400


@pytest.mark.parametrize(
    "mb,expected",
    [
        (500, "500 MB"),
        (1023, "1023 MB"),
        (1024, "1 GB"),
        (1025, "1025 MB"),
        (2048, "2 GB"),
    ],
)
def test_size_label(mb, expected):
    import src.main as main_module

    assert main_module._size_label(mb) == expected


def test_upload_page_shows_cap_in_gigabytes(client):
    html = client.get("/").text
    assert "opptil 2 GB" in html
    assert 'data-max-mb="2048"' in html


# ------------------------------------------------------------------------------
# Compression threshold. 320 kbps is the MP3 ceiling: a max-quality MP3 must
# be left alone, anything above it is lossless/PCM.
# ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bitrate,expected",
    [
        (1, False),
        (128_000, False),
        (319_999, False),
        (320_000, False),
        (320_001, True),
        (1_411_200, True),  # 44.1 kHz / 16-bit stereo WAV
    ],
)
def test_needs_compression(bitrate, expected):
    from src.transcribe import needs_compression

    assert needs_compression(bitrate) is expected


# ------------------------------------------------------------------------------
# End to end: upload -> background job -> served audio.
# ------------------------------------------------------------------------------


def _make_audio(path: Path, *codec_args: str) -> Path:
    """Write 3 s of stereo 44.1 kHz sine to path with the given codec args."""
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
            "-ac", "2", "-ar", "44100",
            *codec_args,
            str(path),
        ],
        check=True,
        capture_output=True,
    )
    return path


def _ffprobe(path: Path, entry: str) -> str:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", entry, "-of", "csv=p=0", str(path)],
        check=True,
        capture_output=True,
    )
    return out.stdout.decode().strip()


@pytest.fixture
def live_client(monkeypatch):
    """TestClient kept open so the upload's background job can finish, with
    the paid transcription and LLM passes stubbed out."""
    from fastapi.testclient import TestClient

    import src.main as main_module
    from src.transcribe import Transcript

    async def fake_transcribe(audio_path: Path) -> Transcript:
        return Transcript(language="no", segments=[])

    async def passthrough(transcript: Transcript) -> Transcript:
        return transcript

    monkeypatch.setattr(main_module, "transcribe_file", fake_transcribe)
    monkeypatch.setattr(main_module, "mark_questions", passthrough)
    monkeypatch.setattr(main_module, "clean_fillers", passthrough)

    with TestClient(
        main_module.app,
        headers={"Cookie": f"session={_signed_cookie(TEST_EMAIL)}"},
    ) as c:
        yield c


def _upload_and_wait(client, path: Path, filename: str) -> str:
    resp = client.post("/upload", files={"file": (filename, path.read_bytes(), "audio/wav")})
    assert resp.status_code == 200
    job_id = resp.json()["job_id"]
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        status = client.get(f"/status/{job_id}").json()["status"]
        if status in ("done", "error"):
            assert status == "done"
            return job_id
        time.sleep(0.1)
    raise AssertionError("job did not finish in 30 s")


@needs_ffmpeg
def test_wav_upload_is_compressed_to_mono_mp3(live_client, tmp_path):
    import src.db as db

    wav = _make_audio(tmp_path / "in.wav", "-c:a", "pcm_s16le")
    job_id = _upload_and_wait(live_client, wav, "interview.wav")

    stored = Path(db.get(job_id, TEST_EMAIL)["audio_path"])
    assert stored.suffix == ".mp3"
    assert not stored.with_suffix(".wav").exists()
    assert _ffprobe(stored, "stream=channels") == "1"
    assert float(_ffprobe(stored, "format=duration")) == pytest.approx(3.0, abs=0.1)


@needs_ffmpeg
def test_compressed_audio_is_served_as_mp3(live_client, tmp_path):
    wav = _make_audio(tmp_path / "in.wav", "-c:a", "pcm_s16le")
    job_id = _upload_and_wait(live_client, wav, "interview.wav")

    resp = live_client.get(f"/audio/{job_id}")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "audio/mpeg"
    assert 'filename="interview.mp3"' in resp.headers["content-disposition"]


@needs_ffmpeg
def test_compressed_mp3_upload_is_left_untouched(live_client, tmp_path):
    import src.db as db

    mp3 = _make_audio(tmp_path / "in.mp3", "-c:a", "libmp3lame", "-b:a", "128k")
    job_id = _upload_and_wait(live_client, mp3, "interview.mp3")

    stored = Path(db.get(job_id, TEST_EMAIL)["audio_path"])
    assert stored.read_bytes() == mp3.read_bytes()


def test_unprobeable_upload_keeps_original_and_still_transcribes(live_client):
    """Compression fails soft: garbage that ffprobe can't read is kept as-is
    and the job still completes."""
    import src.db as db

    resp = live_client.post(
        "/upload", files={"file": ("broken.wav", b"not-really-audio", "audio/wav")}
    )
    job_id = resp.json()["job_id"]
    deadline = time.monotonic() + 30
    while live_client.get(f"/status/{job_id}").json()["status"] not in ("done", "error"):
        assert time.monotonic() < deadline
        time.sleep(0.1)

    record = db.get(job_id, TEST_EMAIL)
    assert record["status"] == "done"
    assert Path(record["audio_path"]).read_bytes() == b"not-really-audio"


# ------------------------------------------------------------------------------
# Scribe upload streams the file instead of loading it into RAM. ElevenLabs is
# a paid external, so its transport is mocked.
# ------------------------------------------------------------------------------


async def test_scribe_request_streams_file_contents(monkeypatch, tmp_path):
    import httpx

    import src.transcribe as transcribe

    sent: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent["body"] = request.read()
        words = [
            {"text": "Hei", "start": 0.0, "end": 0.4, "type": "word", "speaker_id": "speaker_0"}
        ]
        return httpx.Response(200, json={"words": words, "language_code": "nor"})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        transcribe.httpx,
        "AsyncClient",
        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw),
    )
    monkeypatch.setenv("ELEVENLABS_API_KEY", "test-key")
    audio = tmp_path / "opptak.mp3"
    audio.write_bytes(b"ID3-fake-mp3-payload")

    transcript = await transcribe.transcribe_file(audio)

    assert b"ID3-fake-mp3-payload" in sent["body"]
    assert b'filename="opptak.mp3"' in sent["body"]
    assert transcript.language == "no"
