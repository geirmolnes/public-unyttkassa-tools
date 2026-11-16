"""Tests for kladd.prompt."""

import pytest

from kladd.prompt import (
    MANUS_SYSTEM_PROMPT,
    SITAT_SYSTEM_PROMPT,
    SYSTEM_PROMPT,
    build_user_prompt,
)


class TestBuildUserPrompt:
    def test_urls_prefix(self):
        result = build_user_prompt("sources", 2, all_urls=True)
        assert "sitatsak" in result

    def test_general_prefix(self):
        result = build_user_prompt("sources", 2, all_urls=False)
        assert "nyhetsartikkel" in result
        assert "sitatsak" not in result

    def test_manus_prefix(self):
        result = build_user_prompt("sources", 2, manus=True)
        assert "journalistisk artikkel" in result
        assert "200–800 ord" in result

    def test_manus_ignores_all_urls(self):
        # In manus mode the article type is fixed; all_urls must not trigger sitatsak.
        result = build_user_prompt("sources", 2, all_urls=True, manus=True)
        assert "sitatsak" not in result

    def test_contains_sources(self):
        sources_text = "### Kilde 1\nSome content here"
        result = build_user_prompt(sources_text, 1)
        assert sources_text in result

    @pytest.mark.parametrize(
        "instructions,expected_present",
        [
            ("Vinkle på kommunens svar", True),
            ("", False),
            ("   ", False),
            (None, False),
        ],
        ids=["text", "empty", "whitespace-only", "none"],
    )
    def test_instructions(self, instructions, expected_present):
        result = build_user_prompt("sources", 1, instructions=instructions)
        assert ("Instruks fra journalisten" in result) is expected_present

    def test_instructions_trimmed(self):
        result = build_user_prompt("sources", 1, instructions="  maks 300 ord  ")
        assert "maks 300 ord" in result
        assert "maks 300 ord  " not in result


def test_system_prompt_exists():
    assert isinstance(SYSTEM_PROMPT, str)
    assert len(SYSTEM_PROMPT) > 0
    assert SYSTEM_PROMPT == SITAT_SYSTEM_PROMPT


def test_sitat_has_sitatsak_rules_manus_does_not():
    assert "Sitatsak" in SITAT_SYSTEM_PROMPT
    assert "Sitatsak" not in MANUS_SYSTEM_PROMPT
    assert "Lengde og økonomi" in MANUS_SYSTEM_PROMPT
    # Shared rules present in both.
    assert "Kildetroskap" in SITAT_SYSTEM_PROMPT
    assert "Kildetroskap" in MANUS_SYSTEM_PROMPT


class TestMaalform:
    @pytest.mark.parametrize("manus", [False, True], ids=["sitat", "manus"])
    def test_nynorsk_swaps_sprak_line(self, manus):
        from kladd.prompt import system_prompt

        nn = system_prompt(manus=manus, nynorsk=True)
        assert "Skriv på norsk nynorsk." in nn
        assert "Skriv på norsk bokmål." not in nn

    @pytest.mark.parametrize("manus", [False, True], ids=["sitat", "manus"])
    def test_default_is_bokmal(self, manus):
        from kladd.prompt import system_prompt

        bm = system_prompt(manus=manus)
        assert "Skriv på norsk bokmål." in bm
        assert "nynorsk" not in bm.lower()

    def test_constitution_still_has_bokmal_line(self):
        # The nynorsk variant is built via str.replace on this exact line;
        # if the constitution rewords it, the swap silently stops working.
        import unytt_editorial as ed

        assert "Skriv på norsk bokmål." in ed.SPRAK

    def test_module_constants_are_bokmal(self):
        assert "Skriv på norsk bokmål." in SITAT_SYSTEM_PROMPT
        assert "Skriv på norsk bokmål." in MANUS_SYSTEM_PROMPT
