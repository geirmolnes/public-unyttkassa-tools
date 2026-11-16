"""Aksjonærregisteret import: parsing quirks of Skatteetaten's CSV and per-year replace."""

from contextlib import closing
from pathlib import Path

import pytest

from src import shareholders
from src.shareholders import BadFile, import_file, import_files, parse_holder_id

HEADER = (
    "﻿Orgnr;Selskap;Aksjeklasse;Navn aksjonær;Fødselsår/orgnr;Postnr/sted;Landkode;"
    "Antall aksjer;Antall aksjer selskap"
)
ROWS_2025 = [
    "810034882;SANDNES ELEKTRISKE AS;Ordinære aksjer;OLA NORDMANN;1981;4020 STAVANGER;NO;600;5900",
    "810034882;SANDNES ELEKTRISKE AS;Ordinære aksjer;HOLDCO AS;998812623;0150 OSLO;NO;5300;5900",
    # Stray quote, blank id and leading ", " place: verbatim from the 2025 file.
    '897205122;OSE WATER AS;Ordinære aksjer;QWELL HOLDING LTD. ("SELSKAP; ;, Dubai International Financial Cen;AE;354;694',
    "897205122;OSE WATER AS;B-aksjer;KARI NORDMANN;1970;5003 BERGEN;NO;10;40",
]


def _csv(tmp_path: Path, year: int, rows: list[str], header: str = HEADER) -> Path:
    path = tmp_path / f"aksjeeiebok_{year}.csv"
    path.write_text("\r\n".join([header, *rows]) + "\r\n", encoding="utf-8")
    return path


@pytest.fixture
def conn(tmp_path):
    c = shareholders.connect(tmp_path / "shareholders.db")
    yield c
    c.close()


def _holdings(conn, year: int) -> list[tuple]:
    return [
        tuple(r)
        for r in conn.execute(
            "SELECT h.orgnr, c.name, h.holder_name, h.holder_orgnr, h.holder_birth_year, "
            "h.holder_postcode, h.holder_place, h.country, h.shares, h.lots FROM holdings h "
            "JOIN share_classes c ON c.id = h.class_id WHERE year = ? ORDER BY h.rowid",
            (year,),
        )
    ]


@pytest.mark.parametrize(
    "value,expected",
    [
        ("998812623", ("998812623", None)),
        ("1981", (None, 1981)),
        (" 1981 ", (None, 1981)),
        ("198", (None, None)),
        ("19811", (None, None)),
        ("99881262", (None, None)),
        ("9988126230", (None, None)),
        ("", (None, None)),
        (" ", (None, None)),
        ("19a1", (None, None)),
    ],
    ids=["orgnr", "year", "padded", "3", "5", "8", "10", "empty", "space", "non-digit"],
)
def test_parse_holder_id(value, expected):
    assert parse_holder_id(value) == expected


def test_import_parses_every_field(conn, tmp_path):
    stats = import_file(conn, _csv(tmp_path, 2025, ROWS_2025))
    assert (stats.rows, stats.skipped) == (4, 0)
    assert _holdings(conn, 2025) == [
        (
            "810034882",
            "Ordinære aksjer",
            "OLA NORDMANN",
            None,
            1981,
            "4020",
            "STAVANGER",
            "NO",
            600,
            1,
        ),
        (
            "810034882",
            "Ordinære aksjer",
            "HOLDCO AS",
            "998812623",
            None,
            "0150",
            "OSLO",
            "NO",
            5300,
            1,
        ),
        (
            "897205122",
            "Ordinære aksjer",
            'QWELL HOLDING LTD. ("SELSKAP',
            None,
            None,
            None,
            "Dubai International Financial Cen",
            "AE",
            354,
            1,
        ),
        ("897205122", "B-aksjer", "KARI NORDMANN", None, 1970, "5003", "BERGEN", "NO", 10, 1),
    ]
    assert shareholders.check(conn) == {}


