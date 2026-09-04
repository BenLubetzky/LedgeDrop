"""Tests for the pinned Stage 5 validation policy (step 8)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.schemas.extraction import CRITICAL_FIELDS
from app.schemas.normalization import NORMALIZED_SCALAR_FIELD_NAMES
from app.services.processing.validation import policy


def test_required_fields_are_the_extraction_critical_set_in_canonical_order() -> None:
    assert set(policy.REQUIRED_FIELDS) == set(CRITICAL_FIELDS)
    assert policy.REQUIRED_FIELDS == tuple(
        n for n in NORMALIZED_SCALAR_FIELD_NAMES if n in CRITICAL_FIELDS
    )
    assert policy.REQUIRED_FIELDS == (
        "invoice_number",
        "invoice_date",
        "vendor_name",
        "currency",
        "total_amount",
    )
    assert policy.CRITICAL_FIELDS is policy.REQUIRED_FIELDS


def test_tolerances_are_positive_decimals() -> None:
    for value in (
        policy.RECONCILIATION_TOLERANCE,
        policy.LINE_SUM_TOLERANCE_PER_LINE,
        policy.CRITICAL_FIELD_CONFIDENCE_MIN,
        policy.HIGH_VALUE_DEFAULT_THRESHOLD,
    ):
        assert isinstance(value, Decimal)
        assert value > 0
    assert policy.RECONCILIATION_TOLERANCE == Decimal("0.01")
    assert policy.CRITICAL_FIELD_CONFIDENCE_MIN == Decimal("0.70")


@pytest.mark.parametrize(
    ("count", "expected"),
    [(0, "0.01"), (1, "0.01"), (2, "0.02"), (5, "0.05"), (37, "0.37")],
)
def test_line_sum_tolerance_grows_one_minor_unit_per_line(
    count: int, expected: str
) -> None:
    result = policy.line_sum_tolerance(count)
    assert result == Decimal(expected)
    assert isinstance(result, Decimal)


def test_line_sum_tolerance_rejects_negative_count() -> None:
    with pytest.raises(ValueError):
        policy.line_sum_tolerance(-1)


def test_date_windows_match_the_spec() -> None:
    assert policy.DUE_DATE_MAX_GAP_DAYS == 365
    assert policy.INVOICE_DATE_MAX_AGE_YEARS == 10


@pytest.mark.parametrize(
    ("run", "earliest"),
    [
        (date(2026, 6, 1), date(2016, 6, 1)),
        (date(2026, 1, 31), date(2016, 1, 31)),
        (date(2024, 2, 29), date(2014, 2, 28)),  # leap day -> 28 Feb
    ],
)
def test_earliest_plausible_invoice_date(run: date, earliest: date) -> None:
    assert policy.earliest_plausible_invoice_date(run) == earliest


@pytest.mark.parametrize(
    ("currency", "threshold"),
    [
        ("EUR", "10000"),
        ("USD", "10000"),
        ("GBP", "10000"),
        ("CHF", "10000"),
        ("JPY", "1500000"),
        ("SEK", "10000"),  # approved code not in the map -> default
        ("ZZZ", "10000"),
    ],
)
def test_high_value_threshold(currency: str, threshold: str) -> None:
    result = policy.high_value_threshold(currency)
    assert result == Decimal(threshold)
    assert isinstance(result, Decimal)


def test_high_value_map_is_immutable_and_decimal_only() -> None:
    with pytest.raises(TypeError):
        policy.HIGH_VALUE_THRESHOLDS["EUR"] = Decimal("1")  # type: ignore[index]
    assert all(isinstance(v, Decimal) for v in policy.HIGH_VALUE_THRESHOLDS.values())


def test_no_binary_float_in_any_policy_value() -> None:
    for name in policy.__all__:
        value = getattr(policy, name)
        if callable(value):
            continue
        candidates = value.values() if hasattr(value, "values") else [value]
        for item in candidates:
            assert not isinstance(item, float), name
