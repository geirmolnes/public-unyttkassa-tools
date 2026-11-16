"""Tests for kladd.main."""

import os

import pytest

from kladd.main import _ensure_google_api_key, _parse_args


class TestEnsureGoogleApiKey:
    def test_from_gemini(self, monkeypatch):
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
        monkeypatch.setenv("GEMINI_API_KEY", "gem-key-123")
        _ensure_google_api_key()
        assert os.environ["GOOGLE_API_KEY"] == "gem-key-123"

    def test_already_set(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_API_KEY", "existing-key")
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        _ensure_google_api_key()
        assert os.environ["GOOGLE_API_KEY"] == "existing-key"

    def test_missing(self, monkeypatch):
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
            _ensure_google_api_key()


class TestParseArgs:
    def test_basic(self, monkeypatch):
        monkeypatch.setattr("sys.argv", ["kladd", "file.pdf", "https://url.com", "-o", "out.md"])
        args = _parse_args()
        assert args.sources == ["file.pdf", "https://url.com"]
        assert str(args.output) == "out.md"
