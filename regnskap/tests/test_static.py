"""Invariants on the static stylesheet."""

import re
from pathlib import Path

CSS = (Path(__file__).parent.parent / "src" / "static" / "style.css").read_text()


def test_search_box_focus_has_no_outer_ring():
    """Focused search fields recolour their own border instead of drawing a second box outside it."""
    rule = re.search(r"\.search-box:focus-within\s*\{([^}]*)\}", CSS)
    assert rule, "missing .search-box:focus-within rule"
    assert "outline-offset" not in rule.group(1)
    assert "border-color" in rule.group(1)


def test_no_single_key_favourite_shortcut():
    """Favourites toggle only via the button; a bare "f" keypress unfavourited by accident."""
    js = (Path(__file__).parent.parent / "src" / "static" / "app.js").read_text()
    assert not re.search(r"""e\.key === ["']f["']""", js, re.IGNORECASE)


def test_company_page_downloads_from_one_menu():
    """Every download sits behind one "Last ned" menu in the header (five competing buttons was confusing);
    the top of the page is for reading the årsregnskap, not for downloading it."""
    html = (Path(__file__).parent.parent / "src" / "templates" / "index.html").read_text()
    head = html[html.index('<div class="head-btns">') : html.index('id="c-facts"')]
    docs = html[html.index('<div id="docs">') : html.index('id="no-years"')]
    assert ('id="dl-btn"' in head, 'aria-controls="dl-pop"' in head) == (True, True)
    assert ("Last ned" in docs, "data-preset" in html, "data-last=" in docs) == (
        False,
        False,
        False,
    )


def test_every_module_import_is_versioned():
    """Each module app.js and friends import has an import-map entry with ?v=, or a deploy can
    leave browsers on a stale copy of it."""
    static = Path(__file__).parent.parent / "src" / "static"
    html = (Path(__file__).parent.parent / "src" / "templates" / "index.html").read_text()
    imported = {
        m for f in static.glob("*.js") for m in re.findall(r'from "\./([\w-]+\.js)"', f.read_text())
    }
    assert imported, "no module imports found"
    for name in imported:
        assert f'"/static/{name}": "/static/{name}?v={{{{ asset_version }}}}"' in html, name


def test_no_class_rule_defined_twice():
    """Regression: the figures' year buttons reused `.seg`, the sidebar theme switcher's class, and
    their later rule broke the switcher's layout. A second top-level `.name {` rule is a clash."""
    rules = re.findall(r"^(\.[\w-]+) \{", CSS, re.M)
    assert sorted({r for r in rules if rules.count(r) > 1}) == []


def test_open_company_shows_in_nylig_sett():
    """Regression: the sidebar's Nylig sett hid the open company; it belongs on top, removable with ×."""
    js = (Path(__file__).parent.parent / "src" / "static" / "app.js").read_text()
    nav = js[js.index("function renderNav") :]
    nav = nav[: nav.index("\n  }\n")]
    assert "const recent = state.recent.slice(0, 6);" in nav
    opened = js[js.index("async function openCompany") :]
    opened = opened[: opened.index("\n  }\n")]
    assert "state.recent = recentFirst(state.recent, data.company);" in opened


def test_structure_button_says_vis():
    """The mini map's button reads "Vis selskapsstruktur", not "Lag …"; making it is an implementation detail."""
    js = (Path(__file__).parent.parent / "src" / "static" / "app.js").read_text()
    assert "Vis ${root.what} →" in js
    assert "`Lag ${root.what}`" not in js


def test_reading_a_year_explains_the_extra_year_once():
    """Reading 2022 also reads 2023 (its prior-year column checks 2022). That surprised the user, so the first time
    a note says why, and remembers it was shown."""
    js = (Path(__file__).parent.parent / "src" / "static" / "app.js").read_text()
    body = js[js.index("async function readFigYears") :]
    body = body[: body.index("\n  }\n")]
    assert "controlYears(" in body and "explainControlRead(" in body
    note = js[js.index("function explainControlRead") :]
    note = note[: note.index("\n  }\n")]
    assert "localStorage.setItem(SEEN_CONTROL" in note
    assert "fjorårstall" in note


def _year_button() -> str:
    js = (Path(__file__).parent.parent / "src" / "static" / "app.js").read_text()
    body = js[js.index("function yearButton") :]
    return body[: body.index("\n  }\n")]


def test_a_failed_read_offers_a_retry_button():
    """A failed year said "Prøv igjen" as plain red text, with nothing to click (Læringsverkstedet 2023)."""
    failed = _year_button().split('r.status === "failed"')[1].split("\n    }")[0]
    assert ('data-read="${y}"' in failed, "Prøv igjen" in failed) == (True, True)


def test_each_year_in_the_row_is_one_click_into_the_reader():
    """Company top (Årsrad B): a read year opens the reader, a busy one does nothing, an unread one isn't in the row."""
    body = _year_button()
    ok = body.split('r.status === "ok"')[1].split("\n    }")[0]
    busy = body.split("accBusy(r)")[1].split("\n    }")[0]
    assert ('data-reader="${y}"' in ok, "data-re" in busy, 'if (!r) return "";' in body) == (
        True,
        False,
        True,
    )


def _block(src: str, start: str) -> str:
    body = src[src.index(start) :]
    return body[: body.index("\n  }\n")]


