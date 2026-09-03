"""Tests for the reusable deterministic field normalizers (Stage 4, step 6).

Pure functions - no database, no AI, no network.
"""

from __future__ import annotations

from decimal import Decimal, localcontext

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


@pytest.mark.parametrize(
    "raw",
    [None, "", "   ", "\t\n", "  ", "​﻿"],
)
def test_text_empty_or_whitespace_or_invisible_only_is_absent(raw) -> None:
    result = normalize_text(raw, max_length=MAX_PARTY_NAME)
    assert result.value is None and result.error is None


def test_text_collapses_repeated_internal_whitespace() -> None:
    assert _val(normalize_text("Acme   \t  GmbH", max_length=MAX_PARTY_NAME)) == "Acme GmbH"


def test_text_description_becomes_single_line() -> None:
    assert _val(normalize_text("Widget\n  blue\r\n  size L", max_length=MAX_LINE_ITEM_DESCRIPTION)) == "Widget blue size L"


@pytest.mark.parametrize(
    "value",
    [
        "Acme, Inc. (DE) — Süd/West & Partner",
        "Müller’s “Best” Co.",
        "50% off / net-30",
    ],
)
def test_text_preserves_meaningful_punctuation_verbatim(value: str) -> None:
    assert _val(normalize_text(value, max_length=MAX_PARTY_NAME)) == value


@pytest.mark.parametrize(
    "raw",
    [
        "INV-2026/0007",
        "007",                 # leading zeros kept, not parsed
        "1,234",               # comma kept, not stripped
        "1.000",               # not read as a number
        "-42",                 # not read as a negative number
        "2026-0001",
        "DE123456789A",
        "GB 12 3456 78",
    ],
)
def test_identifier_is_returned_verbatim_as_a_string(raw: str) -> None:
    result = normalize_invoice_number(raw)
    assert result.value == raw
    assert isinstance(result.value, str)


def test_identifier_collapses_repeated_spaces_but_keeps_single_ones() -> None:
    assert _val(normalize_tax_id("DE  123   456  789")) == "DE 123 456 789"


@pytest.mark.parametrize("fn", [normalize_invoice_number, normalize_tax_id])
def test_identifier_blank_is_absent(fn) -> None:
    for raw in (None, "", "   ", " "):
        result = fn(raw)
        assert result.value is None and result.error is None


@pytest.mark.parametrize("raw", [123, 4.5, ("a", "b"), b"INV-1"])
def test_text_non_string_input_is_a_technical_error(raw) -> None:
    with pytest.raises(TypeError, match="requires a string or None"):
        normalize_text(raw, max_length=MAX_PARTY_NAME)


def test_text_length_is_measured_after_cleanup() -> None:
    # exactly the cap, plus whitespace that trims/collapses away -> accepted
    padded = "x" * MAX_PARTY_NAME + "   \t\n   "
    assert _val(normalize_text(padded, max_length=MAX_PARTY_NAME)) == "x" * MAX_PARTY_NAME
    # one real char over the cap, even with collapsible whitespace -> error
    over = "y y" + "z" * MAX_PARTY_NAME  # collapses to 1 + 1 + 1 + cap = cap + 2
    assert _code(normalize_text(over, max_length=MAX_PARTY_NAME)) is EC.TEXT_TOO_LONG


# --- currency ------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("EUR", "EUR"),
        ("eur", "EUR"),
        (" eur ", "EUR"),
        ("Usd", "USD"),
        ("uSd", "USD"),
        (" jpy ", "JPY"),   # no-break space trimmed
    ],
)
def test_currency_trims_upper_cases_and_returns_the_canonical_code(raw: str, expected: str) -> None:
    result = normalize_currency(raw)
    assert result.value == expected and result.error is None
    assert expected in APPROVED_CURRENCY_CODES


@pytest.mark.parametrize("raw", [None, "", "   ", "\t\n"])
def test_currency_absent_is_null_without_error_and_is_not_defaulted(raw) -> None:
    result = normalize_currency(raw)
    assert result.value is None and result.error is None  # never defaulted to EUR


