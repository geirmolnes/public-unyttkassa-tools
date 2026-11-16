"""Filenames and Content-Disposition."""

from datetime import date

import pytest

from src.naming import (
    batch_zip_filename,
    company_folder,
    content_disposition,
    pdf_filename,
    sanitize_name,
    zip_filename,
)


@pytest.mark.parametrize(
    "name,expected",
    [
        ("HJR HOLDING AS", "HJR_HOLDING_AS"),
        ("BØLGEN & MOI AS", "BØLGEN_MOI_AS"),
        ("Ærlig Øl og Ål AS", "Ærlig_Øl_og_Ål_AS"),
        ("A/S NORSK / DANSK", "A_S_NORSK_DANSK"),
        ("BACK\\SLASH AS", "BACK_SLASH_AS"),
        ("  lots   of\tspace  ", "lots_of_space"),
        ("DOTS A.S.", "DOTS_AS"),
        ('QUOTE "X" AS', "QUOTE_X_AS"),
        ("../../etc/passwd", "etc_passwd"),
        ("SJØ-BYGG AS", "SJØ-BYGG_AS"),
        ("", "selskap"),
        ("&&& / ///", "selskap"),
        ("Å", "Å"),
    ],
    ids=[
        "plain",
        "ampersand",
        "aeoeaa",
        "slashes",
        "backslash",
        "whitespace",
        "dots",
        "quotes",
        "path-traversal",
        "hyphen",
        "empty",
        "only-symbols",
        "single-char",
    ],
)
def test_sanitize_name(name, expected):
    assert sanitize_name(name) == expected


@pytest.mark.parametrize(
    "length,expected_len",
    [(79, 79), (80, 80), (81, 80), (500, 80)],
    ids=["below-cap", "at-cap", "above-cap", "far-above"],
)
def test_sanitize_name_caps_length(length, expected_len):
    assert len(sanitize_name("Ø" * length)) == expected_len


def test_sanitize_name_truncation_does_not_leave_trailing_underscore():
    assert sanitize_name("A" * 79 + " BCD") == "A" * 79


def test_sanitize_name_decomposed_unicode_is_kept():
    # macOS-style NFD "å" (a + combining ring) must survive as one letter
    assert sanitize_name("Gårdsbruk AS") == "Gårdsbruk_AS"


def test_pdf_filename():
    assert pdf_filename("HJR HOLDING AS", 2024) == "HJR_HOLDING_AS_2024.pdf"


def test_company_folder():
    assert company_folder("HJR HOLDING AS", "998812623") == "HJR_HOLDING_AS_998812623"


@pytest.mark.parametrize(
    "years,expected",
    [
        ([2012, 2013, 2025], "HJR_HOLDING_AS_2012-2025.zip"),
        ([2025, 2012], "HJR_HOLDING_AS_2012-2025.zip"),
        ([2024], "HJR_HOLDING_AS_2024.zip"),
    ],
    ids=["range", "unsorted", "single"],
)
def test_zip_filename(years, expected):
    assert zip_filename("HJR HOLDING AS", years) == expected


@pytest.mark.parametrize(
    "n,expected",
    [(1, "årsregnskap_1_selskap_2026-09-30.zip"), (2, "årsregnskap_2_selskaper_2026-09-30.zip")],
)
def test_batch_zip_filename(n, expected):
    assert batch_zip_filename(n, date(2026, 9, 30)) == expected


@pytest.mark.parametrize(
    "filename,expected",
    [
        (
            "HJR_HOLDING_AS_2024.pdf",
            "attachment; filename=\"HJR_HOLDING_AS_2024.pdf\"; filename*=UTF-8''HJR_HOLDING_AS_2024.pdf",
        ),
        (
            "BØLGEN_MOI_AS_2024.pdf",
            "attachment; filename=\"BOLGEN_MOI_AS_2024.pdf\"; filename*=UTF-8''B%C3%98LGEN_MOI_AS_2024.pdf",
        ),
        (
            "årsregnskap_2_selskaper.zip",
            "attachment; filename=\"aarsregnskap_2_selskaper.zip\"; filename*=UTF-8''%C3%A5rsregnskap_2_selskaper.zip",
        ),
        ('a"b.pdf', "attachment; filename=\"ab.pdf\"; filename*=UTF-8''a%22b.pdf"),
    ],
    ids=["ascii", "oe", "aa", "quote"],
)
def test_content_disposition(filename, expected):
    assert content_disposition(filename) == expected
