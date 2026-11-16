"""Verify that direct quotes in generated articles appear in source material."""

import json
import re
import unicodedata
from urllib.parse import urlparse

from kladd.quote_verify import verify_quotes


# Word-boundary tokens that should never appear in a Norwegian article.
# Hitting any of these means the quote is in a foreign language (or wrong
# målform) and must be routed to the LLM verifier for translation.
#
# Conservative list: only forms with structural features (digraphs, prefixes,
# Swedish-only diacritics) that don't exist in Norwegian spelling. Common
# Norwegian words that happen to be Danish too (meget, intet, noget, imod,
# giver, ...) are NOT in this list because false positives would force
# unnecessary re-translation of valid quotes.
_FOREIGN_MARKERS = (
    # Danish: «ej»-digraf (Norwegian uses «ei»), «ud-»-prefix (Norwegian «ut-»),
    # æ-ligature in Danish-specific words.
    r"arbejds?\w*|udfordr\w*|udtrykk\w*|udtalel\w*|udvikl\w*|udenfor|"
    r"tværtimod|tvært|fællesskab\w*|øgenavn\w*|"
    # Danish verb endings (Norwegian: -er→-er but specific stems differ)
    r"siger|kigger|"
    # Danish past participles where Norwegian uses different stems
    r"blevet|lykkedes|"
    # Danish definite/plural forms
    r"nogle|"
    # Danish «-gt» cluster where Norwegian writes «-kt»
    r"rigtigt|vigtigt|"
    # Other Danish-specific spellings
    r"billede\w*|hvorpå|indlevend\w*|"
    # Swedish ä/ö (don't appear in Norwegian) and Swedish-specific words
    r"mycket|också|någon|några|alltså|säger|säga|finns|"
    r"vilket|vilken"
)

# In a Bokmål article, Nynorsk-only forms are also foreign; in a Nynorsk
# article they are the expected målform and must NOT trigger re-translation.
_NON_BOKMAL_MARKERS = re.compile(
    r"\b(" + _FOREIGN_MARKERS + r"|ikkje|kva|berre|hennar)\b",
    re.IGNORECASE,
)
_NON_NYNORSK_MARKERS = re.compile(
    r"\b(" + _FOREIGN_MARKERS + r")\b",
    re.IGNORECASE,
)

# TLDs whose articles require thorough (LLM) verification of every quote.
# Norwegian sources may incidentally include foreign markers in a quote, but
# articles from these domains are written in a non-Bokmål language entirely.
_THOROUGH_TLDS = (".dk", ".se")


def _source_needs_thorough(url_or_path: str) -> bool:
    """True if the source URL's host TLD requires thorough verification."""
    try:
        host = urlparse(url_or_path).hostname or ""
    except ValueError:
        return False
    host = host.lower()
    return any(host.endswith(tld) for tld in _THOROUGH_TLDS)


def _quote_has_foreign_marker(quote: str, *, nynorsk: bool = False) -> bool:
    """True if the quote contains a token foreign to the article's målform."""
    markers = _NON_NYNORSK_MARKERS if nynorsk else _NON_BOKMAL_MARKERS
    return bool(markers.search(quote))


def _normalize(text: str) -> str:
    """Normalize whitespace and unicode for comparison."""
    # Normalize unicode (e.g. different dash/quote characters)
    text = unicodedata.normalize("NFKC", text)
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()
    return text.lower()


# Attribution verbs (presens + preteritum) that introduce ', <verb> Navn til Medium.'
_ATTRIBUTION_VERBS = (
    "sier|sa|skriver|skrev|svarer|svarte|spør|spurte|"
    "forteller|fortalte|opplyser|opplyste|mener|mente|"
    "understreker|understreket|påpeker|påpekte|fastslår|fastslo|"
    "uttaler|uttalte|forklarer|forklarte|fortsetter|fortsatte|"
    "legger|la|ifølge"
)

_ATTRIBUTION_RE = re.compile(
    rf",\s+(?:{_ATTRIBUTION_VERBS})\s+.{{2,80}}$",
    re.IGNORECASE,
)


def _strip_attribution(quote: str) -> str:
    """Remove trailing attribution like ', sier Navn Navnesen til VG.'"""
    return _ATTRIBUTION_RE.sub("", quote)