@pytest.mark.parametrize(
    "raw",
    [
        "EU", "EURO", "E U R", "US$", "12", "978",   # numeric ISO code, not alphabetic
        "$", "€", "£", "¥", "US $",                  # symbols are not interpreted
        "EUR.", "E.U.R", "eu r",
        "E​UR", "​GBP﻿",                             # no text-field cleanup
        "ＥＵＲ",                                      # full-width letters are not ASCII
        "ßd",                                        # upper-case would widen to "SSD" - rejected before upper
    ],
)
def test_currency_not_three_ascii_letters_is_invalid_currency(raw: str) -> None:
    assert _code(normalize_currency(raw)) is EC.INVALID_CURRENCY


@pytest.mark.parametrize("raw", [123, 978, 3.5, b"EUR", ["EUR"]])
def test_currency_non_string_input_is_a_field_error_not_a_crash(raw) -> None:
    assert _code(normalize_currency(raw)) is EC.INVALID_CURRENCY


@pytest.mark.parametrize(
    "raw",
    ["DEM", "FRF", "ITL", "ESP", "XXX", "XTS", "XAU", "XAG", "XDR", "XSU", "ZZZ", "QQQ"],
)
def test_currency_well_formed_but_not_approved_is_unknown_currency(raw: str) -> None:
    result = normalize_currency(raw)
    assert result.value is None
    assert _code(result) is EC.UNKNOWN_CURRENCY


def test_currency_allow_list_shape_and_key_members() -> None:
    # every entry is exactly three upper-case ASCII letters
    assert all(len(c) == 3 and c.isascii() and c.isalpha() and c.isupper() for c in APPROVED_CURRENCY_CODES)
    for excluded in ("XAU", "XAG", "XPT", "XPD", "XXX", "XTS", "XDR", "XSU", "XUA", "XBA", "XBB"):
        assert excluded not in APPROVED_CURRENCY_CODES
    for kept in ("EUR", "USD", "GBP", "JPY", "CHF", "XAF", "XCD", "XOF", "XPF"):
        assert kept in APPROVED_CURRENCY_CODES


def test_currency_allow_list_excludes_withdrawn_codes() -> None:
    # BGN/ANG/SLL/ZWL treated as withdrawn (replaced by EUR/XCG/SLE/ZWG) per
    # the vendored snapshot; confirm against the eval set before relying on this.
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


def test_decimal_is_never_rounded_or_quantized() -> None:
    # exact value and scale kept, no minor-unit alignment
    assert str(_val(normalize_money(Decimal("1.005")))) == "1.005"
    assert str(_val(normalize_money(Decimal("2.500")))) == "2.500"
    assert str(_val(normalize_quantity(Decimal("0.333333333333")))) == "0.333333333333"
    assert _val(normalize_money(Decimal("-0"))) == Decimal("0")


def test_string_negation_does_not_apply_decimal_context_rounding() -> None:
    raw_digits = "1234567890123456789012345678901234567890.00"
    with localcontext() as context:
        context.prec = 10
        result = _val(normalize_money(f"-{raw_digits}"))

    assert str(result) == f"-{raw_digits}"


