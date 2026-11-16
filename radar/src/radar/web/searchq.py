"""The one query language the feed speaks.

Everything the reader can ask for arrives as a single `q` string: free text plus
`nøkkel:verdi` tokens. This module turns that string into a `Query` — and it is
the only thing allowed to build an FTS5 MATCH expression, because that is where
a stray quote or a bare `NOT` stops being a search and becomes a 500.

Two rules run through all of it:

- **Never fail on input.** A colon appears in headlines («Ystebø: dette er
  galskap»), so an unknown key degrades to free text, and a malformed value
  degrades to free text too. There is no parse error a reader can trigger.
- **Never emit an operator we did not mean.** Every user word goes into the
  MATCH expression inside double quotes, which is what makes `*`, `^`, `-`, `(`
  and the bare words AND/OR/NOT/NEAR ordinary characters again.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from urllib.parse import urlencode

from .. import config

# A paste accident, not a query. Both caps exist because the feed's ORDER BY is a
# computed expression, so SQLite materialises and sorts the whole match set
# before LIMIT applies — a query that matches everything costs the whole table.
MAX_RAW_CHARS = 200
MAX_TOKENS = 12

# Display names. Canonical here — a chip needs its label at parse time, and
# queries.py re-exports these under its own names.
STAGE_LABELS = {
    "barnehage": "Barnehage",
    "grunnskole": "Grunnskole",
    "videregaende": "Videregående",
    "laererutdanning": "Lærerutdanning",
    "tverrgaende": "På tvers",
}
KIND_LABELS_NO = {
    "nyhet": "Nyhet", "debatt": "Debatt",
    "forskning": "Forskning", "fagstoff": "Fagstoff",
}
COVERAGE_LABELS_NO = {
    "nei": "Ikke dekket", "ja": "Dekket", "sitert": "Sitert av oss",
    "dekket": "Vi har saken", "oppfolging": "Ny reaksjon",
}

STAGE_VALUES = {
    "barnehage": "barnehage",
    "grunnskole": "grunnskole",
    "videregaende": "videregaende",
    "videregående": "videregaende",
    "vgs": "videregaende",
    "laererutdanning": "laererutdanning",
    "lærerutdanning": "laererutdanning",
    "tverrgaende": "tverrgaende",
    "tverrgående": "tverrgaende",
    "tvers": "tverrgaende",
}

KIND_VALUES = {k: k for k in ("nyhet", "debatt", "forskning", "fagstoff")}

# «ny» is what the badge says; «oppfolging» is what the column stores. Both work.
COVERAGE_VALUES = {
    "nei": "nei",
    "ja": "ja",
    "sitert": "sitert",
    "dekket": "dekket",
    "oppfolging": "oppfolging",
    "oppfølging": "oppfolging",
    "ny": "oppfolging",
}

SORT_VALUES = {"relevans": "relevans", "dato": "dato", "tid": "dato"}

BOOL_VALUES = {"ja": True, "nei": False, "true": True, "false": False}

# key → canonical key. Norwegian is canonical because the whole interface is;
# the English spellings are accepted silently for the muscle memory half the
# desk brings from GitHub, and are never suggested.
KEY_ALIASES = {
    "trinn": "trinn", "stage": "trinn",
    "type": "type", "kind": "type",
    "kilde": "kilde", "source": "kilde",
    "dekning": "dekning", "dekket": "dekning", "covered": "dekning",
    "score": "score", "poeng": "score", "terskel": "score", "relevans": "score",
    "dager": "dager", "dag": "dager", "siden": "dager", "dato": "dager",
    "mur": "mur", "betalingsmur": "mur", "paywall": "mur",
    "sak": "sak", "klynge": "sak", "cluster": "sak",
    "sorter": "sorter", "sort": "sorter",
}

_TOKEN = re.compile(
    r'(?P<neg>-)?(?:(?P<key>[a-zæøåA-ZÆØÅ]{2,12}):)?(?P<val>"[^"]*"|\S+)'
)

_SCORE = re.compile(r"^(?P<op>>=|<=|>|<)?(?P<n>\d{1,3})(?P<plus>\+)?$")
_SCORE_RANGE = re.compile(r"^(?P<lo>\d{1,3})-(?P<hi>\d{1,3})$")
_ISO_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_CLUSTER_ID = re.compile(r"^[0-9a-f]{6,32}$")


@dataclass(frozen=True)
class Chip:
    """One structured filter, in both spellings it needs.

    `text` is what goes back into the query string; `display` is what the
    reader sees on the chip. They differ on purpose — «dekning:oppfolging» is
    the column value, «Ny reaksjon» is the thing the desk recognises.
    """

    key: str
    value: str
    label: str
    neg: bool = False

    @property
    def text(self) -> str:
        return f"{'-' if self.neg else ''}{self.key}:{self.value}"

    @property
    def display(self) -> str:
        if not self.neg:
            return self.label
        # «Ikke debatt», but «Ikke VG» — a source name keeps its capitals, and
        # the tell is an uppercase letter anywhere but the first position.
        proper = any(c.isupper() for c in self.label[1:])
        return f"Ikke {self.label if proper else self.label[0].lower() + self.label[1:]}"


@dataclass(frozen=True)
class Query:
    """A parsed `q`. Everything the feed needs to build its SQL and its chrome."""

    raw: str = ""
    text: str = ""
    match: str | None = None
    stages: tuple[str, ...] = ()
    kinds: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()
    not_sources: tuple[str, ...] = ()
    coverage: tuple[str, ...] = ()
    not_coverage: tuple[str, ...] = ()
    min_score: int | None = None
    max_score: int | None = None
    since_day: str | None = None
    on_day: str | None = None
    paywalled: bool | None = None
    cluster_id: str | None = None
    sort: str = "auto"
    page: int = 1
    chips: tuple[Chip, ...] = field(default=())
    days_token: str = ""
    # Muted words from the session cookie, not from `q`. They ride on the Query
    # so every predicate builder — feed, counts, facets — excludes them without
    # growing a second argument, but they are not chips: a chip is URL state and
    # these deliberately are not.
    hidden: tuple[str, ...] = ()

    @property
    def filter_tokens(self) -> str:
        """The structured half of the query, without the free text.

        The search box carries only what the reader typed as words, so this is
        what rides along in a hidden field to stop a search from silently
        dropping the filters.
        """
        return " ".join(chip.text for chip in self.chips)

    @property
    def mode(self) -> str:
        """'rank' for a relevance list, 'time' for the chronological feed.

        A chronological feed answers «what happened, in order»; a search answers
        «where is this thing». They rank on different axes, and grouping a
        relevance list by day would tell the reader the order is chronological
        when it is not. Text switches the view; `sorter:` overrides either way.
        """
        if self.sort == "relevans":
            return "rank"
        if self.sort == "dato":
            return "time"
        return "rank" if self.match else "time"

    @property
    def is_empty(self) -> bool:
        return not self.chips and not self.text

    def without(self, chip: Chip) -> str:
        """`raw` with one chip's token removed — the chip's × link."""
        return " ".join(t for t in _split(self.raw) if t.lower() != chip.text.lower())

    def with_token(self, key: str, value: str | None) -> str:
        """`raw` with `key:` set to one value, or dropped when value is None."""
        kept = [t for t in _split(self.raw) if _key_of(t) != key]
        if value is not None:
            kept.append(f"{key}:{value}")
        return " ".join(kept)


def _split(raw: str) -> list[str]:
    return [m.group(0) for m in _TOKEN.finditer(raw or "")]


def _key_of(token: str) -> str | None:
    m = _TOKEN.fullmatch(token)
    if not m or not m.group("key"):
        return None
    return KEY_ALIASES.get(m.group("key").lower())


def parse(
    raw: str | None,
    *,
    sources: frozenset[str] = frozenset(),
    source_aliases: dict[str, str] | None = None,
    page: int = 1,
    hidden: tuple[str, ...] = (),
) -> Query:
    """Turn a `q` string into a Query. Never raises.

    `sources` is the live distinct set from the database, so `kilde:` matches
    what actually exists rather than a hardcoded list; `source_aliases` maps a
    display label back to the stored name («Forskning.no» → «Forskning»).
    """
    raw = (raw or "").strip()[:MAX_RAW_CHARS]
    aliases = {k.casefold(): v for k, v in (source_aliases or {}).items()}
    by_source = {s.casefold(): s for s in sources}

    words: list[tuple[str, bool, bool]] = []  # (word, negated, quoted)
    chips: list[Chip] = []
    acc: dict = {
        "stages": [], "kinds": [], "sources": [], "not_sources": [],
        "coverage": [], "not_coverage": [],
        "min_score": None, "max_score": None, "since_day": None, "on_day": None,
        "paywalled": None, "cluster_id": None, "sort": "auto", "days_token": "",
    }

    for m in list(_TOKEN.finditer(raw))[:MAX_TOKENS]:
        neg = bool(m.group("neg"))
        val_raw = m.group("val")
        quoted = val_raw.startswith('"') and val_raw.endswith('"') and len(val_raw) >= 2
        value = val_raw[1:-1] if quoted else val_raw
        key = m.group("key")
        if not key:
            if value:
                words.append((value, neg, quoted))
            continue

        canon = KEY_ALIASES.get(key.lower())
        if canon is None:
            # A headline with a colon in it, or a typo. Either way it is text.
            words.append((f"{key}:{value}", neg, quoted))
            continue

        chip = _apply(canon, value, neg, acc, by_source, aliases)
        if chip is None:
            # Known key, unusable value — search for it rather than dropping it.
            words.append((value, neg, quoted))
            continue
        chips.append(chip)

    text = " ".join(w for w, n, _ in words if not n)
    return Query(
        raw=raw,
        text=text,
        match=build_match(words),
        stages=tuple(dict.fromkeys(acc["stages"])),
        kinds=tuple(dict.fromkeys(acc["kinds"])),
        sources=tuple(dict.fromkeys(acc["sources"])),
        not_sources=tuple(dict.fromkeys(acc["not_sources"])),
        coverage=tuple(dict.fromkeys(acc["coverage"])),
        not_coverage=tuple(dict.fromkeys(acc["not_coverage"])),
        min_score=acc["min_score"],
        max_score=acc["max_score"],
        since_day=acc["since_day"],
        on_day=acc["on_day"],
        paywalled=acc["paywalled"],
        cluster_id=acc["cluster_id"],
        sort=acc["sort"],
        page=max(1, page),
        chips=tuple(chips),
        days_token=acc["days_token"],
        hidden=tuple(hidden),
    )


def _apply(key, value, neg, acc, by_source, aliases) -> Chip | None:
    """Fold one structured token into the accumulator. None = unusable value."""
    low = value.casefold()

    if key == "trinn":
        stage = STAGE_VALUES.get(low)
        if not stage:
            return None
        acc["stages"].append(stage)
        return Chip("trinn", stage, STAGE_LABELS.get(stage, stage), neg)

    if key == "type":
        kind = KIND_VALUES.get(low)
        if not kind:
            return None
        acc["kinds"].append(kind)
        return Chip("type", kind, KIND_LABELS_NO.get(kind, kind), neg)

    if key == "kilde":
        stored = by_source.get(low) or by_source.get(aliases.get(low, "").casefold())
        if not stored:
            return None
        acc["not_sources" if neg else "sources"].append(stored)
        return Chip("kilde", stored, stored, neg)

    if key == "dekning":
        status = COVERAGE_VALUES.get(low)
        if not status:
            return None
        acc["not_coverage" if neg else "coverage"].append(status)
        return Chip("dekning", status, COVERAGE_LABELS_NO.get(status, status), neg)

    if key == "score":
        return _apply_score(low, acc)

    if key == "dager":
        return _apply_days(low, acc)

    if key == "mur":
        flag = BOOL_VALUES.get(low)
        if flag is None:
            return None
        acc["paywalled"] = (not flag) if neg else flag
        wanted = (not flag) if neg else flag
        return Chip("mur", "ja" if flag else "nei",
                    "Bak betalingsmur" if wanted else "Uten betalingsmur")

    if key == "sak":
        if not _CLUSTER_ID.match(low):
            return None
        acc["cluster_id"] = low
        return Chip("sak", low, "Ett forløp", neg)

    if key == "sorter":
        sort = SORT_VALUES.get(low)
        if not sort:
            return None
        acc["sort"] = sort
        return Chip("sorter", sort,
                    "Sortert på dato" if sort == "dato" else "Sortert på relevans")

    return None


def _apply_score(value: str, acc) -> Chip | None:
    rng = _SCORE_RANGE.match(value)
    if rng:
        lo, hi = int(rng.group("lo")), int(rng.group("hi"))
        acc["min_score"], acc["max_score"] = min(lo, hi), max(lo, hi)
        return Chip("score", f"{lo}-{hi}", f"Relevans {min(lo, hi)}–{max(lo, hi)}")
    m = _SCORE.match(value)
    if not m:
        return None
    n = min(100, int(m.group("n")))
    op = m.group("op")
    if op in ("<", "<="):
        acc["max_score"] = n - 1 if op == "<" else n
        return Chip("score", value, f"Relevans under {n}")
    # Bare number, `60+` and `>=60` all mean "this good or better"; `>60` is the
    # one that excludes the boundary, and getting that wrong silently drops the
    # articles sitting exactly on a threshold the desk chose.
    acc["min_score"] = n + 1 if op == ">" else n
    return Chip("score", value, f"Relevans {acc['min_score']} og oppover")


def _apply_days(value: str, acc) -> Chip | None:
    if _ISO_DAY.match(value):
        acc["on_day"] = value
        acc["days_token"] = value
        return Chip("dager", value, value)
    today = datetime.now(config.OSLO).date()
    if value in ("idag", "i-dag", "0"):
        acc["on_day"] = today.isoformat()
        acc["days_token"] = "1"
        return Chip("dager", "idag", "I dag")
    if value in ("igar", "igår", "i-gar", "i-går"):
        acc["on_day"] = (today - timedelta(days=1)).isoformat()
        acc["days_token"] = "igår"
        return Chip("dager", "igår", "I går")
    if not value.isdigit():
        return None
    n = min(int(value), 365)
    acc["since_day"] = (today - timedelta(days=n - 1 if n else 0)).isoformat()
    acc["days_token"] = str(n)
    label = "I dag" if n <= 1 else ("Siste uke" if n == 7 else f"Siste {n} dager")
    return Chip("dager", str(n), label)


# A bare term is starred from four characters; the last term from three, because
# it is the one still being typed and the one carrying the compound. Never below
# three: `"på"*` matches a third of the corpus and defeats the prefix index,
# which is also the one query that can make LIMIT meaningless — the temp b-tree
# sorts the whole match set before the limit applies.
_STAR_MIN = 4
_STAR_MIN_LAST = 3


def build_match(words: list[tuple[str, bool, bool]]) -> str | None:
    """The FTS5 MATCH expression for these words, or None when there is no text."""
    positive = [(w, q) for w, n, q in words if not n]
    negative = [(w, q) for w, n, q in words if n]
    if not positive:
        # Nothing to negate against. A leading NOT is a syntax error, and a query
        # of pure exclusions is better served by the structured filters alone.
        return None

    parts = []
    for i, (word, quoted) in enumerate(positive):
        last = i == len(positive) - 1
        floor = _STAR_MIN_LAST if last else _STAR_MIN
        term = fts_term(word, quoted=quoted, star=len(word) >= floor)
        if term:
            parts.append(term)
    if not parts:
        # Every positive word was punctuation the tokenizer would discard. An
        # empty phrase is not a search — fall through to the structured filters.
        return None
    for word, quoted in negative:
        term = fts_term(word, quoted=quoted, star=False)
        if term:
            parts.append(f"NOT {term}")
    return " ".join(parts)


def fts_term(word: str, *, quoted: bool, star: bool) -> str:
    """One user word as an FTS5 phrase, or "" when there is nothing to search for.

    The double quotes are what make this safe: inside a quoted phrase FTS5 treats
    `*`, `^`, `:`, `(`, `)`, `-` and the bare words AND/OR/NOT/NEAR as ordinary
    characters. Internal quotes are escaped by doubling, FTS5's own convention.

    A term with no word characters is dropped rather than emitted as an empty
    phrase: a lone `*` is the one query that returns the entire table, and the
    feed's computed ORDER BY sorts the whole match set before LIMIT applies.
    """
    body = word.replace('"', '""').strip()
    if not re.search(r"\w", body):
        return ""
    return f'"{body}"*' if star and not quoted else f'"{body}"'


def mute_match(term: str) -> str:
    """The index half of what a muted word hides: title and ingress.

    Not the body. A story that mentions PISA in paragraph nine is about
    something else, and hiding it too would turn a mute into a hole in the
    feed. What the story is *about* is judged in queries._mute_clause, which
    adds the classifier's own account of it. Prefix-starred so «PISA» also
    catches «PISA-resultatene» in the compounds Norwegian headlines run to.
    """
    return f"{{title summary}} : {fts_term(term, quoted=False, star=True)}"


def from_controls(
    *,
    sok: str = "",
    filtre: str = "",
    trinn: list[str] | None = None,
    type_: list[str] | None = None,
    kilde: list[str] | None = None,
    dekning: list[str] | None = None,
    score: str | None = None,
    dager: str | None = None,
    mur: str | None = None,
    submitted: bool = False,
) -> str:
    """Build the canonical `q` from what the filter panel and search box sent.

    The panel is a plain GET form of checkboxes, so this is the only place its
    spelling exists; everything downstream sees the one query grammar. There is
    no second parser and no second set of predicates.

    `filtre` is the hidden field the *search* form carries — searching must not
    silently drop the filters, and the search form has no checkboxes to resend.
    `submitted` says the panel itself was the sender, in which case an unticked
    box means «off», not «unchanged».
    """
    parts: list[str] = []
    if submitted:
        for key, values in (
            ("trinn", trinn), ("type", type_), ("kilde", kilde), ("dekning", dekning)
        ):
            for value in values or []:
                if value:
                    parts.append(f"{key}:{_quote_if_spaced(value)}")
        # The panel's threshold radio always sends something, so the default is
        # written out only when it differs from what an unfiltered feed means.
        if score is not None and score != "" and int(score) != config.FEED_MIN_SCORE:
            parts.append(f"score:{int(score)}")
        if dager:
            parts.append(f"dager:{dager}")
        if mur:
            parts.append(f"mur:{mur}")
    elif filtre:
        parts.extend(_split(filtre))

    # Free text last, so the chips read left-to-right in the order they were set.
    return " ".join([*parts, sok.strip()]).strip()


def from_legacy(
    *,
    stage: str | None = None,
    kind: str | None = None,
    source: str | None = None,
    covered: str | None = None,
    min_score: int | None = None,
) -> str:
    """Build a `q` string out of the pre-search query params.

    Bookmarks and Slack links carrying `?stage=…&covered=nei` have to keep
    working, so the route redirects them through this into the new form rather
    than serving two grammars forever.
    """
    parts = []
    if stage:
        parts.append(f"trinn:{stage}")
    if kind:
        parts.append(f"type:{kind}")
    if source:
        parts.append(f"kilde:{_quote_if_spaced(source)}")
    if covered in ("ja", "nei"):
        parts.append(f"dekning:{covered}")
    if min_score is not None and min_score != config.FEED_MIN_SCORE:
        parts.append(f"score:{min_score}")
    return " ".join(parts)


def _quote_if_spaced(value: str) -> str:
    return f'"{value}"' if " " in value else value


def qs(q: str, *, page: int | None = None) -> str:
    """A feed URL for this query string."""
    params: dict = {}
    if q:
        params["q"] = q
    if page and page > 1:
        params["page"] = page
    return f"/?{urlencode(params)}" if params else "/"
