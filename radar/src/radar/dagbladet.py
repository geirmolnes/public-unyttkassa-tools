"""What is particular to Dagbladet: its live blogs, and its quote dashes.

Two things, both scoped to the one source because the same shape means
something else elsewhere.

**Studio posts are not articles.** Dagbladet runs its live coverage as
«studio» pages — nyhetsstudio, sportsstudio, one per big event — and the
front page links to every post in them under `/studio/<blog>/<post>`. What
the scraper gets for such a link is the studio *page*: the blog's title, a
«Siste artikler» rail of teasers for unrelated stories, nothing of the post.
That shell then passes the word filter on a teaser about the kunnskapsminister
and is scored as a story about her. 453 of them had come through when this
was written; the thirteen the model kept were a fire, a football board and a
Russian ship. They are dropped before they are stored.

**Quote dashes.** Dagbladet opens a quote with an ASCII hyphen at the start of the line —
"- Jeg ser stor forskjell, sier han." — where every other source we read uses
an en dash. CommonMark sees a bullet marker, so an interview lands on the story
page as a dotted list. Dagbladet's own page renders those lines as paragraphs,
and sets the interviewer's questions in italics through a CSS class that does
not survive the scraper's HTML-to-markdown step. Both are put back here.

Scoped to Dagbladet on purpose: the same shape from other sources really is a
list — BT's contact rail, Kommunal Rapport's subscription pitch. Dagbladet
itself prints real bullets too, for a table of party donations, which is why
the conversion still has to read what the items say.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from markdown_it.token import Token

SOURCE = "Dagbladet"

# Where the live blogs live. The SQL spelling is for prune, which clears out
# whatever arrived before the ingest check existed.
STUDIO_PATH = "/studio"
STUDIO_LIKE = "https://www.dagbladet.no/studio/%"

# What the page should show, and what every other source already writes.
_QUOTE_DASH = "–"

# A quote is a sentence: several words, and closed like a sentence. The colon
# carries "sier Aune - og fortsetter:", a quote continued in the next paragraph.
# Party donations ("Høyre: 21 355 273 kroner") and a run of candidate names
# ("Ole Gunnar Solskjær (klubbløs)") end open, so those items stay bullets.
_MIN_QUOTE_CHARS = 15
_MIN_QUOTE_WORDS = 3
_SENTENCE_END = re.compile(r"[.!?…»:]$")


def applies_to(source: str | None) -> bool:
    return (source or "").strip().casefold() == SOURCE.casefold()


def is_studio_post(source: str | None, link: str | None) -> bool:
    """True for a link into one of Dagbladet's live blogs.

    Path-based and source-scoped: `/studio/` is Dagbladet's own product name,
    and another outlet's page under the same path is someone else's article.
    """
    if not applies_to(source) or not link:
        return False
    path = urlsplit(link).path
    return path == STUDIO_PATH or path.startswith(STUDIO_PATH + "/")


def _items(block: list[Token]) -> list[list[Token]]:
    """The list's items, each with its own li tokens still around it."""
    items: list[list[Token]] = []
    current: list[Token] = []
    depth = 0
    for token in block[1:-1]:
        if token.type == "list_item_open" and depth == 0:
            current = []
        current.append(token)
        depth += token.nesting
        if depth == 0:
            items.append(current)
    return items


def _plain(inline: Token) -> str:
    """What the item reads as, markup removed.

    The raw markdown is the wrong thing to look at: a quote that closes on a
    link — "fortalte Solberg til [VG.](https://…)" — ends in a bracket there
    and in a full stop on the page.
    """
    if not inline.children:
        return inline.content
    parts = [
        " " if c.type in ("softbreak", "hardbreak") else c.content
        for c in inline.children
        if c.type in ("text", "text_special", "code_inline", "softbreak", "hardbreak")
    ]
    return "".join(parts)


def _quote_inline(item: list[Token]) -> Token | None:
    """The item's inline token, when the item is one paragraph of quote."""
    inner = item[1:-1]
    if len(inner) != 3 or inner[0].type != "paragraph_open" or inner[1].type != "inline":
        return None
    text = _plain(inner[1]).strip()
    if len(text) < _MIN_QUOTE_CHARS or len(text.split()) < _MIN_QUOTE_WORDS:
        return None
    return inner[1] if _SENTENCE_END.search(text) else None


def _paragraph(inline: Token) -> list[Token]:
    """A quote paragraph: the dash restored, and a question set in italics.

    The dash sits inside the emphasis because Dagbladet italicises the whole
    line, dash and all.
    """
    children = [Token("text", "", 0, content=f"{_QUOTE_DASH} "), *(inline.children or [])]
    if _plain(inline).strip().endswith("?"):
        children = [Token("em_open", "em", 1), *children, Token("em_close", "em", -1)]
    inline.children = children
    inline.content = f"{_QUOTE_DASH} {inline.content}"
    return [
        Token("paragraph_open", "p", 1, block=True),
        inline,
        Token("paragraph_close", "p", -1, block=True),
    ]


def _split(block: list[Token]) -> list[list[Token]]:
    """One list → quote paragraphs, and the items that really were bullets.

    The decision is per item because CommonMark merges everything separated by
    blank lines into a single list: Dagbladet's table of party donations and
    the quote printed under it arrive as one ul. Items that stay bullets are
    regrouped into lists of their own, in the order they were written.
    """
    out: list[list[Token]] = []
    kept: list[Token] = []

    def flush() -> None:
        if kept:
            out.append([block[0], *kept, block[-1]])
            kept.clear()

    for item in _items(block):
        inline = _quote_inline(item)
        if inline is None:
            kept.extend(item)
            continue
        flush()
        out.append(_paragraph(inline))
    flush()
    return out


def unbullet_quotes(blocks: list[list[Token]]) -> list[list[Token]]:
    """Turn quote-dash lists back into paragraphs."""
    out: list[list[Token]] = []
    for block in blocks:
        if block and block[0].type == "bullet_list_open":
            out.extend(_split(block))
        else:
            out.append(block)
    return out