def strip_guillemets_in_dash_quotes(article: str) -> tuple[str, int]:
    """Remove guillemet wrappers from dash-prefixed quote lines.

    Article generators (especially smaller ones) sometimes produce
    `– «Quote», sier X til Y.` — combining tankestrek with anførselstegn,
    which violates NTB style. Cheap post-process to fix this regardless
    of the model.

    Returns (cleaned article, edit count).
    """
    edits = 0
    out_lines: list[str] = []
    for line in article.splitlines():
        stripped = line.lstrip()
        leading_ws = line[: len(line) - len(stripped)]
        m = re.match(r"^([–—])\s*[«\"](.*?)[»\"](.*)$", stripped, re.DOTALL)
        if m:
            dash, inner, tail = m.groups()
            edits += 1
            out_lines.append(f"{leading_ws}{dash} {inner.strip()}{tail}")
        else:
            out_lines.append(line)
    return "\n".join(out_lines), edits


def normalize_inner_quotes_in_dash_quotes(article: str) -> tuple[str, int]:
    """Convert ASCII/curly quotation marks inside dash-prefixed quote lines
    to Norwegian guillemets («»).

    Norwegian convention: nested quotations and emphasized terms inside a
    direct quote use «», not "..." or '...'. Smaller article models often
    produce ASCII or curly quotes inside dash-quotes; this normalizes them.

    The single-quote pass uses lookbehind/lookahead so contractions like
    `don't` are not affected — only paired quotes that open after
    whitespace/punctuation and close before whitespace/punctuation.

    Returns (cleaned article, edit count).
    """
    edits = 0
    out_lines: list[str] = []
    for line in article.splitlines():
        stripped = line.lstrip()
        if not re.match(r"^[–—]\s", stripped):
            out_lines.append(line)
            continue

        # Double quotes (ASCII " and curly “ ”) → «»
        new_line, n_d = re.subn(
            r'["“]([^"”\n]+?)["”]',
            r"«\1»",
            line,
        )
        # Single quotes (ASCII ' and curly ‘ ’) → «»
        # Constraints prevent matching contractions: opening must be
        # preceded by whitespace, opening punctuation, dash or guillemet;
        # closing must be followed by whitespace, closing punctuation,
        # dash, guillemet, or end of line.
        new_line, n_s = re.subn(
            r"(?<=[\s«(\[—–\-])['‘]([^'’\n]+?)['’](?=[\s,.;:!?»)\]\-—–]|$)",
            r"«\1»",
            new_line,
        )
        edits += n_d + n_s
        out_lines.append(new_line)
    return "\n".join(out_lines), edits


def nest_guillemets(article: str) -> tuple[str, int]:
    """Rewrite «» nested inside «» as single guillemets ‹›.

    Norwegian convention: a quotation inside a «»-excerpt uses ‹›, e.g.
    «Klager melder om ‹feilaktig gjengivelse›.» Depth is tracked per line.

    Returns (cleaned article, edit count).
    """
    edits = 0
    out_lines: list[str] = []
    for line in article.splitlines():
        out: list[str] = []
        depth = 0
        for char in line:
            if char == "«":
                depth += 1
                if depth >= 2:
                    char = "‹"
                    edits += 1
            elif char == "»":
                if depth >= 2:
                    char = "›"
                    edits += 1
                depth = max(0, depth - 1)
            out.append(char)
        out_lines.append("".join(out))
    return "\n".join(out_lines), edits


# Inline «...» spans this short are usually titles/emphasis, not quotes.
_MIN_INLINE_QUOTE_WORDS = 4


def extract_quotes(article: str) -> list[str]:
    """Extract direct quotes from article text.

    Covers both dash-prefixed quote lines (– Sitat, sier X.) and inline
    «...» quotations in running text (≥ _MIN_INLINE_QUOTE_WORDS words, so
    emphasized terms and titles are not treated as quotes). Inline quotes
    are returned without the guillemets.
    """
    quotes = []
    for line in article.splitlines():
        line = line.strip()
        # Match lines starting with en-dash or em-dash
        if re.match(r"^[–—]\s*", line):
            quote_text = re.sub(r"^[–—]\s*", "", line)
            if quote_text:
                quotes.append(quote_text)
            continue
        for inner in re.findall(r"«([^»]+)»", line):
            if len(inner.split()) >= _MIN_INLINE_QUOTE_WORDS:
                quotes.append(inner.strip())
    return quotes


