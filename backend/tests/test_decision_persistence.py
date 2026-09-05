"""Tests for the InvoiceDecision <-> flat-row bridge (Stage 6, package 2)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import pytest

from app.schemas.decision import (
    DecisionOutcome,
    DecisionReason,
    DecisionReasonCode,
    InvoiceDecision,
)
from app.schemas.decision_persistence import (
    REASON_FIELD_NAMES,
    invoice_decision_from_rows,
    reason_rows,
)
from app.schemas.validation import ValidationRule


@dataclass(frozen=True)
class _Row:
    """A duck-typed stand-in for a ``DecisionReasonRow`` ORM object."""

    position: int
    code: DecisionReasonCode
    triggers_review: bool
    source_rule: str | None
    source_finding_id: uuid.UUID | None
    field_path: str | None
    message: str


def _rule_reason(**overrides) -> DecisionReason:
    data = {
        "code": DecisionReasonCode.TOTALS_DO_NOT_RECONCILE,
        "triggers_review": True,
        "source_rule": ValidationRule.TOTALS_DO_NOT_RECONCILE,
        "field_path": None,
        "message": "The subtotal plus tax does not equal the invoice total.",
    }
    data.update(overrides)
    return DecisionReason.model_validate(data)


def _manual_reason() -> DecisionReason:
    return DecisionReason.model_validate(
        {
            "code": "manual_review_requested",
            "triggers_review": True,
            "source_rule": None,
            "field_path": None,
            "message": "A manual review of this invoice was requested.",
        }
    )


def test_reason_field_names_match_the_contract_plus_the_finding_reference() -> None:
    assert REASON_FIELD_NAMES == (
        "code",
        "triggers_review",
        "source_rule",
        "source_finding_id",
        "field_path",
        "message",
    )


def test_reason_rows_assigns_zero_based_contiguous_positions() -> None:
    finding_id = uuid.uuid4()
    decision = InvoiceDecision.from_reasons([_rule_reason(), _manual_reason()])
    rows = reason_rows(decision, [finding_id, None])
    assert [row["position"] for row in rows] == [0, 1]


def test_reason_rows_stores_source_rule_as_its_string_value() -> None:
    finding_id = uuid.uuid4()
    (row,) = reason_rows(InvoiceDecision.from_reasons([_rule_reason()]), [finding_id])
    assert row["source_rule"] == "totals_do_not_reconcile"
    assert isinstance(row["source_rule"], str)
    assert row["source_finding_id"] == finding_id


def test_reason_rows_leaves_manual_reason_source_fields_null() -> None:
    (row,) = reason_rows(InvoiceDecision.from_reasons([_manual_reason()]), [None])
    assert row["source_rule"] is None
    assert row["source_finding_id"] is None


def test_reason_rows_empty_for_no_reasons() -> None:
    assert reason_rows(InvoiceDecision.from_reasons([]), []) == []


def test_reason_rows_rejects_mismatched_length() -> None:
    decision = InvoiceDecision.from_reasons([_rule_reason(), _manual_reason()])
    with pytest.raises(ValueError):
        reason_rows(decision, [uuid.uuid4()])


def test_reason_rows_rejects_a_finding_id_for_a_manual_reason() -> None:
    decision = InvoiceDecision.from_reasons([_manual_reason()])
    with pytest.raises(ValueError):
        reason_rows(decision, [uuid.uuid4()])


def test_reason_rows_rejects_a_missing_finding_id_for_a_rule_reason() -> None:
    decision = InvoiceDecision.from_reasons([_rule_reason()])
    with pytest.raises(ValueError):
        reason_rows(decision, [None])


def test_round_trip_through_rows_preserves_shape() -> None:
    finding_id = uuid.uuid4()
    original = InvoiceDecision.from_reasons(
        [
            _rule_reason(),
            _rule_reason(
                code="no_line_items",
                source_rule="no_line_items",
                triggers_review=False,
                field_path=None,
                message="The invoice has no line items.",
            ),
            _manual_reason(),
        ]
    )
    rows = [_Row(**row) for row in reason_rows(original, [finding_id, uuid.uuid4(), None])]
    rebuilt = invoice_decision_from_rows(rows)

    assert rebuilt.outcome == original.outcome
    assert len(rebuilt.reasons) == len(original.reasons)
    for got, want in zip(rebuilt.reasons, original.reasons):
        assert got.code == want.code
        assert got.triggers_review == want.triggers_review
        assert got.source_rule == want.source_rule
        assert got.field_path == want.field_path
        assert got.message == want.message


def test_invoice_decision_from_rows_accepts_mappings_too() -> None:
    finding_id = uuid.uuid4()
    rows = reason_rows(InvoiceDecision.from_reasons([_rule_reason()]), [finding_id])
    rebuilt = invoice_decision_from_rows(rows)
    assert rebuilt.outcome is DecisionOutcome.NEEDS_REVIEW
    assert len(rebuilt.reasons) == 1


def test_invoice_decision_from_rows_empty_is_accepted() -> None:
    assert invoice_decision_from_rows([]) == InvoiceDecision.from_reasons([])


def test_invoice_decision_from_rows_rejects_non_contiguous_positions() -> None:
    rows = reason_rows(
        InvoiceDecision.from_reasons([_rule_reason(), _manual_reason()]),
        [uuid.uuid4(), None],
    )
    rows[1]["position"] = 5
    with pytest.raises(ValueError):
        invoice_decision_from_rows(rows)


def test_invoice_decision_from_rows_sorts_by_position_regardless_of_input_order() -> None:
    original = InvoiceDecision.from_reasons(
        [
            _rule_reason(field_path="invoice_number", code="missing_required_field", source_rule="missing_required_field"),
            _rule_reason(field_path="currency", code="missing_required_field", source_rule="missing_required_field"),
        ]
    )
    rows = reason_rows(original, [uuid.uuid4(), uuid.uuid4()])
    rebuilt = invoice_decision_from_rows(list(reversed(rows)))
    assert [r.field_path for r in rebuilt.reasons] == ["invoice_number", "currency"]


def test_invoice_decision_from_rows_never_discards_a_non_gating_reason() -> None:
    finding_id = uuid.uuid4()
    non_gating = _rule_reason(
        code="no_line_items",
        source_rule="no_line_items",
        triggers_review=False,
        field_path=None,
        message="The invoice has no line items.",
    )
    rows = reason_rows(InvoiceDecision.from_reasons([non_gating]), [finding_id])
    rebuilt = invoice_decision_from_rows(rows)
    assert rebuilt.outcome is DecisionOutcome.ACCEPTED
    assert len(rebuilt.reasons) == 1
