"""Plain text out of whatever the scraper happened to store.

The scraper keeps feed payloads raw — it only renders HTML when it publishes a
snapshot, and we read the live database. So a body that came from a feed's
`content:encoded` (Folkeskolen, among others) arrives as HTML, while a fetched
body is markdown. Both end up in front of a reader and in front of the model.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from markdown_it import MarkdownIt
from markdown_it.token import Token

from . import dagbladet

# Tags whose boundaries are worth a line break when flattening to text.
_BLOCK_TAGS = {
    "p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
    "blockquote", "section", "article", "figure", "figcaption",
}
_DROP_TAGS = {"script", "style"}

_LOOKS_LIKE_HTML = re.compile(r"<(?:p|div|br|span|a|h[1-6]|ul|ol|li|img|figure)\b|&[a-z]+;", re.I)
_BLANK_RUNS = re.compile(r"\n{3,}")
_SPACES = re.compile(r"[ \t]{2,}")


class _Flattener(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in _DROP_TAGS:
            self._skip += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _DROP_TAGS and self._skip:
            self._skip -= 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def html_to_text(value: str | None) -> str | None:
    """Flatten HTML to readable text. Markdown and plain text pass through."""
    if not value or not _LOOKS_LIKE_HTML.search(value):
        return value
    parser = _Flattener()
    parser.feed(value)
    parser.close()
    text = "".join(parser.parts)
    text = _SPACES.sub(" ", text)
    text = _BLANK_RUNS.sub("\n\n", text)
    return "\n".join(line.strip() for line in text.split("\n")).strip()


# html=False makes the renderer escape any markup that survives the flattening,
# so the output is safe to mark |safe in a template.
_MD = MarkdownIt("commonmark", {"html": False})

# A short line ahead of the H1 is the source's own kicker ("arbeidstid",
# "[Innherred](/innherred)") — page furniture, not story.
_MAX_KICKER_CHARS = 70

# Sources write "**Aksjen faller. **" often enough to matter. CommonMark won't
# close emphasis on a space, so the asterisks would show up as text.
_LOOSE_STRONG = re.compile(r"\*\*[ \t]*([^*\n]+?)[ \t]*\*\*")


def _walk(tokens: list[Token]) -> Iterator[Token]:
    for token in tokens:
        yield token
        if token.children:
            yield from _walk(token.children)


def _blocks(tokens: list[Token]) -> list[list[Token]]:
    """Split a token stream into top-level blocks. Nesting nets to zero at each."""
    blocks: list[list[Token]] = []
    current: list[Token] = []
    depth = 0
    for token in tokens:
        current.append(token)
        depth += token.nesting
        if depth == 0:
            blocks.append(current)
            current = []
    if current:
        blocks.append(current)
    return blocks


def _block_text(block: list[Token]) -> str:
    return " ".join(t.content for t in block if t.type == "inline")


def _drop_empty_list_items(blocks: list[list[Token]]) -> list[list[Token]]:
    """Remove bullets with nothing in them.

    Several sources emit a bare "-" as the marker of a teaser whose content was
    never inlined. The bullet renders as an empty dot; whatever followed is its
    own block and survives on its own.
    """
    out = []
    for block in blocks:
        if not block or block[0].type != "bullet_list_open":
            out.append(block)
            continue
        kept, item, depth = [], [], 0
        for token in block[1:-1]:
            item.append(token)
            depth += token.nesting
            if depth == 0:
                if len(item) > 2:  # more than the list_item open/close pair
                    kept.extend(item)
                item = []
        if kept:
            out.append([block[0], *kept, block[-1]])
    return out


# Publishers append navigation rails to the body: video teasers, "Mest lest",
# job ads, subscription pitches. Two shapes, deliberately handled separately —
# an earlier version cut any heading-dominated tail and ate real articles whose
# closing sections happened to be short (an NRK live review, a forskning.no
# piece with terse subheads). Prose is never sacrificed to catch a rail.
_RAIL_MIN_HEADINGS = 3

# A rail's items are headings and one-liners — a deadline, a teaser caption.
# Anything longer is prose, and prose means the article is still going.
_RAIL_MAX_PARA_CHARS = 110

_RAIL_HEADINGS = (
    "les også", "les mer", "les flere", "mest lest", "siste nytt",
    "ledige stillinger", "mer debatt", "har du fått med deg",
    "flere saker", "relaterte saker", "andre saker", "annonse",
    "toppselgere", "nettbutikker vi anbefaler", "digitalt abonnement",
)


def _strip_bare_heading_run(blocks: list[list[Token]]) -> list[list[Token]]:
    """Cut a tail that is nothing but headings — a teaser list stripped of links.

    Requires no paragraphs at all in the run: an article can end with short
    sections, but never with three consecutive headings and no prose.
    """
    cut = len(blocks)
    headings = 0
    for i in range(len(blocks) - 1, -1, -1):
        if blocks[i][0].type != "heading_open":
            break
        headings += 1
        cut = i
    return blocks[:cut] if headings >= _RAIL_MIN_HEADINGS else blocks


def _is_rail_tail(blocks: list[list[Token]]) -> bool:
    """True when what follows is a list of titles, not writing.

    Every block has to be a heading or a one-liner, and headings have to carry
    the tail: a rail is titles with captions under them, while prose runs more
    paragraphs than headings even when the paragraphs are short quotes.
    """
    headings = paragraphs = 0
    for block in blocks:
        if block[0].type == "heading_open":
            headings += 1
        elif block[0].type == "paragraph_open" and len(_block_text(block)) <= _RAIL_MAX_PARA_CHARS:
            paragraphs += 1
        else:
            return False
    return headings >= max(1, paragraphs)


def _strip_labelled_rail(blocks: list[list[Token]]) -> list[list[Token]]:
    """Cut from a rail's own heading — "Mest lest", "Ledige stillinger" — to the end.

    The label alone is not enough: forskning.no drops a "Mest lest" widget into
    the middle of a story and then carries on reporting. The cut only happens
    when no real paragraph survives it.
    """
    for i, block in enumerate(blocks):
        if (
            block[0].type == "heading_open"
            and _block_text(block).strip().lower().startswith(_RAIL_HEADINGS)
            and _is_rail_tail(blocks[i + 1 :])
        ):
            return blocks[:i]
    return blocks


# Between the headline and the first real paragraph sits the page's furniture:
# the standfirst printed again, a photo caption and its credit, the byline's
# address, the label of a summary widget whose contents never made it into the
# markup. Frifagbevegelse serves all five before a word of the story.
#
# The scan is bounded twice — it stops at the first substantial paragraph, and
# never runs past a handful of blocks — and every drop needs an explicit match.
# An article that simply opens with a short paragraph keeps it.
_LEDE_RUN_BLOCKS = 8
_LEDE_PROSE_CHARS = 200

_EMAIL_ONLY = re.compile(r"^[^\s@]+@[^\s@]+\.[a-z]{2,}$", re.I)
_CREDIT_PREFIX = re.compile(
    r"^(foto|illustrasjonsfoto|arkivfoto|faksimile|skjermdump|illustrasjon|grafikk)\s*[:.]",
    re.I,
)
_CREDIT_MAX_CHARS = 80
_CAPTION_MAX_CHARS = 200

# A credit is an attribution, not a sentence: capitalised names and agencies
# joined by slashes — "COLOURBOX/Lev Dolgachov", "Diane Bondareff / AP / NTB".
# No digits and no clause structure, which is what separates it from prose that
# merely contains a slash. The loose version of this rule — short line, has a
# slash — claimed "Farten ble målt til 165 km/t i 110-sonen." and a trotting
# schedule, and every false credit took the line above it as its caption too.
_CREDIT_ATTRIBUTION = re.compile(
    r"^[A-ZÆØÅ][^\W\d_]*(?: [^\W\d_]+){0,3}"
    r"(?:\s*/\s*[A-ZÆØÅ][^\W\d_]*(?: [^\W\d_]+){0,3})+$"
)

_SUMMARY_WIDGET_HEADINGS = (
    "saken oppsummert", "kort fortalt", "dette er saken", "saken kort",
    "oppsummering", "hovedpunkter", "dette har skjedd",
)


def _norm(text: str) -> str:
    return " ".join(text.split()).casefold()


def _same_text(a: str, b: str) -> bool:
    """Whether two lines are the same sentence, give or take punctuation."""
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return False
    return a == b or (min(len(a), len(b)) >= 60 and (a.startswith(b) or b.startswith(a)))


def _is_photo_credit(text: str) -> bool:
    """"Foto: Ola Nordmann", "COLOURBOX/Lev Dolgachov" — an attribution, not a sentence."""
    return bool(
        _CREDIT_PREFIX.match(text)
        or (len(text) <= _CREDIT_MAX_CHARS and _CREDIT_ATTRIBUTION.match(text))
    )


def _strip_lede_run(blocks: list[list[Token]], summary: str | None) -> list[list[Token]]:
    """Drop the furniture standing between the headline and the story."""
    # Nothing above the body's own copy of the standfirst is story, so the guard
    # below must not stop the scan short of it. Two shapes used to defeat it and
    # print the standfirst twice: the standfirst itself running past the guard's
    # length, and Dagens Medisin's opening photo caption doing the same a block
    # above it.
    last_standfirst = -1
    if summary:
        for i, block in enumerate(blocks[:_LEDE_RUN_BLOCKS]):
            if _same_text(_block_text(block), summary):
                last_standfirst = i

    limit = 0
    for i, block in enumerate(blocks[:_LEDE_RUN_BLOCKS]):
        past_standfirst = i > last_standfirst
        if (
            past_standfirst
            and block[0].type == "paragraph_open"
            and len(_block_text(block)) > _LEDE_PROSE_CHARS
        ):
            break
        limit = i + 1

    kept: list[list[Token]] = []
    for block in blocks[:limit]:
        text = _block_text(block).strip()
        heading = block[0].type == "heading_open"
        if summary and _same_text(text, summary):
            continue
        if heading and _norm(text).startswith(_SUMMARY_WIDGET_HEADINGS):
            continue
        if not heading and _EMAIL_ONLY.match(text):
            continue
        if not heading and _is_photo_credit(text):
            # A credit line stands directly under the caption it belongs to, so
            # finding one is also how the caption above it gets identified.
            if (
                kept
                and kept[-1][0].type == "paragraph_open"
                and len(_block_text(kept[-1])) <= _CAPTION_MAX_CHARS
            ):
                kept.pop()
            continue
        kept.append(block)
    return kept + blocks[limit:]


def _strip_lede_furniture(tokens: list[Token], title: str | None = None) -> list[Token]:
    """Drop the source's kicker line and its own headline — the page prints the title.

    Usually that headline is an H1. NRK files its kortnytt with an H2 instead,
    so a heading of any level goes when it says what the page already says.
    """
    heading = len(tokens) >= 3 and tokens[0].type == "heading_open"
    repeats_title = bool(title) and heading and _same_text(tokens[1].content, title)
    if heading and (tokens[0].tag == "h1" or repeats_title):
        return tokens[3:]
    kicker = (
        len(tokens) >= 6
        and tokens[0].type == "paragraph_open"
        and len(tokens[1].content) <= _MAX_KICKER_CHARS
        and tokens[3].type == "heading_open"
        and tokens[3].tag == "h1"
    )
    return tokens[6:] if kicker else tokens


# A standfirst is a teaser. When it runs to half the body or more it is not a
# teaser, it is the article — NRK files its kortnytt that way, the whole brief
# in the feed's description and the same text again in the body. Printed above
# the body it makes the reader read the story twice, the flattened version
# first. Real standfirsts are nowhere near: across the sources we read the
# median is a twentieth of the body, and nothing but kortnytt clears a third.
_STANDFIRST_MAX_SHARE = 0.5


def standfirst(summary: str | None, body: str | None) -> str | None:
    """The summary to print above the body, or None when it *is* the body."""
    text = html_to_text(summary)
    if not text or not text.strip():
        return None
    full = html_to_text(body) or ""
    lede_chars, body_chars = len(_norm(text)), len(_norm(full))
    if body_chars and lede_chars / body_chars >= _STANDFIRST_MAX_SHARE:
        return None
    return text


def article_html(
    value: str | None,
    *,
    base_url: str | None = None,
    summary: str | None = None,
    source: str | None = None,
    title: str | None = None,
) -> str:
    """Story body → safe HTML for the detail page.

    HTML bodies are flattened to text first; the rest is markdown from the
    scraper's own fetch. Relative links are resolved against the article URL,
    which is the only place they mean anything. `summary` is the standfirst the
    page prints above the body, so the body's own copy of it can go — pass what
    the page will actually print, which is `standfirst()`, not the raw summary.
    `title` is the headline the page prints, and `source` only decides whether a
    publisher's own quirks are worth correcting.
    """
    text = html_to_text(value)
    if not text or not text.strip():
        return ""

    tokens = _strip_lede_furniture(_MD.parse(_LOOSE_STRONG.sub(r"**\1**", text)), title)
    blocks = _drop_empty_list_items(_blocks(tokens))
    if dagbladet.applies_to(source):
        blocks = dagbladet.unbullet_quotes(blocks)
    blocks = _strip_lede_run(blocks, summary)
    blocks = _strip_bare_heading_run(_strip_labelled_rail(blocks))
    tokens = [t for block in blocks for t in block]
    for token in _walk(tokens):
        # both ends, or the close tag stays </h1> and the markup is malformed
        if token.type in ("heading_open", "heading_close") and token.tag == "h1":
            token.tag = "h2"  # the page's own headline is the only h1
        elif token.type == "link_open":
            href = token.attrGet("href") or ""
            if base_url and href and not urlsplit(href).scheme and not href.startswith("#"):
                token.attrSet("href", urljoin(base_url, href))
            token.attrSet("target", "_blank")
            token.attrSet("rel", "noopener nofollow")
        elif token.type == "image":
            token.attrSet("loading", "lazy")
            src = token.attrGet("src") or ""
            if base_url and src and not urlsplit(src).scheme:
                token.attrSet("src", urljoin(base_url, src))

    return _MD.renderer.render(tokens, _MD.options, {})