def _quote_found_in_source(quote: str, combined_source: str) -> bool:
    """Check whether a single quote can be found in the combined source text."""
    core = _strip_attribution(quote)
    if _normalize(core) in combined_source:
        return True
    if _normalize(quote) in combined_source:
        return True
    sentences = re.split(r"[.!?]+\s*", core)
    sentences = [s.strip() for s in sentences if len(s.strip()) > 15]
    if sentences and all(_normalize(s) in combined_source for s in sentences):
        return True
    return False


def _find_source_snippet(quote: str, source_texts: list[str]) -> str | None:
    """Find the original source text that matches a quote."""
    core = _strip_attribution(quote)
    words = core.split()
    if not words:
        return None

    # Build a flexible regex from quote words (handles whitespace/newline differences)
    pattern = r"\s+".join(re.escape(w) for w in words)
    for source in source_texts:
        m = re.search(pattern, source, re.IGNORECASE | re.DOTALL)
        if m:
            return m.group(0)

    # Try individual sentences
    sentences = re.split(r"[.!?]+\s*", core)
    sentences = [s.strip() for s in sentences if len(s.strip()) > 15]
    if not sentences:
        return None

    snippets = []
    for sentence in sentences:
        s_words = sentence.split()
        if not s_words:
            continue
        pattern = r"\s+".join(re.escape(w) for w in s_words)
        for source in source_texts:
            m = re.search(pattern, source, re.IGNORECASE | re.DOTALL)
            if m:
                snippets.append(m.group(0))
                break
    return " [...] ".join(snippets) if snippets else None


def check_quotes_detailed(
    article: str,
    source_texts: list[str],
    source_urls: list[str] | None = None,
    *,
    nynorsk: bool = False,
) -> list[dict]:
    """Return list of {quote, ok, source, verified_by} for every quote in the article.

    A quote is marked ok=False (and routed to the LLM verifier) when any of:
      • literal substring match against source text fails
      • the quote contains a marker token foreign to the article's målform
      • any source URL has a TLD that requires thorough verification (.dk/.se)
    """
    quotes = extract_quotes(article)
    if not quotes:
        return []

    combined_source = _normalize(" ".join(source_texts))
    thorough_required = bool(source_urls) and any(
        _source_needs_thorough(u) for u in source_urls if u
    )

    result = []
    for q in quotes:
        literal_match = _quote_found_in_source(q, combined_source)
        has_marker = _quote_has_foreign_marker(q, nynorsk=nynorsk)

        ok = literal_match and not has_marker and not thorough_required
        if ok:
            verified_by = "literal"
            source = _find_source_snippet(q, source_texts)
        else:
            verified_by = None
            source = None

        entry = {
            "quote": q,
            "ok": ok,
            "source": source,
            "verified_by": verified_by,
        }
        if has_marker:
            entry["marker_hit"] = True
        if thorough_required:
            entry["thorough"] = True
        result.append(entry)
    return result


def apply_llm_verdicts(
    details: list[dict], verdicts: list, *, nynorsk: bool = False
) -> list[dict]:
    """Merge LLM verdicts into detail entries that failed the literal check.

    Verdicts must align one-to-one with the failing quotes (in order).
    For rejected quotes with a corrected_quote, stores the replacement so
    the caller can rewrite the article.

    A `marker_hit=True` entry has a regex-confirmed non-Bokmål token in the
    quote. The LLM is not allowed to override that signal — its `ok=True`
    is downgraded, and absence of a corrected_quote is treated as a
    translation failure rather than acceptance.
    """
    failing_idx = [i for i, d in enumerate(details) if not d["ok"]]
    for idx, v in zip(failing_idx, verdicts):
        details[idx]["reason"] = v.reason
        marker_hit = details[idx].get("marker_hit", False)

        if v.ok and not marker_hit:
            details[idx]["ok"] = True
            details[idx]["source"] = v.source_snippet
            details[idx]["verified_by"] = "llm"
            continue

        details[idx]["verified_by"] = "llm-rejected"
        if v.ok and marker_hit:
            details[idx]["verified_by"] = "llm-overridden"
            details[idx]["reason"] = (
                f"Regex flagged non-Bokmål marker; LLM mistakenly accepted. "
                f"LLM reason: {v.reason}"
            )
        if v.corrected_quote and v.corrected_quote.strip() != details[idx]["quote"].strip():
            # The LLM sometimes returns the corrected_quote with a leading
            # «– » that the article already has from the surrounding format,
            # which produces «– – ...» when the substitution is applied.
            # Strip leading dash markers so the replacement is clean.
            cleaned = re.sub(r"^[\s]*[–—]\s*", "", v.corrected_quote)
            details[idx]["original_quote"] = details[idx]["quote"]
            details[idx]["corrected_quote"] = cleaned
            details[idx]["edited"] = True
            details[idx]["source"] = v.source_snippet
            # Post-translation safety: if the corrected text still contains
            # a non-Bokmål marker, the LLM left untranslated residue. Apply
            # the edit anyway (better than the original) but keep ok=False
            # so the failure surfaces.
            if _quote_has_foreign_marker(cleaned, nynorsk=nynorsk):
                details[idx]["translation_residue"] = True
                details[idx]["verified_by"] = "llm-residue"
        elif marker_hit:
            # Marker-hit quote with no usable corrected_quote stays failed.
            # Better to surface the issue than silently accept Danish text.
            details[idx]["source"] = v.source_snippet
    return details