@pytest.mark.parametrize(
    "line,expected",
    [
        (
            "988120545;MOL NORGE AS;Ordinære aksjer;MOL NORDSJÖN BV; ;, 1011 PZ; Amsterdam;NL;30000001;30000001",
            ("988120545", "MOL NORDSJÖN BV", "1011 PZ; Amsterdam", "NL", 30000001, 30000001),
        ),
        (
            "1;A AS;Ordinære aksjer;B; ;x;y;z;SE;100;100",
            ("1", "B", "x;y;z", "SE", 100, 100),
        ),
        (
            "930832189;ERNSTRÖM ELECTRO AS;Ordinære aksjer;KTA INVEST AS;927282550;3320 VESTFOSSEN;NO;10900061247;",
            ("930832189", "KTA INVEST AS", "VESTFOSSEN", "NO", 10900061247, None),
        ),
    ],
    ids=["semicolon-in-place", "two-semicolons", "blank-company-total"],
)
def test_quirky_lines_from_real_files_are_kept(conn, tmp_path, line, expected):
    """Lines the 2020–2023 files had that a strict 9-field parser dropped."""
    stats = import_file(conn, _csv(tmp_path, 2023, [line]))
    row = conn.execute(
        "SELECT h.orgnr, h.holder_name, h.holder_place, h.country, h.shares, cc.total_shares "
        "FROM holdings h LEFT JOIN company_classes cc USING (year, orgnr, class_id)"
    ).fetchone()
    assert (stats.skipped, tuple(row)) == (0, expected)


@pytest.mark.parametrize(
    "value,country,expected",
    [
        ("4020 STAVANGER", "NO", ("4020", "STAVANGER")),
        ("0191, OSLO", "NO", ("0191", "OSLO")),
        ("0191,  OSLO ", "NO", ("0191", "OSLO")),
        ("0191OSLO", "NO", (None, "0191OSLO")),
        ("191 OSLO", "NO", (None, "191 OSLO")),
        ("01910 OSLO", "NO", (None, "01910 OSLO")),
        ("4005, Stavanger", "SE", (None, "4005, Stavanger")),
        (", 1011 PZ; Amsterdam", "NL", (None, "1011 PZ; Amsterdam")),
        ("", "NO", (None, None)),
        (" , ", "NO", (None, None)),
    ],
    ids=[
        "no",
        "no-comma",
        "no-spaces",
        "no-gap",
        "3-digit",
        "5-digit",
        "foreign-digits",
        "foreign",
        "blank",
        "only-comma",
    ],
)
def test_split_place(value, country, expected):
    assert shareholders.split_place(value, country) == expected


@pytest.mark.parametrize(
    "value,expected",
    [
        ("REYL  & CIE S.A.", "REYL & CIE S.A."),
        ("  A\tB ", "A B"),
        ("", None),
        ("   ", None),
        ("A", "A"),
    ],
    ids=["double-space", "tab-and-edges", "empty", "spaces", "one-char"],
)
def test_clean_text(value, expected):
    assert shareholders.clean_text(value) == expected


def test_identical_lots_are_summed_into_one_holding(conn, tmp_path):
    """2025: ROLF ØFSTI is listed twice in 886582412 with the same fields; only both together
    add up to the company total, so they are separate lots, not duplicates to drop."""
    lot = "886582412;X AS;Ordinære aksjer;ROLF ØFSTI;1959;7011 TRONDHEIM;NO;100000;200001"
    other = "886582412;X AS;Ordinære aksjer;ROLF ØFSTI;1959;7012 TRONDHEIM;NO;1;200001"
    stats = import_file(conn, _csv(tmp_path, 2025, [lot, other, lot]))
    rows = conn.execute("SELECT holder_postcode, shares, lots FROM holdings ORDER BY 1").fetchall()
    assert (stats.rows, [tuple(r) for r in rows]) == (3, [("7011", 200000, 2), ("7012", 1, 1)])
    assert shareholders.check(conn) == {}


