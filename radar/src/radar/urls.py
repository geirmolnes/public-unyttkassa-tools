"""Reducing a URL to a key two different databases can be joined on.

The scraper stores the link an outlet published under; our own archive stores
the link a journalist pasted into a sitatsak. They point at the same article and
almost never agree character for character — one has a campaign parameter, the
other went through Outlook's link rewriter, one ends in a slash.

The key is deliberately conservative in one direction: it would rather miss a
match than invent one. A link that proves nothing comes back as None.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit

# Parameters that identify the reader or the campaign, not the article. Removing
# these is safe; removing *every* parameter is not — see `normalize`.
TRACKING_PARAMS = frozenset(
    {
        "fbclid", "gclid", "gbraid", "wbraid", "msclkid", "igshid", "twclid",
        "mc_cid", "mc_eid", "_hsenc", "_hsmi", "xtor", "spm",
        "ref", "ref_src", "referrer", "source",
        "at_medium", "at_campaign", "at_custom1", "at_custom2", "at_custom3",
        "at_custom4",
    }
)
TRACKING_PREFIXES = ("utm_",)

# Outlook rewrites every link in a mail it scans. Journalists paste the rewritten
# form straight into the article, so the real URL arrives wrapped.
SAFELINKS_HOST = "safelinks.protection.outlook.com"

_MAX_UNWRAP = 2


def _is_tracking(key: str) -> bool:
    return key in TRACKING_PARAMS or key.startswith(TRACKING_PREFIXES)


def normalize(url: str | None, _depth: int = 0) -> str | None:
    """A join key for `url`, or None when it can't identify a single article.

    Query parameters are filtered, never dropped wholesale. Dropping them all
    looks tidier and is wrong: BT's live blog is one URL per *day* distinguished
    only by `?pinnedEntry=`, so stripping the query collapses a month of
    unrelated events onto one key — which is how a link to yesterday's live blog
    ends up "proving" we covered an unrelated story.
    """
    if not url:
        return None

    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https"):
        return None

    host = parts.hostname or ""
    if host.endswith(SAFELINKS_HOST):
        if _depth >= _MAX_UNWRAP:
            return None
        inner = dict(parse_qsl(parts.query)).get("url")
        return normalize(inner, _depth + 1)

    host = host.removeprefix("www.").rstrip(".")
    path = parts.path.rstrip("/")
    # A bare host is a front page or a section. Every outlet gets linked that way
    # sooner or later, and it says nothing about any particular article.
    if not host or not path:
        return None

    kept = sorted(
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not _is_tracking(k)
    )
    key = f"{host}{path}"
    if kept:
        key += f"?{urlencode(kept)}"
    # Both sides go through this, so lowercasing costs nothing and rescues the
    # outlets that mix case in their article ids.
    return key.lower()


# NRK files its regional kortnytt as anchors on one shared page: every item in
# «nyhetssenter Nordland» is `.../nyhetssenter-nordland-1.15898380#1.17992892…`
# and differs from its neighbours only after the #. A server never receives the
# fragment, so a body fetched from that URL is the *page* — whichever item
# happened to lead it — filed under a headline it has nothing to do with.
#
# Four characters, because an id is what makes the page unservable and `#page-2`
# is an ordinal. `#hovedinnhold` and `#comments` carry no id at all: those are
# ordinary anchors on the article's own page and must keep working.
_FRAGMENT_ID = re.compile(r"\d[\d.]{3,}")


# Schibsted's live blogs (BT, Aftenposten) link each post as the blog's URL
# plus `?pinnedEntry=<id>`. The page that serves is the blog, so the body we get
# is every post on it — and the classifier then scores a school story that
# happens to sit further down the same page.
_LIVE_ENTRY_PARAM = "pinnedEntry"


def is_page_anchor(url: str | None) -> bool:
    """True when the link addresses an item *on* a page rather than the page.

    The test is the one thing that makes the body unfetchable: the fragment
    names an id the path does not, or the query pins one entry of a live blog.
    """
    if not url:
        return False
    parts = urlsplit(url)
    if any(k == _LIVE_ENTRY_PARAM for k, _ in parse_qsl(parts.query, keep_blank_values=True)):
        return True
    return any(i not in parts.path for i in _FRAGMENT_ID.findall(parts.fragment))