def test_unread_years_wait_behind_one_button():
    """15 PDF years gave 11 dashed "2014 Les inn" buttons in the row: "janky, a lot of text repeated". The row holds
    read, busy and failed years; the unread ones are chips in one "Les inn flere år" menu, a click starts the read."""
    js = (Path(__file__).parent.parent / "src" / "static" / "app.js").read_text()
    html = (Path(__file__).parent.parent / "src" / "templates" / "index.html").read_text()
    row = html[html.index('<div class="doc-row">') : html.index('id="doc-more-pop"')]
    year = _year_button()
    menu = _block(js, "function setMoreMenu")
    clicks = _block(js, '$("docs").addEventListener("click"')
    assert (
        "Les inn" in year,
        year.rstrip().endswith('return "";'),
        row.count("Les inn"),
        'unreadYears(state.data.years, [], state.accRows).map((y) => `<button type="button" class="yr" data-read="${y}">'
        in menu,
        "readYears([Number(rd.dataset.read)], rd)" in clicks,
    ) == (False, True, 1, True, True)


def test_more_years_says_flere_only_after_other_years():
    """With nothing in the row yet, "Les inn flere år" is wrong: the button says "Les inn"."""
    js = (Path(__file__).parent.parent / "src" / "static" / "app.js").read_text()
    body = _block(js, "function renderAccYears")
    assert '$("doc-years").childElementCount ? "Les inn flere år" : "Les inn"' in body


def test_company_search_opens_the_reader_with_its_hits():
    """The company page's search box hands its query to the reader, which runs it on arrival (not when it came
    from a hit in another year, where it shows that page)."""
    js = (Path(__file__).parent.parent / "src" / "static" / "app.js").read_text()
    search = js[js.index("function searchAccounts") :]
    search = search[: search.index("\n  }\n")]
    assert 'go("accounts"' in search and "q: q.trim()" in search
    assert 'if (!a.goto && $("a-q").value.trim().length >= 2) runSearch();' in js


def test_a_brreg_year_offers_reading_its_pdf():
    """2024 showed only "Brreg" with no way to read its PDF from the table (Læringsverkstedet)."""
    js = (Path(__file__).parent.parent / "src" / "static" / "app.js").read_text()
    head = js[js.index("function figHead") :]
    head = head[: head.index("\n  }\n")]
    assert "!c.f.pdf" in head and 'data-fig-read="${c.year}"' in head.split("const offer")[1]


# Called without being defined in app.js: JS builtins, destructured callback parameters and CSS functions in strings.
NOT_DEFINED_HERE = {
    "Boolean",
    "Date",
    "Error",
    "Map",
    "MutationObserver",
    "Number",
    "Set",
    "String",
    "URLSearchParams",
    "clearTimeout",
    "decodeURIComponent",
    "encodeURIComponent",
    "fetch",
    "getComputedStyle",
    "parseInt",
    "setTimeout",
    "col",
    "exclude",
    "getItems",
    "onPick",
    "rerender",
    "scale",
    "translate",
}


def defined_and_called(js: str) -> tuple[set[str], set[str]]:
    code = re.sub(r"//[^\n]*", "", js)
    defined = set(re.findall(r"function\s*\*?\s*([A-Za-z_$][\w$]*)\s*\(", code))
    defined |= set(re.findall(r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=", code))
    for names in re.findall(r"import\s*\{([^}]*)\}", code):
        defined |= {n.split(" as ")[-1].strip() for n in names.split(",")}
    called = set(re.findall(r"(?<![.\w$])([A-Za-z_$][\w$]*)\(", code))
    # functions handed over by name: .map(accLine), addEventListener("click", readSelected)
    called |= set(
        re.findall(
            r"\.(?:map|flatMap|forEach|filter|some|every|find|findIndex|sort)\(\s*([A-Za-z_$][\w$]*)\s*\)",
            code,
        )
    )
    called |= set(
        re.findall(r"addEventListener\(\s*\"[\w-]+\"\s*,\s*([A-Za-z_$][\w$]*)\s*[,)]", code)
    )
    return defined, called


def test_app_js_calls_only_what_it_defines():
    """Regression: removing accLine left the reader's year switch calling it; the reader broke with a
    ReferenceError that no test saw. Every bare call must name a function, const or import of app.js."""
    js = (Path(__file__).parent.parent / "src" / "static" / "app.js").read_text()
    defined, called = defined_and_called(js)
    assert sorted(called - defined - NOT_DEFINED_HERE) == []


def test_mini_map_stake_is_on_the_line():
    """Regression: the company page's mini map put each stake in the box, as if it were the box's own;
    like the full map it belongs on the box's line to the bar. The «+ N» boxes carry no stake."""
    js = (Path(__file__).parent.parent / "src" / "static" / "app.js").read_text()
    mini = js[js.index("function miniMap(") : js.index('$("own-block")')]
    assert (mini.count("stakeLabel("), "fmtPct(" in mini) == (2, False)
    rule = re.search(r"\.mbox \.ep \{([^}]*)\}", CSS)
    assert rule and "position: absolute" in rule.group(1)
    assert re.search(r"\.mrow\.up \.mbox \.ep \{[^}]*bottom: calc\(-1px - var\(--drop\) / 2\)", CSS)