@pytest.mark.parametrize(
    "line,expected",
    [
        ("913748174;X AS;Ordinære aksjer;;1980;;NO;10;10", (None, None, None, 10)),
        (
            "964750882;X AS;Ordinære aksjer;TECO  MARITIME AS;;, Oslo;NO;5;0",
            ("TECO MARITIME AS", None, "Oslo", None),
        ),
    ],
    ids=["blank-name-and-place", "zero-total-and-double-space"],
)
def test_blanks_become_null(conn, tmp_path, line, expected):
    """2024 has holders with no name; 2020 TECO MARITIME GROUP AS has a company total of 0."""
    import_file(conn, _csv(tmp_path, 2024, [line]))
    row = conn.execute(
        "SELECT holder_name, holder_postcode, holder_place, total_shares FROM holdings "
        "JOIN company_classes USING (year, orgnr, class_id)"
    ).fetchone()
    assert (tuple(row), shareholders.check(conn)) == (expected, {})


def test_company_split_across_file_is_rejected(conn, tmp_path):
    rows = [ROWS_2025[0], ROWS_2025[3], ROWS_2025[1]]
    with pytest.raises(BadFile, match="line 4: 810034882 seen earlier"):
        import_file(conn, _csv(tmp_path, 2025, rows))


@pytest.mark.parametrize(
    "sql,broken",
    [
        ("UPDATE holdings SET holder_name = 'A  B'", "untrimmed or double-spaced text"),
        ("UPDATE holdings SET holder_place = ''", "empty strings instead of NULL"),
        ("UPDATE holdings SET holder_postcode = '401'", "Norwegian postcode not 4 digits"),
        (  # lots = 0 keeps the line count intact, so only the duplicate shows
            "INSERT INTO holdings SELECT year, orgnr, class_id, holder_name, holder_orgnr, "
            "holder_birth_year, holder_postcode, holder_place, country, shares, 0 FROM holdings",
            "duplicate holder rows",
        ),
        ("UPDATE company_classes SET total_shares = 0", "zero company totals"),
        ("UPDATE holdings SET lots = 2", "source lines lost"),
    ],
    ids=["spaces", "empty", "postcode", "duplicate", "zero-total", "lost-lines"],
)
def test_check_detects_each_broken_invariant(conn, tmp_path, sql, broken):
    import_file(conn, _csv(tmp_path, 2025, ROWS_2025[:1]))
    conn.execute(sql)
    assert list(shareholders.check(conn)) == [broken]


def test_company_totals_stored_once_per_class(conn, tmp_path):
    import_file(conn, _csv(tmp_path, 2025, ROWS_2025))
    rows = conn.execute(
        "SELECT cc.orgnr, co.name, c.name, cc.total_shares FROM company_classes cc "
        "JOIN companies co USING (year, orgnr) JOIN share_classes c ON c.id = cc.class_id "
        "ORDER BY cc.orgnr, c.name"
    ).fetchall()
    assert [tuple(r) for r in rows] == [
        ("810034882", "SANDNES ELEKTRISKE AS", "Ordinære aksjer", 5900),
        ("897205122", "OSE WATER AS", "B-aksjer", 40),
        ("897205122", "OSE WATER AS", "Ordinære aksjer", 694),
    ]


@pytest.mark.parametrize(
    "bad_line",
    [
        "810034882;SANDNES ELEKTRISKE AS;Ordinære aksjer;X;1981;4020 STAVANGER;NO;600",
        "810034882;SANDNES ELEKTRISKE AS;Ordinære aksjer;X;1981;4020 STAVANGER;NO;600;5900;1",
        "810034882;SANDNES ELEKTRISKE AS;Ordinære aksjer;X;1981;40;20 STAVANGER;N0;600;5900",
        "810034882;SANDNES ELEKTRISKE AS;Ordinære aksjer;X;1981;4020 STAVANGER;NO;6,5;5900",
        "8100348X2;SANDNES ELEKTRISKE AS;Ordinære aksjer;X;1981;4020 STAVANGER;NO;600;5900",
    ],
    ids=["8-fields", "10-fields", "10-fields-no-country", "non-int-shares", "non-digit-orgnr"],
)
def test_malformed_line_is_skipped_and_reported(conn, tmp_path, bad_line):
    stats = import_file(conn, _csv(tmp_path, 2025, [ROWS_2025[0], bad_line, ROWS_2025[1]]))
    assert (stats.rows, stats.skipped, stats.skipped_examples) == (2, 1, [(3, bad_line)])


