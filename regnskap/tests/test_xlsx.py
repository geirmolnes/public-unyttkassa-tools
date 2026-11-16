"""Excel export: /api/xlsx turns the page's rows into a workbook (read back with openpyxl)."""

import io

import pytest
from openpyxl import load_workbook

SHEETS = [
    {
        "name": "Resultat",
        "rows": [
            ["Post (NOK)", 2024, 2025],
            ["Driftsinntekter", 233268, 257000],
            ["herav lønn", None, -57000],
            ["Årsresultat", 25783920, 25264000],
        ],
        "bold": [3],
        "indent": [2],
    },
    {"name": "Balanse", "rows": [["Post (NOK)", 2024, 2025], ["EK-andel (%)", 42.4, 26.3]]},
]


def _post(app_client, body):
    return app_client.post("/api/xlsx", json=body)


def _book(resp):
    return load_workbook(io.BytesIO(resp.content))


def test_two_sheets_with_values_as_numbers(app_client):
    resp = _post(
        app_client, {"filename": "HJR_HOLDING_AS_selskapsregnskap_2024-2025.xlsx", "sheets": SHEETS}
    )
    wb = _book(resp)
    assert (
        resp.headers["content-disposition"],
        wb.sheetnames,
        [[c.value for c in r] for r in wb["Resultat"].iter_rows()],
        [[c.value for c in r] for r in wb["Balanse"].iter_rows()],
    ) == (
        'attachment; filename="HJR_HOLDING_AS_selskapsregnskap_2024-2025.xlsx"; '
        "filename*=UTF-8''HJR_HOLDING_AS_selskapsregnskap_2024-2025.xlsx",
        ["Resultat", "Balanse"],
        [
            ["Post (NOK)", 2024, 2025],
            ["Driftsinntekter", 233268, 257000],
            ["herav lønn", None, -57000],
            ["Årsresultat", 25783920, 25264000],
        ],
        [["Post (NOK)", 2024, 2025], ["EK-andel (%)", 42.4, 26.3]],
    )


def test_formatting(app_client):
    ws = _book(_post(app_client, {"filename": "x.xlsx", "sheets": SHEETS}))["Resultat"]
    assert (
        ws.freeze_panes,
        ws["A1"].font.b,
        ws["A4"].font.b,
        ws["A2"].font.b,
        ws["A3"].alignment.indent,
        ws["B2"].number_format,
    ) == ("B2", True, True, False, 1, "#,##0")


def test_decimals_get_one_decimal_format(app_client):
    ws = _book(_post(app_client, {"filename": "x.xlsx", "sheets": SHEETS}))["Balanse"]
    assert ws["B2"].number_format == "#,##0.0"


def test_norwegian_letters_in_filename(app_client):
    resp = _post(
        app_client, {"filename": "Bølgen_Moi_selskapsregnskap_2025.xlsx", "sheets": SHEETS}
    )
    assert resp.headers["content-disposition"].endswith(
        "filename*=UTF-8''B%C3%B8lgen_Moi_selskapsregnskap_2025.xlsx"
    )


@pytest.mark.parametrize(
    "body",
    [
        {"filename": "../etc/passwd.xlsx", "sheets": SHEETS},
        {"filename": "x.csv", "sheets": SHEETS},
        {"filename": "x.xlsx", "sheets": []},
        {"filename": "x.xlsx", "sheets": [{"name": "A/B", "rows": [["x"]]}]},
    ],
    ids=["path", "not-xlsx", "no-sheets", "slash-in-sheet-name"],
)
def test_rejects_bad_input(app_client, body):
    assert _post(app_client, body).status_code == 422


@pytest.mark.parametrize("n,status", [(31, 200), (32, 422)], ids=["31-chars", "32-chars"])
def test_sheet_name_length_limit(app_client, n, status):
    body = {"filename": "x.xlsx", "sheets": [{"name": "x" * n, "rows": [["x"]]}]}
    assert _post(app_client, body).status_code == status


@pytest.mark.parametrize("n,status", [(40, 200), (41, 422)], ids=["40-cols", "41-cols"])
def test_column_limit(app_client, n, status):
    body = {"filename": "x.xlsx", "sheets": [{"name": "A", "rows": [list(range(n))]}]}
    assert _post(app_client, body).status_code == status
