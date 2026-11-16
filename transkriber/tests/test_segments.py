"""Unit tests for _elevenlabs_words_to_segments — pure function, tricky
boundary logic (speaker changes, pause breaks, the soft time cap, spacing
tokens). No network, no fixtures.

src.transcribe is imported lazily inside each test (codebase convention):
importing it constructs the pydantic-ai Agents, which need the API-key env
var that conftest's autouse fixture only sets once tests start running.
"""

from __future__ import annotations


def _word(text: str, start: float, end: float, speaker: str = "speaker_0") -> dict:
    return {"type": "word", "text": text, "start": start, "end": end, "speaker_id": speaker}


def _space(start: float, end: float) -> dict:
    return {"type": "spacing", "text": " ", "start": start, "end": end}


def test_empty_words_give_no_segments():
    from src.transcribe import _elevenlabs_words_to_segments

    assert _elevenlabs_words_to_segments([]) == []


def test_single_speaker_single_segment_with_replikk_dash():
    from src.transcribe import _elevenlabs_words_to_segments

    words = [_word("Hei", 0.0, 0.4), _space(0.4, 0.5), _word("der.", 0.5, 0.9)]
    segments = _elevenlabs_words_to_segments(words)
    assert len(segments) == 1
    assert segments[0].text == "– Hei der."
    assert segments[0].start_seconds == 0.0
    assert segments[0].end_seconds == 0.9
    assert segments[0].speaker == "speaker_0"


def test_speaker_change_breaks_segment_and_keeps_ids():
    from src.transcribe import _elevenlabs_words_to_segments

    words = [
        _word("Navn?", 0.0, 0.5, "speaker_0"),
        _word("Kari.", 1.0, 1.4, "speaker_1"),
    ]
    segments = _elevenlabs_words_to_segments(words)
    assert [s.speaker for s in segments] == ["speaker_0", "speaker_1"]
    assert [s.text for s in segments] == ["– Navn?", "– Kari."]


def test_long_pause_breaks_segment_even_without_speaker_change():
    from src.transcribe import (
        ELEVENLABS_PAUSE_BREAK_SECONDS,
        _elevenlabs_words_to_segments,
    )

    gap = ELEVENLABS_PAUSE_BREAK_SECONDS + 0.1
    words = [
        _word("Først.", 0.0, 0.5),
        _word("Senere.", 0.5 + gap, 1.0 + gap),
    ]
    segments = _elevenlabs_words_to_segments(words)
    assert len(segments) == 2


def test_short_pause_does_not_break_segment():
    from src.transcribe import (
        ELEVENLABS_PAUSE_BREAK_SECONDS,
        _elevenlabs_words_to_segments,
    )

    gap = ELEVENLABS_PAUSE_BREAK_SECONDS - 0.5
    words = [
        _word("Først", 0.0, 0.5),
        _word("senere.", 0.5 + gap, 1.0 + gap),
    ]
    assert len(_elevenlabs_words_to_segments(words)) == 1


def test_monologue_over_time_cap_is_split():
    from src.transcribe import (
        ELEVENLABS_MAX_SEGMENT_SECONDS,
        _elevenlabs_words_to_segments,
    )

    cap = ELEVENLABS_MAX_SEGMENT_SECONDS
    words = [_word(f"ord{i}", i * 1.0, i * 1.0 + 0.8) for i in range(int(cap) + 10)]
    segments = _elevenlabs_words_to_segments(words)
    assert len(segments) >= 2
    assert all(s.end_seconds - s.start_seconds <= cap + 1.0 for s in segments)


def test_leading_spacing_token_does_not_open_a_segment():
    from src.transcribe import _elevenlabs_words_to_segments

    words = [_space(0.0, 0.1), _word("Hei.", 0.1, 0.5)]
    segments = _elevenlabs_words_to_segments(words)
    assert len(segments) == 1
    assert segments[0].text == "– Hei."
    assert segments[0].start_seconds == 0.1


# ------------------------------------------------------------------------------
# _strip_enum_prefix — guards the filler-cleanup pass against the model echoing
# the input's "0: " line index back into the cleaned text.
# ------------------------------------------------------------------------------


def test_strip_enum_prefix_removes_leaked_index():
    from src.transcribe import _strip_enum_prefix

    assert _strip_enum_prefix("0: – Hei der.") == "– Hei der."
    assert _strip_enum_prefix("12: – Et svar.") == "– Et svar."
    assert _strip_enum_prefix("3. – Punktum-variant.") == "– Punktum-variant."


def test_strip_enum_prefix_leaves_clean_text_untouched():
    from src.transcribe import _strip_enum_prefix

    # Real replikker start with the dash, so nothing is stripped — including
    # text that legitimately contains a year.
    assert _strip_enum_prefix("– Det startet i 2008.") == "– Det startet i 2008."
    assert _strip_enum_prefix("– 150 elever møtte opp.") == "– 150 elever møtte opp."