def test_float_is_rejected_never_coerced() -> None:
    # binary floating point never enters the pipeline, even via str()
    assert _code(normalize_money(1234.56)) is EC.INVALID_NUMBER
    assert _code(normalize_quantity(0.1)) is EC.INVALID_NUMBER


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1234.56", Decimal("1234.56")),
        ("1,234.56", Decimal("1234.56")),
        ("1,234", Decimal("1234")),
        ("1,234,567.89", Decimal("1234567.89")),
        ("1 234.56", Decimal("1234.56")),            # ASCII space grouping
        ("1 234 567.89", Decimal("1234567.89")),  # no-break space grouping
        ("1 234.56", Decimal("1234.56")),       # narrow no-break space grouping
        ("$1,234.56", Decimal("1234.56")),
        ("₭1,234.56", Decimal("1234.56")),
        ("1234.56 ₮", Decimal("1234.56")),
        ("₿ 0.125", Decimal("0.125")),
        ("USD 1 234.56", Decimal("1234.56")),
        ("1234.56 EUR", Decimal("1234.56")),
        ("  1234.56  ", Decimal("1234.56")),         # leading/trailing trim
        ("(123.45)", Decimal("-123.45")),
        ("123.45-", Decimal("-123.45")),
        ("-123.45", Decimal("-123.45")),
        ("+99", Decimal("99")),
        ("0", Decimal("0")),
    ],
)
def test_money_string_parsing_follows_the_documented_policy(raw: str, expected: Decimal) -> None:
    assert _val(normalize_money(raw)) == expected


@pytest.mark.parametrize("raw", ["1234,56", "1.234,56", "1 234,56", "12,34"])
def test_money_decimal_comma_is_ambiguous_number(raw: str) -> None:
    assert _code(normalize_money(raw)) is EC.AMBIGUOUS_NUMBER


@pytest.mark.parametrize(
    "raw",
    [
        "1,23,456",      # non-3 grouping
        "1,2345.67",     # wrong group size
        "12.34.56",      # two decimal points
        "1..2",
        "12 34",         # 2-digit "group"
        "1  234.56",     # repeated space is NOT collapsed
        "1\t234.56",     # tab is not a grouping separator
        "1​234.56", # zero-width space inside is not stripped -> malformed
        "1﻿234",    # BOM inside is not stripped -> malformed
        "1,234 567",
        "-123-",
        "(-123)",
        "1e5",           # scientific notation is not a documented format
        ".5",            # bare leading dot
        "5.",            # trailing dot
        "USD",           # a code with no number
        "abc",
    ],
)
def test_money_malformed_string_is_invalid_number(raw: str) -> None:
    assert _code(normalize_money(raw)) is EC.INVALID_NUMBER


@pytest.mark.parametrize("raw", ["", "   ", "\t", " "])
def test_money_blank_string_is_absent_without_error(raw: str) -> None:
    result = normalize_money(raw)
    assert result.value is None and result.error is None


def test_money_and_quantity_share_the_same_rules() -> None:
    for fn in (normalize_money, normalize_quantity):
        assert _val(fn("1 234.5")) == Decimal("1234.5")
        assert _code(fn("1,23,456")) is EC.INVALID_NUMBER
        assert _code(fn("1.234,56")) is EC.AMBIGUOUS_NUMBER


# --- dates -------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # year-first numeric, every accepted separator, 1-2 digit month/day
        ("2026-01-15", "2026-01-15"),
        ("2026/1/5", "2026-01-05"),
        ("2026.01.15", "2026-01-15"),
        ("2026-1-1", "2026-01-01"),
        # English month-name forms
        ("15 Jan 2026", "2026-01-15"),
        ("15 January 2026", "2026-01-15"),
        ("15-Jan-2026", "2026-01-15"),
        ("15 JANUARY 2026", "2026-01-15"),
        ("Jan 15, 2026", "2026-01-15"),
        ("January 15 2026", "2026-01-15"),
        ("5 May 2026", "2026-05-05"),
        # ordinal suffixes, case, trailing period, commas
        ("1st Feb 2026", "2026-02-01"),
        ("15th January 2026.", "2026-01-15"),
        ("AUGUST 1ST, 2026", "2026-08-01"),
        # surrounding / internal whitespace
        ("  2026-01-15  ", "2026-01-15"),
        ("2026-01-15\n", "2026-01-15"),
        ("15 Jan 2026", "2026-01-15"),
        # all-numeric year-last: order resolved by the >12 rule
        ("13/04/2026", "2026-04-13"),
        ("04/13/2026", "2026-04-13"),
        ("15/1/2026", "2026-01-15"),
        ("1/15/2026", "2026-01-15"),
        ("12/13/2026", "2026-12-13"),
        ("13/12/2026", "2026-12-13"),
        # all-numeric year-last: order undetermined -> day-first default
        ("03/04/2026", "2026-04-03"),
        ("4-3-2026", "2026-03-04"),
        ("1.2.2026", "2026-02-01"),
        # real calendar edge: leap day in a leap year, both paths
        ("29/02/2024", "2024-02-29"),
        ("February 29, 2024", "2024-02-29"),
        # no plausibility window - far past / future real dates pass
        ("2099-12-31", "2099-12-31"),
        ("0001-01-01", "0001-01-01"),
    ],
)
def test_date_supported_formats(raw: str, expected: str) -> None:
    assert _val(normalize_date(raw)) == expected


