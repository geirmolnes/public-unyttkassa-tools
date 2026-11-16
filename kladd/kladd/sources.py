"""Parse and format source material for the article prompt."""

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from unytt_parser import ParsedSource, parse_source

from kladd.storage import get_cached_source

log = logging.getLogger(__name__)

# In-memory cache for eagerly parsed URLs. Bounded and TTL'd: the server is
# long-running, and news articles get updated — entries only need to survive
# the paste-URL → draft window.
_CACHE_MAX = 128
_CACHE_TTL_S = 3600
_cache: dict[str, tuple[float, ParsedSource]] = {}
_cache_lock = threading.Lock()


@dataclass
class LoadedSources:
    """Result of parsing a batch of source inputs."""

    formatted: str
    count: int
    all_urls: bool
    parsed: list[ParsedSource]
    dropped: list[tuple[str, str]] = field(default_factory=list)  # (source, error)


def _cache_get(url: str) -> ParsedSource | None:
    with _cache_lock:
        entry = _cache.get(url)
        if not entry:
            return None
        ts, src = entry
        if time.monotonic() - ts > _CACHE_TTL_S:
            del _cache[url]
            return None
        return src


def _cache_put(url: str, src: ParsedSource) -> None:
    with _cache_lock:
        if url not in _cache and len(_cache) >= _CACHE_MAX:
            oldest = min(_cache, key=lambda k: _cache[k][0])
            del _cache[oldest]
        _cache[url] = (time.monotonic(), src)


def _cache_pop(url: str) -> ParsedSource | None:
    with _cache_lock:
        entry = _cache.pop(url, None)
    if not entry:
        return None
    ts, src = entry
    if time.monotonic() - ts > _CACHE_TTL_S:
        return None
    return src


def _format_source(idx: int, source: ParsedSource) -> str:
    """Format a single parsed source as a labeled block."""
    parts = [f"### Kilde {idx}"]

    meta = []
    if source.headline:
        meta.append(f"Tittel: {source.headline}")
    if source.publication:
        meta.append(f"Publikasjon: {source.publication}")
    if source.publication_date:
        meta.append(f"Dato: {source.publication_date}")
    if source.byline:
        meta.append(f"Forfatter: {source.byline}")
    meta.append(f"Type: {source.source_type.value}")
    if source.source_type.value == "URL":
        meta.append(f"URL: {source.source}")

    parts.append(" | ".join(meta))
    parts.append("")
    parts.append(source.markdown or "(tomt innhold)")

    return "\n".join(parts)


def describe_error(error: str) -> str:
    """Short Norwegian reason for a source that failed to parse, shown to the user."""
    # unytt_parser: every UA got a 403, e.g. oecd.org's Cloudflare challenge. Retrying won't help.
    if "HTTP 403" in error:
        return "siden blokkerer automatisk henting – last opp som PDF"
    if error.startswith("No article content extracted"):
        return "fant ingen artikkeltekst"
    return "kunne ikke leses"


def _resolve_url(url: str) -> ParsedSource:
    """Resolve a URL: check in-memory cache, then DB, then parse fresh."""
    hit = _cache_get(url)
    if hit:
        return hit
    cached = get_cached_source(url)
    if cached:
        _cache_put(url, cached)
        return cached
    result = parse_source(url, source_id=f"eager-{url}")
    if not result.error:
        _cache_put(url, result)
    return result


def parse_urls_eager(urls: list[str]) -> dict[str, str]:
    """Parse URLs eagerly, returning status per URL."""
    results: dict[str, str] = {}
    # Filter to only new URLs not already cached
    to_parse = [u for u in urls if not _cache_get(u)]
    if not to_parse:
        return {u: "ok" for u in urls}

    with ThreadPoolExecutor() as pool:
        futures = {pool.submit(_resolve_url, u): u for u in to_parse}
        for f in futures:
            url = futures[f]
            try:
                src = f.result()
                if src.error:
                    # The user sees a short Norwegian reason; keep the parser's own text in the log.
                    log.warning("parse failed: %s", src.error)
                results[url] = f"error: {describe_error(src.error)}" if src.error else "ok"
            except Exception as e:
                log.warning("parse failed: %s: %r", url, e)
                results[url] = f"error: {describe_error(str(e))}"

    # Already-cached URLs
    for u in urls:
        if u not in results:
            results[u] = "ok"
    return results


def load_and_format(inputs: list[str]) -> LoadedSources:
    """Parse source inputs; failed sources are reported in .dropped, not raised."""

    def _parse_input(i: int, inp: str) -> ParsedSource:
        cached = _cache_pop(inp)
        if cached:
            return cached
        return parse_source(inp, source_id=f"source-{i}")

    with ThreadPoolExecutor() as pool:
        futures = {
            pool.submit(_parse_input, i, inp): i
            for i, inp in enumerate(inputs, 1)
        }
        results = {futures[f]: f.result() for f in futures}
    sources_ordered = [results[i] for i in sorted(results)]

    parsed: list[ParsedSource] = []
    dropped: list[tuple[str, str]] = []
    all_urls = True
    for src in sources_ordered:
        if src.error:
            dropped.append((src.source, src.error))
            continue
        if src.source_type.value != "URL":
            all_urls = False
        parsed.append(src)

    if not parsed:
        raise ValueError("Ingen gyldige kilder funnet.")

    # Number the surviving sources 1..n so labels match the stated count.
    formatted = "\n\n---\n\n".join(
        _format_source(i, src) for i, src in enumerate(parsed, 1)
    )
    return LoadedSources(formatted, len(parsed), all_urls, parsed, dropped)
