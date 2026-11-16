"""Detect whether source material is written in Nynorsk.

The article follows the source's målform: Nynorsk sources give a Nynorsk
article, everything else (Bokmål, Danish, Swedish, English, mixed) gives
Bokmål as before.
"""

import re

# Function words that exist in exactly one of the two written norms.
# Content words are avoided — they show up in quotes and proper names.
_NYNORSK_MARKERS = re.compile(
    r"\b("
    r"ikkje|kva|korleis|kvifor|berre|noko|nokon|nokre|"
    r"hennar|dykkar|vore|gjekk|fekk|heiter|"
    r"ein|eit|dei|frå|òg"
    r")\b",
    re.IGNORECASE,
)

_BOKMAL_MARKERS = re.compile(
    r"\b("
    r"ikke|hva|hvordan|hvorfor|bare|noe|noen|"
    r"hennes|deres|vært|gikk|fikk|heter|"
    r"en|et|de|fra|også"
    r")\b",
    re.IGNORECASE,
)

# Distinct markers, not raw hits: a single shared word repeated — German
# "ein", Italian "dei" — must not make a foreign text look Nynorsk.
_MIN_DISTINCT = 3


def detect_nynorsk(texts: list[str]) -> bool:
    """True when the combined source text is predominantly Nynorsk."""
    combined = " ".join(t for t in texts if t)
    if not combined.strip():
        return False
    nn_hits = [m.lower() for m in _NYNORSK_MARKERS.findall(combined)]
    if len(set(nn_hits)) < _MIN_DISTINCT:
        return False
    bm = len(_BOKMAL_MARKERS.findall(combined))
    return len(nn_hits) > bm
