"""Shared fixtures for kladd tests."""

import base64
import hashlib
import json
import os
import tempfile
import time
from pathlib import Path

# Agent construction in kladd.drafter needs a Google key at import time;
# set a dummy so the suite collects without real credentials.
os.environ.setdefault("GOOGLE_API_KEY", "test-dummy-key")
# Fallbacks without a key are skipped; give the chain its full length.
os.environ.setdefault("ANTHROPIC_API_KEY", "test-dummy-key")
os.environ.setdefault("OPENROUTER_API_KEY", "test-dummy-key")

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from unytt_parser import ParsedSource, SourceType

# Importing session_token doesn't read settings; they're read lazily at the first request.
from kladd.frontend.session_token import SigningKey, sign_v2


def signing_key(label: str) -> SigningKey:
    """Deterministic Ed25519 test key, same derivation as the session-token repo."""
    seed = hashlib.sha256(f"session-token test key {label}".encode()).digest()
    return SigningKey(f"test-{label}", Ed25519PrivateKey.from_private_bytes(seed))


def v2_token(
    claims: dict,
    key: str = "A",
    issuer: str = "example.org",
    age: int = 0,
    ttl: int = 3600,
) -> str:
    """A v2 session token minted `age` seconds ago that lives `ttl` seconds."""
    return sign_v2(
        claims, signing_key(key), issuer=issuer, now=int(time.time()) - age, ttl=ttl
    )


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def v2_signed(body: object, key: str = "A") -> str:
    """Sign any JSON value as a v2 token body; skips sign_v2's checks (for bad-claims tests)."""
    payload = _b64(json.dumps(body).encode())
    return f"v2.{payload}.{_b64(signing_key(key).key.sign(f'v2.{payload}'.encode()))}"


# Session settings are read once, so they must be set before the app is imported.
# Only test key "A" is published.
_pub = signing_key("A").key.public_key().public_bytes_raw()
os.environ["SESSION_PUBLIC_KEYS"] = "test-A:" + _b64(_pub)
os.environ["SESSION_ISSUER"] = "example.org"


def make_source(
    *,
    source_id: str = "src-1",
    source_type: SourceType = SourceType.URL,
    source: str = "https://example.com/article",
    headline: str | None = "Test Headline",
    byline: str | None = "Test Author",
    publication_date: str | None = "2025-01-15",
    publication: str | None = "Test Publication",
    markdown: str | None = "Article body text.",
    pdf_bytes: bytes | None = None,
    error: str | None = None,
) -> ParsedSource:
    """Create a ParsedSource with sensible defaults."""
    return ParsedSource(
        source_id=source_id,
        source_type=source_type,
        source=source,
        headline=headline,
        byline=byline,
        publication_date=publication_date,
        publication=publication,
        markdown=markdown,
        pdf_bytes=pdf_bytes,
        error=error,
    )


@pytest.fixture()
def tmp_db(monkeypatch):
    """Point storage's database, PDF dir and profiling log to a temp dir, auto-cleanup."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        import kladd.storage as storage_mod

        monkeypatch.setattr(storage_mod, "DB_PATH", tmpdir / "test.sqlite")
        monkeypatch.setattr(storage_mod, "PDF_DIR", tmpdir / "pdfs")
        # Unpatched, save_profile appended to the real log in the developer's data dir.
        monkeypatch.setattr(storage_mod, "PROFILE_LOG", tmpdir / "profiling_log.md")
        yield tmpdir
