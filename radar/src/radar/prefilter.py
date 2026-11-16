"""Layer 1: cheap lexical gate.

Tuned for recall, not precision. Whatever this drops is invisible to the LLM
and therefore invisible forever, so the rules err towards letting things
through — the expensive layer downstream is what says no.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .vocab import (
    ACRONYMS,
    COMPOUND_ROOTS,
    EDUCATION_NATIVE_SOURCES,
    PHRASES,
    STEM_STOPWORDS,
    STEM_TERMS,
)


def _alternation(terms: list[str]) -> str:
    return "|".join(re.escape(t) for t in terms)


def _compound_re() -> re.Pattern[str]:
    return re.compile(_alternation(COMPOUND_ROOTS))


def _stem_re() -> re.Pattern[str]:
    return re.compile(rf"\b(?:{_alternation(STEM_TERMS)})\w*")


def _phrase_re() -> re.Pattern[str]:
    return re.compile(_alternation(PHRASES))


def _acronym_re() -> re.Pattern[str]:
    return re.compile(rf"\b(?:{_alternation(ACRONYMS)})\b")


_COMPOUND = _compound_re()
_STEM = _stem_re()
_PHRASE = _phrase_re()
_ACRONYM = _acronym_re()  # applied to the original case, not the lowered text

# An article's topic lives in its headline, lede and opening paragraphs. A
# strong term there is enough; deeper down it is usually a passing mention.
_LEDE_CHARS = 500

# Past the lede, only sustained attention counts. Two stray roots in a 4000-char
# body matched crime reporting and foreign news — «skole» and «undervisning»
# turn up eventually in almost any long Norwegian article.
_MIN_BODY_ROOTS = 3
_MIN_BODY_HITS = 6


@dataclass
class PrefilterResult:
    """Verdict of the lexical gate, with the evidence that produced it."""

    passed: bool
    reason: str
    head_terms: list[str] = field(default_factory=list)
    body_terms: list[str] = field(default_factory=list)
    body_hits: int = 0

    @property
    def terms(self) -> list[str]:
        return sorted(set(self.head_terms) | set(self.body_terms))


def _strong_terms(text: str, raw: str) -> tuple[list[str], int]:
    """Distinct strong terms found, and the total number of occurrences."""
    stems = [w for w in _STEM.findall(text) if w not in STEM_STOPWORDS]
    found = _COMPOUND.findall(text) + stems + _PHRASE.findall(text) + _ACRONYM.findall(raw)
    return sorted({t.lower() for t in found}), len(found)


def prefilter(
    *, source: str, title: str | None, summary: str | None, body: str | None
) -> PrefilterResult:
    """Decide whether an article is worth an LLM call."""
    if source in EDUCATION_NATIVE_SOURCES:
        return PrefilterResult(True, "education-native source")

    body_raw = body or ""
    head_raw = " ".join(p for p in (title, summary, body_raw[:_LEDE_CHARS]) if p)
    rest_raw = body_raw[_LEDE_CHARS:]

    head_terms, _ = _strong_terms(head_raw.lower(), head_raw)
    if head_terms:
        return PrefilterResult(True, "strong term in title/lede", head_terms=head_terms)

    body_terms, body_hits = _strong_terms(rest_raw.lower(), rest_raw)
    if len(body_terms) >= _MIN_BODY_ROOTS or body_hits >= _MIN_BODY_HITS:
        return PrefilterResult(
            True, "sustained strong terms in body", body_terms=body_terms, body_hits=body_hits
        )

    return PrefilterResult(False, "no education vocabulary", body_terms=body_terms, body_hits=body_hits)
