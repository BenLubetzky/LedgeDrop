"""Pinned Stage 5 validation policy (step 8).

Every ⚠ judgement-call constant from ``docs/stage-5-validation.md`` Part 2 lives
here and nowhere else. This is a **vendored** module, not environment
configuration (spec §2.9): a validation run must be reproducible from the code
alone, and each finding records the constant it applied. The values are
deliberately conservative defaults pending review against real invoice data
(spec "Open questions" 1-8); retuning one is a change to this file plus its
tests, never a runtime setting.

No AI, no I/O, no network. ``Decimal`` only - no binary float anywhere.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from decimal import Decimal
from types import MappingProxyType
from typing import Final

from app.schemas.extraction import CRITICAL_FIELDS as _EXTRACTION_CRITICAL_FIELDS
from app.schemas.normalization import NORMALIZED_SCALAR_FIELD_NAMES

__all__ = [
    "REQUIRED_FIELDS",
    "CRITICAL_FIELDS",
    "RECONCILIATION_TOLERANCE",
    "LINE_SUM_TOLERANCE_PER_LINE",
    "line_sum_tolerance",
    "DUE_DATE_MAX_GAP_DAYS",
    "INVOICE_DATE_MAX_AGE_YEARS",
    "earliest_plausible_invoice_date",
    "CRITICAL_FIELD_CONFIDENCE_MIN",
    "HIGH_VALUE_THRESHOLDS",
    "HIGH_VALUE_DEFAULT_THRESHOLD",
    "high_value_threshold",
]


# --- §2.1 required fields ------------------------------------------------
# The same set as ``app.schemas.extraction.CRITICAL_FIELDS``, ordered to match
# the canonical scalar order so ``missing_required_field`` findings come out in
# a stable sequence. ⚠ open question 1: is this the right required set?
REQUIRED_FIELDS: Final[tuple[str, ...]] = tuple(
    name
    for name in NORMALIZED_SCALAR_FIELD_NAMES
    if name in _EXTRACTION_CRITICAL_FIELDS
)
# §2.6 evaluates per-field confidence for "the five required/critical fields" -
# exactly this set.
CRITICAL_FIELDS: Final[tuple[str, ...]] = REQUIRED_FIELDS


# --- §2.3 / §2.4 / §2.5 monetary reconciliation tolerance --------------
# Absolute, in the invoice's own currency. ⚠ open question 2.
RECONCILIATION_TOLERANCE: Final[Decimal] = Decimal("0.01")


# --- §2.4 line-sum tolerance grows one minor unit per line ------------
LINE_SUM_TOLERANCE_PER_LINE: Final[Decimal] = Decimal("0.01")


def line_sum_tolerance(line_count: int) -> Decimal:
    """``max(0.01, 0.01 × line_count)`` - the §2.4 per-line growth. ⚠ oq 3."""
    if line_count < 0:
        raise ValueError("line_count must not be negative")
    return max(RECONCILIATION_TOLERANCE, LINE_SUM_TOLERANCE_PER_LINE * line_count)


# --- §2.2 date windows ----------------------------------------------
DUE_DATE_MAX_GAP_DAYS: Final[int] = 365  # ⚠ open question 5
INVOICE_DATE_MAX_AGE_YEARS: Final[int] = 10  # ⚠ open question 5


def earliest_plausible_invoice_date(run_date: date) -> date:
    """``run_date`` minus :data:`INVOICE_DATE_MAX_AGE_YEARS` calendar years.

    A 29 February run date maps to 28 February that many years earlier.
    """
    year = run_date.year - INVOICE_DATE_MAX_AGE_YEARS
    try:
        return run_date.replace(year=year)
    except ValueError:  # 29 February -> 28 February
        return date(year, 2, 28)


# --- §2.6 confidence threshold -------------------------------------
CRITICAL_FIELD_CONFIDENCE_MIN: Final[Decimal] = Decimal("0.70")  # ⚠ open question 6


# --- §2.7 high-value thresholds (invoice-total major units) -------
# ⚠ open question 7: the magnitudes are placeholders; set them from the real
# invoice distribution. ``abs(total_amount)`` is compared, so a large credit
# note is flagged for the same attention as a large charge.
HIGH_VALUE_THRESHOLDS: Final[Mapping[str, Decimal]] = MappingProxyType(
    {
        "EUR": Decimal("10000"),
        "USD": Decimal("10000"),
        "GBP": Decimal("10000"),
        "CHF": Decimal("10000"),
        "JPY": Decimal("1500000"),
    }
)
HIGH_VALUE_DEFAULT_THRESHOLD: Final[Decimal] = Decimal("10000")


def high_value_threshold(currency: str) -> Decimal:
    """The high-value threshold for ``currency`` (an upper-case ISO code).

    Falls back to :data:`HIGH_VALUE_DEFAULT_THRESHOLD` for any approved code not
    in :data:`HIGH_VALUE_THRESHOLDS`.
    """
    return HIGH_VALUE_THRESHOLDS.get(currency, HIGH_VALUE_DEFAULT_THRESHOLD)
