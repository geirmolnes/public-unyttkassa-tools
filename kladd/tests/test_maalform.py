"""Tests for kladd.maalform."""

import pytest

from kladd.maalform import detect_nynorsk


NYNORSK_TEXT = (
    "Regjeringa vil ikkje seie kva dei meiner om saka. Det er berre "
    "eit av fleire spørsmål som står att. Kommunen fekk ikkje svar frå "
    "departementet, og ordføraren veit ikkje korleis det gjekk."
)

BOKMAL_TEXT = (
    "Regjeringen vil ikke si hva de mener om saken. Det er bare et av "
    "flere spørsmål som står igjen. Kommunen fikk ikke svar fra "
    "departementet, og ordføreren vet ikke hvordan det gikk."
)

DANISH_TEXT = (
    "Regeringen vil ikke sige hvad de mener om sagen. Det er blevet et "
    "af flere spørgsmål. Kommunen fik ikke svar fra ministeriet om det "
    "rigtige arbejde."
)

GERMAN_TEXT = (
    "Die Bundesregierung hat am Dienstag ein neues Gesetz beschlossen. Ein "
    "Sprecher sagte, es sei ein wichtiger Schritt für die Energiewende. Die "
    "Opposition kritisierte das Vorhaben als ein teures Projekt."
)

ITALIAN_TEXT = (
    "Il governo ha approvato il piano dei trasporti. I sindaci dei comuni "
    "del nord chiedono più fondi per la manutenzione dei ponti e dei treni."
)


@pytest.mark.parametrize(
    "texts,expected",
    [
        ([NYNORSK_TEXT], True),
        ([BOKMAL_TEXT], False),
        ([DANISH_TEXT], False),
        # Regression: "ein" (German) / "dei" (Italian) alone flipped these to nynorsk
        ([GERMAN_TEXT], False),
        ([ITALIAN_TEXT], False),
        ([], False),
        ([""], False),
        (["   "], False),
        # Mixed sources: bokmål hits outnumber nynorsk → bokmål wins
        ([NYNORSK_TEXT, BOKMAL_TEXT, BOKMAL_TEXT], False),
        # Nynorsk-dominant mix stays nynorsk
        ([NYNORSK_TEXT, NYNORSK_TEXT, "Kort tekst uten markører."], True),
    ],
    ids=[
        "nynorsk",
        "bokmal",
        "danish",
        "german",
        "italian",
        "no-texts",
        "empty-string",
        "whitespace",
        "bokmal-dominant-mix",
        "nynorsk-dominant-mix",
    ],
)
def test_detect_nynorsk(texts, expected):
    assert detect_nynorsk(texts) is expected


@pytest.mark.parametrize(
    "text,expected",
    [
        # Exactly at the minimum distinct-marker count (3), no bokmål hits
        ("ikkje kva berre", True),
        # One below the minimum
        ("ikkje kva", False),
        # Many hits but only one distinct marker
        ("ein ein ein ein ein", False),
        # Two distinct markers, repeated
        ("ein eit ein eit ein eit", False),
        # Case variants are the same marker
        ("Ein ein EIN eit", False),
        # Tie between nynorsk and bokmål hits → bokmål (strict majority required)
        ("ikkje kva berre ikke hva bare", False),
        # One nynorsk hit above the tie
        ("ikkje kva berre noko ikke hva bare", True),
    ],
    ids=[
        "at-min-distinct",
        "below-min-distinct",
        "one-distinct-repeated",
        "two-distinct-repeated",
        "case-insensitive-distinct",
        "tie",
        "majority-by-one",
    ],
)
def test_detect_nynorsk_thresholds(text, expected):
    assert detect_nynorsk([text]) is expected


def test_markers_are_word_bounded():
    # "ikkje"/"kva" as substrings of longer words must not count
    assert detect_nynorsk(["frikkjent kvalitet berrestad"]) is False