def test_date_ambiguous_numeric_is_read_day_first_not_errored() -> None:
    # 03/04/2026: both components <= 12. Policy fixes this to day-first for
    # every source (not locale inference); it is a value, never an error.
    result = normalize_date("03/04/2026")
    assert result.error is None
    assert result.value == "2026-04-03"


@pytest.mark.parametrize("raw", [None, "", "   ", "\t\n"])
def test_date_absent_is_null_without_error(raw) -> None:
    result = normalize_date(raw)
    assert result.value is None and result.error is None


@pytest.mark.parametrize(
    "raw",
    [
        # impossible calendar dates - numeric paths
        "31/02/2026",        # the canonical example: 31 is the day, Feb has 28
        "2026-02-30",
        "2026-02-31",
        "2026-13-01",        # impossible month
        "2026-00-10",        # zero month
        "2026-01-00",        # zero day
        "00/01/2026",
        "0/0/2026",
        "29/02/2026",        # 2026 is not a leap year
        "2026-02-29",
        # impossible calendar dates - month-name paths
        "Feb 30, 2026",
        "April 31, 2026",
        "February 29, 2026",
        # order genuinely unresolvable
        "13/13/2026",        # both > 12
        # two-digit years - the century is not guessed
        "15/01/26",
        "1/1/26",
        # unrecognized formats
        "20260115",          # no separators
        "2026-01-15T00:00:00",   # datetime, not a date
        "Q1 2026",           # quarter notation
        "3rd Quarter 2026",
        "Wednesday 15 2026",  # weekday
        "Montag 15 2026",    # non-English weekday
        "15 Januar 2026",    # non-English month
        "15 Janvier 2026",
        "Sept 15 2026",      # 4-letter abbreviation (policy: full or 3-letter)
        "15/Jan/2026",       # month name with slash separators
        "Jan. 15, 2026",     # internal period is not stripped
        "2026th-01-15",      # ordinal suffix is on the year, not the day
        "2026-01st-15",      # ordinal suffix is on the month, not the day
        "01st/15/2026",      # 15 resolves as day; suffix is on the month
        "11st Jan 2026",     # malformed ordinal suffix
        "2026-01/15",        # mixed separators
        "15 Jan-2026",       # mixed separators
        "March 2026",        # month + year, no day
        "sometime in 2026",  # free text
        "2026",              # bare year
        "0000-01-01",        # year zero is not a real date
        "٢٠٢٦-٠١-١٥",        # non-ASCII digits are outside the listed formats
        "2026\u200b-01-15",  # date preprocessing does not delete format chars
    ],
)
def test_date_rejected_inputs_are_invalid_date(raw: str) -> None:
    assert _code(normalize_date(raw)) is EC.INVALID_DATE


@pytest.mark.parametrize("raw", [20260115, 20260115.0, ("2026", "01", "15"), b"2026-01-15"])
def test_date_non_string_input_is_a_field_error_not_a_crash(raw) -> None:
    assert _code(normalize_date(raw)) is EC.INVALID_DATE


def test_date_normalizer_never_returns_none() -> None:
    for raw in ("2026-01-15", "31/02/2026", "garbage", "", None, 5):
        assert isinstance(normalize_date(raw), type(normalize_date("2026-01-15")))
