"""Tests for the InvoiceValidation <-> flat-row bridge (Stage 5, step 11)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pytest

from app.schemas.validation import (
    FindingSeverity,
    InvoiceValidation,
    ValidationFinding,
    ValidationRule,
)
from app.schemas.validation_persistence import (
    FINDING_FIELD_NAMES,
    finding_rows,
    invoice_validation_from_rows,
)


@dataclass(frozen=True)
class _Row:
    """A duck-typed stand-in for a ``ValidationFindingRow`` ORM object."""

    position: int
    rule: ValidationRule
    severity: FindingSeverity
    field_path: str | None
    expected: str | None
    actual: str | None
    message: str
    context: dict[str, Any]


def _finding(**overrides) -> ValidationFinding:
    data = {
        "rule": ValidationRule.TOTALS_DO_NOT_RECONCILE,
        "severity": FindingSeverity.WARNING,
        "field_path": None,
        "expected": Decimal("119.00"),
        "actual": Decimal("120.00"),
        "message": "The subtotal plus tax does not equal the invoice total.",
        "context": {"delta": Decimal("-1.00"), "tolerance": Decimal("0.01")},
    }
    data.update(overrides)
    return ValidationFinding.model_validate(data)


def test_finding_field_names_match_the_contract_minus_bookkeeping() -> None:
    assert FINDING_FIELD_NAMES == (
        "rule",
        "severity",
        "field_path",
        "expected",
        "actual",
        "message",
        "context",
    )


def test_finding_rows_assigns_zero_based_contiguous_positions() -> None:
    result = InvoiceValidation.from_findings([_finding(), _finding(field_path="currency")])
    rows = finding_rows(result)
    assert [row["position"] for row in rows] == [0, 1]


def test_finding_rows_stringifies_decimal_expected_actual() -> None:
    (row,) = finding_rows(InvoiceValidation.from_findings([_finding()]))
    assert row["expected"] == "119.00"
    assert row["actual"] == "120.00"
    assert isinstance(row["expected"], str) and isinstance(row["actual"], str)


def test_finding_rows_preserves_null_expected_actual() -> None:
    (row,) = finding_rows(
        InvoiceValidation.from_findings(
            [_finding(rule=ValidationRule.NO_LINE_ITEMS, expected=None, actual=None)]
        )
    )
    assert row["expected"] is None
    assert row["actual"] is None


def test_finding_rows_keeps_context_as_is() -> None:
    context = {"confidence": Decimal("0.4"), "threshold": Decimal("0.70")}
    (row,) = finding_rows(
        InvoiceValidation.from_findings(
            [
                _finding(
                    rule=ValidationRule.LOW_CONFIDENCE_CRITICAL_FIELD,
                    field_path="vendor_name",
                    expected=None,
                    actual=None,
                    context=context,
                )
            ]
        )
    )
    assert row["context"] == context


def test_finding_rows_empty_for_no_findings() -> None:
    assert finding_rows(InvoiceValidation.from_findings([])) == []


def test_round_trip_through_rows_preserves_shape() -> None:
    original = InvoiceValidation.from_findings(
        [
            _finding(),
            _finding(
                rule=ValidationRule.NO_LINE_ITEMS,
                field_path=None,
                expected=None,
                actual=None,
                context={},
            ),
        ]
    )
    rows = [_Row(**row) for row in finding_rows(original)]
    rebuilt = invoice_validation_from_rows(rows)

    assert rebuilt.summary == original.summary
    assert len(rebuilt.findings) == len(original.findings)
    for got, want in zip(rebuilt.findings, original.findings):
        assert got.rule == want.rule
        assert got.severity == want.severity
        assert got.field_path == want.field_path
        assert got.message == want.message
        assert got.context == want.context
    # Decimal expected/actual come back as their display string, not a Decimal -
    # the wire shape (a JSON string) is identical either way.
    assert rebuilt.findings[0].expected == "119.00"
    assert isinstance(rebuilt.findings[0].expected, str)


def test_invoice_validation_from_rows_accepts_mappings_too() -> None:
    rows = finding_rows(InvoiceValidation.from_findings([_finding()]))
    rebuilt = invoice_validation_from_rows(rows)
    assert rebuilt.summary.total == 1


def test_invoice_validation_from_rows_empty() -> None:
    assert invoice_validation_from_rows([]) == InvoiceValidation.from_findings([])


def test_invoice_validation_from_rows_rejects_non_contiguous_positions() -> None:
    rows = finding_rows(InvoiceValidation.from_findings([_finding(), _finding()]))
    rows[1]["position"] = 5
    with pytest.raises(ValueError):
        invoice_validation_from_rows(rows)


def test_invoice_validation_from_rows_sorts_by_position_regardless_of_input_order() -> None:
    original = InvoiceValidation.from_findings(
        [
            _finding(field_path="invoice_number", expected=None, actual=None, context={}),
            _finding(field_path="currency", expected=None, actual=None, context={}),
        ]
    )
    rows = finding_rows(original)
    rebuilt = invoice_validation_from_rows(list(reversed(rows)))
    assert [f.field_path for f in rebuilt.findings] == ["invoice_number", "currency"]