def test_reimport_replaces_year_and_keeps_others(conn, tmp_path):
    import_files(conn, [_csv(tmp_path, 2024, ROWS_2025[:1]), _csv(tmp_path, 2025, ROWS_2025)])
    import_file(conn, _csv(tmp_path, 2025, ROWS_2025[1:2]))
    counts = conn.execute("SELECT year, COUNT(*) FROM holdings GROUP BY year").fetchall()
    imports = conn.execute("SELECT year, rows FROM imports ORDER BY year").fetchall()
    assert ([tuple(r) for r in counts], [tuple(r) for r in imports]) == (
        [(2024, 1), (2025, 1)],
        [(2024, 1), (2025, 1)],
    )


def test_bad_header_leaves_existing_year_untouched(conn, tmp_path):
    import_file(conn, _csv(tmp_path, 2025, ROWS_2025))
    with pytest.raises(BadFile):
        import_file(conn, _csv(tmp_path, 2025, ROWS_2025, header="orgnr;navn"))
    assert conn.execute("SELECT COUNT(*) FROM holdings").fetchone()[0] == 4


def test_import_files_builds_indexes(conn, tmp_path):
    import_files(conn, [_csv(tmp_path, 2025, ROWS_2025)])
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
    assert {"holdings_company", "holdings_holder"} <= names


@pytest.mark.parametrize(
    "name",
    ["aksjeeiebok_25.csv", "aksjeeiebok_2025.xlsx", "aksjonærer_2025.csv"],
    ids=["short-year", "wrong-ext", "wrong-prefix"],
)
def test_cli_rejects_unexpected_file_name(tmp_path, capsys, name):
    path = tmp_path / name
    path.write_text(HEADER, encoding="utf-8")
    assert shareholders.main([str(path), "--db", str(tmp_path / "s.db")]) == 2
    assert "aksjeeiebok_2025.csv" in capsys.readouterr().err


def test_cli_imports_and_reports(tmp_path, capsys):
    db = tmp_path / "s.db"
    assert shareholders.main([str(_csv(tmp_path, 2025, ROWS_2025)), "--db", str(db)]) == 0
    assert capsys.readouterr().out.splitlines()[0] == "2025: 4 rows, 0 skipped"


def test_open_live_reads_env_path(tmp_path, monkeypatch):
    path = tmp_path / "aksjonaerregisteret.db"
    with shareholders.connect(path) as c:
        import_file(c, _csv(tmp_path, 2025, ROWS_2025))
        c.execute("PRAGMA journal_mode=DELETE")
    monkeypatch.setenv("SHAREHOLDERS_DB_PATH", str(path))
    with closing(shareholders.open_live()) as live:
        assert live.execute("SELECT COUNT(*) FROM holdings").fetchone()[0] == 4


def test_open_live_sees_rows_still_in_wal(tmp_path, monkeypatch):
    """Datachat writes indexes in WAL mode; an immutable open would miss uncheckpointed pages."""
    path = tmp_path / "aksjonaerregisteret.db"
    writer = shareholders.connect(path)
    writer.execute("PRAGMA wal_autocheckpoint=0")
    import_file(writer, _csv(tmp_path, 2025, ROWS_2025))
    monkeypatch.setenv("SHAREHOLDERS_DB_PATH", str(path))
    with closing(shareholders.open_live()) as live:
        assert live.execute("SELECT COUNT(*) FROM holdings").fetchone()[0] == 4
    writer.close()


def test_open_live_missing_file(tmp_path, monkeypatch):
    monkeypatch.setenv("SHAREHOLDERS_DB_PATH", str(tmp_path / "nope.db"))
    with pytest.raises(FileNotFoundError):
        shareholders.open_live()