def apply_quote_edits(article: str, details: list[dict]) -> tuple[str, int]:
    """Rewrite the article, replacing rejected quotes with their corrections.

    Returns the new article text and the number of substitutions made.

    A correction with `translation_residue=True` is still applied (the
    partial translation is closer to Bokmål than the original) but ok stays
    False so callers can see the failure.
    """
    edits = 0
    for d in details:
        if not d.get("edited"):
            continue
        original = d["original_quote"]
        corrected = d["corrected_quote"]
        if original in article:
            article = article.replace(original, corrected, 1)
            edits += 1
            if not d.get("translation_residue"):
                d["ok"] = True
    return article, edits


async def verify_failing_quotes(
    article: str,
    details: list[dict],
    source_texts: list[str],
    *,
    nynorsk: bool = False,
) -> tuple[str, int]:
    """Run LLM verification on quotes marked ok=False, apply corrections.

    Returns (possibly-edited article, number of edits applied). Mutates
    `details` in place — verdicts are merged via apply_llm_verdicts.
    """
    failing_idx = [i for i, d in enumerate(details) if not d["ok"]]
    if not failing_idx:
        return article, 0
    failing = [details[i]["quote"] for i in failing_idx]
    forced = [
        details[i].get("marker_hit", False) or details[i].get("thorough", False)
        for i in failing_idx
    ]
    verdicts = await verify_quotes(failing, source_texts, forced, nynorsk=nynorsk)
    apply_llm_verdicts(details, verdicts, nynorsk=nynorsk)
    return apply_quote_edits(article, details)


def apply_quote_typography(article: str) -> str:
    """Run all typography post-processing on quote lines.

    Combines:
      • strip_guillemets_in_dash_quotes — removes wrong-style «» wrapping
        around dash-prefixed quotes
      • normalize_inner_quotes_in_dash_quotes — converts inner ASCII or
        curly quotes to Norwegian guillemets (for nested quotations)
      • nest_guillemets — rewrites «» nested inside «» as ‹›
    """
    article, _ = strip_guillemets_in_dash_quotes(article)
    article, _ = normalize_inner_quotes_in_dash_quotes(article)
    article, _ = nest_guillemets(article)
    return article


async def verify_and_apply(
    article: str,
    parsed: list,
    *,
    nynorsk: bool = False,
) -> tuple[str, bool | None, str | None, list[dict]]:
    """End-to-end quote verification: literal check + LLM verify + apply edits.

    Returns (possibly-edited article, quotes_ok, quotes_json, details). For
    callers that need to interleave their own logic between steps (e.g. SSE
    status events), use check_quotes_detailed + verify_failing_quotes
    directly instead.
    """
    article, _ = strip_guillemets_in_dash_quotes(article)
    source_texts = [p.markdown or "" for p in parsed]
    source_urls = [p.source or "" for p in parsed]
    details = check_quotes_detailed(article, source_texts, source_urls, nynorsk=nynorsk)
    article, _ = await verify_failing_quotes(article, details, source_texts, nynorsk=nynorsk)
    article = apply_quote_typography(article)
    quotes_ok = all(d["ok"] for d in details) if details else True
    quotes_json = json.dumps(details, ensure_ascii=False)
    return article, quotes_ok, quotes_json, details
