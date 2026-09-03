"""Tests for the reusable deterministic field normalizers (Stage 4, step 6).

Pure functions - no database, no AI, no network.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.models.normalization import NormalizationAttempt, NormalizationLineItem
from app.schemas.normalization import NormalizationErrorCode as EC
from app.services.processing.normalization.iso4217 import APPROVED_CURRENCY_CODES
from app.services.processing.normalization.normalizers import (
    FieldError,
    MAX_INVOICE_NUMBER,
    MAX_LINE_ITEM_DESCRIPTION,
    MAX_PARTY_NAME,
    MAX_TAX_ID,
    NormResult,
    clean_text,
    normalize_currency,
    normalize_date,
    normalize_invoice_number,
    normalize_money,
    normalize_quantity,
    normalize_tax_id,
    normalize_text,
)


def _val(result):
    assert result.error is None, result.error
    return result.value


def _code(result):
    assert result.error is not None
    return result.error.code


def test_norm_result_rejects_value_and_error_together() -> None:
    with pytest.raises(ValueError, match="both value and error"):
        NormResult(
            value="EUR",
            error=FieldError(EC.UNKNOWN_CURRENCY, "Unknown currency."),
        )


# --- shared cleanup ----------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  Acme  GmbH  ", "Acme GmbH"),
        ("Acme\tGmbH\n(Ltd.)", "Acme GmbH (Ltd.)"),
        ("Café Müller & Co.", "Café Müller & Co."),  # accents/punctuation kept
        ("A​B﻿C", "ABC"),  # zero-width + BOM removed
        ("no break space", "no break space"),  # nbsp / narrow nbsp -> space
    ],
)
def test_clean_text(raw: str, expected: str) -> None:
    assert clean_text(raw) == expected


def test_clean_text_does_not_change_case_or_fold_accents() -> None:
    assert clean_text("MÜLLER Straße") == "MÜLLER Straße"


def test_clean_text_preserves_format_characters_not_named_by_policy() -> None:
    assert clean_text("A\u2060B") == "A\u2060B"  # WORD JOINER is outside U+200B-D/BOM


# --- general text and identifiers -----------------------------------


def test_text_length_caps_match_the_orm_columns() -> None:
    cols = NormalizationAttempt.__table__.columns
    assert MAX_INVOICE_NUMBER == cols["invoice_number"].type.length
    assert MAX_TAX_ID == cols["vendor_tax_id"].type.length
    assert MAX_PARTY_NAME == cols["vendor_name"].type.length == cols["customer_name"].type.length
    assert MAX_LINE_ITEM_DESCRIPTION == NormalizationLineItem.__table__.columns["description"].type.length


@pytest.mark.parametrize("raw", [None, "", "   ", "\t\n", " "])
def test_text_absent_values_normalize_to_null_without_error(raw) -> None:
    result = normalize_text(raw, max_length=MAX_PARTY_NAME)
    assert result.value is None and result.error is None


def test_text_is_trimmed_and_collapsed() -> None:
    assert _val(normalize_text("  Beta   Ltd  ", max_length=MAX_PARTY_NAME)) == "Beta Ltd"


def test_text_over_the_limit_is_an_error_not_a_truncation() -> None:
    result = normalize_text("x" * (MAX_PARTY_NAME + 1), max_length=MAX_PARTY_NAME)
    assert result.value is None
    assert _code(result) is EC.TEXT_TOO_LONG


def test_identifier_preserves_internal_spaces_and_separators() -> None:
    assert _val(normalize_invoice_number("  INV 2026 / 0007 ")) == "INV 2026 / 0007"
    assert _val(normalize_tax_id("DE 123 456 789")) == "DE 123 456 789"


def test_identifier_caps_differ() -> None:
    assert _code(normalize_invoice_number("1" * (MAX_INVOICE_NUMBER + 1))) is EC.TEXT_TOO_LONG
    assert _val(normalize_tax_id("1" * MAX_TAX_ID)) == "1" * MAX_TAX_ID


# --- currency ------------------------------------------------------


@pytest.mark.parametrize("raw", ["EUR", " eur ", "Usd"])
def test_currency_trims_and_upper_cases_known_codes(raw: str) -> None:
    assert _val(normalize_currency(raw)) in APPROVED_CURRENCY_CODES


@pytest.mark.parametrize("raw", [None, "", "   "])
def test_currency_absent_is_null_without_error(raw) -> None:
    result = normalize_currency(raw)
    assert result.value is None and result.error is None


@pytest.mark.parametrize("raw", ["EU", "EURO", "E U", "12", "$"])
def test_currency_malformed_is_invalid_currency(raw: str) -> None:
    assert _code(normalize_currency(raw)) is EC.INVALID_CURRENCY


@pytest.mark.parametrize("raw", ["DEM", "FRF", "XXX", "XAU", "XDR", "ZZZ"])
def test_currency_well_formed_but_not_approved_is_unknown_currency(raw: str) -> None:
    assert _code(normalize_currency(raw)) is EC.UNKNOWN_CURRENCY


def test_currency_allow_list_excludes_metals_and_test_codes() -> None:
    for excluded in ("XAU", "XAG", "XPT", "XPD", "XXX", "XTS", "XDR", "XSU", "XUA"):
        assert excluded not in APPROVED_CURRENCY_CODES
    for kept in ("EUR", "USD", "GBP", "XAF", "XCD", "XOF", "XPF"):
        assert kept in APPROVED_CURRENCY_CODES


def test_currency_allow_list_excludes_withdrawn_codes() -> None:
    for withdrawn in ("ANG", "BGN", "SLL", "ZWL"):
        assert withdrawn not in APPROVED_CURRENCY_CODES
        assert _code(normalize_currency(withdrawn)) is EC.UNKNOWN_CURRENCY


# --- money and quantities ----------------------------------------


def test_decimal_passes_through_with_sign_and_scale_preserved() -> None:
    assert _val(normalize_money(Decimal("119.00"))) == Decimal("119.00")
    assert str(_val(normalize_money(Decimal("119.00")))) == "119.00"  # trailing zeros kept
    assert _val(normalize_money(Decimal("-42.5"))) == Decimal("-42.5")  # negative preserved
    assert _val(normalize_quantity(Decimal("2"))) == Decimal("2")


@pytest.mark.parametrize("raw", [None])
def test_money_absent_is_null_without_error(raw) -> None:
    result = normalize_money(raw)
    assert result.value is None and result.error is None


@pytest.mark.parametrize("raw", [Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity"), True, 3.14])
def test_money_non_finite_or_wrong_type_is_invalid_number(raw) -> None:
    assert _code(normalize_money(raw)) is EC.INVALID_NUMBER


def test_money_integer_object_is_not_an_accepted_decimal_input() -> None:
    assert _code(normalize_money(123)) is EC.INVALID_NUMBER


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1,234.56", Decimal("1234.56")),
        ("1,234", Decimal("1234")),
        ("1,234,567.89", Decimal("1234567.89")),
        ("1 234.56", Decimal("1234.56")),
        ("$1,234.56", Decimal("1234.56")),
        ("USD 1 234.56", Decimal("1234.56")),
        ("(123.45)", Decimal("-123.45")),
        ("123.45-", Decimal("-123.45")),
        ("+99", Decimal("99")),
    ],
)
def test_money_string_parsing_follows_the_documented_policy(raw: str, expected: Decimal) -> None:
    assert _val(normalize_money(raw)) == expected


@pytest.mark.parametrize("raw", ["1234,56", "1.234,56", "1 234,56"])
def test_money_decimal_comma_is_ambiguous_number(raw: str) -> None:
    assert _code(normalize_money(raw)) is EC.AMBIGUOUS_NUMBER


@pytest.mark.parametrize(
    "raw",
    ["1,23,456", "12.34.56", "12 34", "1,234 567", "-123-", "abc", "1..2", ""],
)
def test_money_unparseable_string_is_invalid_number_or_absent(raw: str) -> None:
    result = normalize_money(raw)
    if raw == "":
        assert result.value is None and result.error is None
    else:
        assert _code(result) is EC.INVALID_NUMBER


# --- dates -------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-01-15", "2026-01-15"),
        ("2026/1/5", "2026-01-05"),
        ("2026.01.15", "2026-01-15"),
        ("15 Jan 2026", "2026-01-15"),
        ("15 January 2026", "2026-01-15"),
        ("15-Jan-2026", "2026-01-15"),
        ("Jan 15, 2026", "2026-01-15"),
        ("January 15 2026", "2026-01-15"),
        ("1st Feb 2026", "2026-02-01"),
        ("15th January 2026.", "2026-01-15"),
        ("13/04/2026", "2026-04-13"),  # 13 > 12 -> day resolved
        ("04/13/2026", "2026-04-13"),
        ("03/04/2026", "2026-04-03"),  # both <= 12 -> day-first default
        ("4-3-2026", "2026-03-04"),
    ],
)
def test_date_supported_formats(raw: str, expected: str) -> None:
    assert _val(normalize_date(raw)) == expected


@pytest.mark.parametrize("raw", [None, "", "   "])
def test_date_absent_is_null_without_error(raw) -> None:
    result = normalize_date(raw)
    assert result.value is None and result.error is None


@pytest.mark.parametrize(
    "raw",
    [
        "15/01/26",          # two-digit year
        "2026-02-30",        # impossible day
        "2026-13-01",        # impossible month
        "13/13/2026",        # both > 12
        "20260115",          # no separators
        "Q1 2026",           # quarter notation
        "Montag 15 2026",    # non-English weekday
        "15 Januar 2026",    # non-English month
        "sometime in 2026",  # free text
        "2026-01/15",        # mixed separators
        "15 Jan-2026",       # mixed separators
        "Sept 15 2026",      # month names are full or exactly 3 letters
        "Jan. 15, 2026",     # only one trailing period is stripped
        "٢٠٢٦-٠١-١٥",        # non-ASCII digits are outside the listed formats
    ],
)
def test_date_rejected_inputs_are_invalid_date(raw: str) -> None:
    assert _code(normalize_date(raw)) is EC.INVALID_DATE


def test_date_far_future_but_real_is_accepted() -> None:
    assert _val(normalize_date("2099-12-31")) == "2099-12-31"
