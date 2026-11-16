"""Nothing key-shaped reaches a log or the verdicts table."""

from types import SimpleNamespace

import pytest
from google.genai import types

from radar import config, gemini

# Shaped like a Google API key, and not one: AIza + 35 characters.
FAKE = "AIza" + "S" * 35


@pytest.fixture(autouse=True)
def configured_key(monkeypatch):
    monkeypatch.setattr(config, "GEMINI_API_KEY", FAKE)


def test_the_configured_key_is_stripped():
    assert gemini.redact(f"401 from https://x/?key={FAKE}") == "401 from https://x/?key=***"


def test_a_key_the_config_never_saw_is_stripped_too():
    """A second project's key in a quoted header is still a key in our log."""
    other = "AIza" + "T" * 35
    assert gemini.redact(f"rejected {other}") == "rejected ***"


def test_a_key_quoted_back_through_repr_is_stripped(monkeypatch):
    """The case that put one in a log in full.

    A key read with a stray newline in it is rejected by the transport, which
    quotes the header using repr() — so the newline arrives as two characters
    and the string no longer equals the one we hold.
    """
    monkeypatch.setattr(config, "GEMINI_API_KEY", f"[venv] radar\n{FAKE}")
    message = f"Illegal header value b'[venv] radar\\n{FAKE}'"
    assert gemini.redact(message) == "Illegal header value b'[venv] radar\\n***'"


@pytest.mark.parametrize(
    "tail,redacted",
    (("S" * 34, False), ("S" * 35, True), ("S" * 36, True)),
    ids=["one-short", "exact", "one-over"],
)
def test_how_long_a_run_counts_as_a_key(monkeypatch, tail, redacted):
    """One character short is not a key; one over is a key with a character after it."""
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")  # shape is the only test here
    assert ("***" in gemini.redact(f"saw AIza{tail}")) is redacted


@pytest.mark.parametrize(
    "text",
    ("429 RESOURCE_EXHAUSTED", "", "AIzaSHORT", "the AIza prefix alone means nothing"),
)
def test_text_without_a_key_is_left_alone(text):
    assert gemini.redact(text) == text


def test_an_unset_key_does_not_redact_the_whole_message(monkeypatch):
    """An empty configured key must not turn into an empty-string replacement."""
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    assert gemini.redact("429 quota") == "429 quota"


def test_news_classification_explicitly_disables_adjustable_safety_filters():
    settings = gemini._generation_config("prompt", {}).safety_settings
    assert {setting.category for setting in settings} == {
        types.HarmCategory.HARM_CATEGORY_HARASSMENT,
        types.HarmCategory.HARM_CATEGORY_HATE_SPEECH,
        types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,
        types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,
    }
    assert all(setting.threshold == types.HarmBlockThreshold.OFF for setting in settings)


@pytest.mark.parametrize(
    "payload,expected",
    [
        ({"prompt_feedback": {"block_reason": "PROHIBITED_CONTENT"}},
         "blocked response: PROHIBITED_CONTENT"),
        ({"candidates": [{"finish_reason": "SAFETY"}]}, "blocked response: SAFETY"),
        ({"candidates": [{"finish_reason": "MAX_TOKENS"}]}, "empty response:"),
        ({}, "empty response:"),
    ],
)
def test_empty_responses_preserve_the_reason(monkeypatch, payload, expected):
    response = types.GenerateContentResponse(**payload)
    fake = SimpleNamespace(models=SimpleNamespace(generate_content=lambda **kwargs: response))
    monkeypatch.setattr(gemini, "client", lambda: fake)
    with pytest.raises(gemini.GeminiError, match=expected):
        gemini.generate_json("prompt", {}, "article")
