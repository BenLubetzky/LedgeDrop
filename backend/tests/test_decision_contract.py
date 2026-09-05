"""Tests for the internal invoice-decision data contract (Stage 6, package 1).

No database and no AI provider are involved - these exercise pure schema
behaviour and the boundary guarantees from ``docs/stage-6-decision.md`` §1.
"""

from __future__ import annotations

import json
import uuid

import pytest
from pydantic import ValidationError

from app.schemas.decision import (
    DECISION_REASON_CODES,
    DECISION_REASON_FIELD_NAMES,
    DecidedInvoiceResult,
    DecisionOutcome,
    DecisionReason,
    DecisionReasonCode,
    DecisionStatus,
    InvoiceDecision,
)
from app.schemas.validation import ValidationRule


def _reason(**overrides) -> dict:
    data = {
        "code": "totals_do_not_reconcile",
        "triggers_review": True,
        "source_rule": "totals_do_not_reconcile",
        "field_path": None,
        "message": "The subtotal plus tax does not equal the invoice total.",
    }
    data.update(overrides)
    return data


# --- enums are closed and exactly as specified --------------------------


def test_decision_status_members_are_exactly_three() -> None:
    assert {s.value for s in DecisionStatus} == {"PROCESSING", "COMPLETED", "FAILED"}


def test_decision_outcome_members_are_exactly_two() -> None:
    assert {o.value for o in DecisionOutcome} == {"ACCEPTED", "NEEDS_REVIEW"}


def test_no_approval_or_rejection_vocabulary_leaks_into_outcome() -> None:
    forbidden = {"REJECTED", "APPROVED", "DENIED", "ESCALATED"}
    assert forbidden.isdisjoint({o.value for o in DecisionOutcome})


def test_decision_reason_catalogue_is_closed_and_complete() -> None:
    expected_rule_codes = {rule.value for rule in ValidationRule}
    assert expected_rule_codes <= set(DECISION_REASON_CODES)
    assert set(DECISION_REASON_CODES) - expected_rule_codes == {"manual_review_requested"}
    assert len(DECISION_REASON_CODES) == len(set(DECISION_REASON_CODES)) == 16


def test_unknown_reason_code_is_rejected() -> None:
    with pytest.raises(ValidationError):
        DecisionReason.model_validate(_reason(code="looks_fine_but_fake"))


# --- source_rule must agree with code --------------------------------


def test_manual_review_reason_has_no_source_rule() -> None:
    reason = DecisionReason.model_validate(
        _reason(
            code="manual_review_requested",
            source_rule=None,
            field_path=None,
            message="A manual review of this invoice was requested.",
        )
    )
    assert reason.source_rule is None


def test_manual_review_reason_rejects_a_source_rule() -> None:
    with pytest.raises(ValidationError):
        DecisionReason.model_validate(
            _reason(code="manual_review_requested", source_rule="totals_do_not_reconcile")
        )


def test_rule_derived_reason_requires_a_matching_source_rule() -> None:
    with pytest.raises(ValidationError):
        DecisionReason.model_validate(_reason(source_rule=None))
    with pytest.raises(ValidationError):
        DecisionReason.model_validate(
            _reason(code="totals_do_not_reconcile", source_rule="no_line_items")
        )


# --- field_path reuses the Stage 4 / Stage 5 vocabulary ---------------


@pytest.mark.parametrize(
    "path",
    [None, "invoice_number", "total_amount", "line_items.0.line_total"],
)
def test_field_path_accepts_stage4_paths(path: str | None) -> None:
    assert DecisionReason.model_validate(_reason(field_path=path)).field_path == path


@pytest.mark.parametrize(
    "path",
    ["not_a_field", "line_items", "line_items.01.line_total", "LINE_ITEMS.0.line_total", ""],
)
def test_field_path_rejects_malformed_paths(path: str) -> None:
    with pytest.raises(ValidationError):
        DecisionReason.model_validate(_reason(field_path=path))


# --- message and strictness -----------------------------------------


@pytest.mark.parametrize("message", ["", "   ", "\t\n"])
def test_message_must_be_non_blank(message: str) -> None:
    with pytest.raises(ValidationError):
        DecisionReason.model_validate(_reason(message=message))


def test_triggers_review_is_a_strict_bool() -> None:
    with pytest.raises(ValidationError):
        DecisionReason.model_validate(_reason(triggers_review="true"))


def test_reason_field_set_is_exactly_the_contract() -> None:
    assert set(DECISION_REASON_FIELD_NAMES) == {
        "code",
        "triggers_review",
        "source_rule",
        "field_path",
        "message",
    }


def test_reason_rejects_unknown_keys() -> None:
    with pytest.raises(ValidationError):
        DecisionReason.model_validate(_reason(severity="warning"))


def test_reason_requires_every_field_present() -> None:
    incomplete = _reason()
    del incomplete["field_path"]
    with pytest.raises(ValidationError):
        DecisionReason.model_validate(incomplete)


# --- InvoiceDecision ties the outcome to the reasons -----------------


def test_invoice_decision_accepts_empty_reasons_as_accepted() -> None:
    payload = InvoiceDecision.from_reasons([])
    assert payload.reasons == []
    assert payload.outcome is DecisionOutcome.ACCEPTED


def test_invoice_decision_is_accepted_when_no_reason_triggers_review() -> None:
    non_gating = [
        DecisionReason.model_validate(
            _reason(
                code="no_line_items",
                triggers_review=False,
                source_rule="no_line_items",
                field_path=None,
                message="The invoice has no line items.",
            )
        )
    ]
    payload = InvoiceDecision.from_reasons(non_gating)
    assert payload.outcome is DecisionOutcome.ACCEPTED
    # the non-gating reason is preserved, not discarded, even though it did
    # not change the outcome
    assert payload.reasons == non_gating


def test_invoice_decision_needs_review_when_any_reason_triggers_review() -> None:
    mixed = [
        DecisionReason.model_validate(
            _reason(
                code="no_line_items",
                triggers_review=False,
                source_rule="no_line_items",
                field_path=None,
                message="The invoice has no line items.",
            )
        ),
        DecisionReason.model_validate(_reason()),
    ]
    payload = InvoiceDecision.from_reasons(mixed)
    assert payload.outcome is DecisionOutcome.NEEDS_REVIEW
    assert payload.reasons == mixed  # order preserved, nothing dropped


def test_invoice_decision_rejects_outcome_that_disagrees_with_reasons() -> None:
    with pytest.raises(ValidationError):
        InvoiceDecision(outcome=DecisionOutcome.ACCEPTED, reasons=[DecisionReason.model_validate(_reason())])
    with pytest.raises(ValidationError):
        InvoiceDecision(outcome=DecisionOutcome.NEEDS_REVIEW, reasons=[])


def test_invoice_decision_json_shape_is_stable() -> None:
    payload = InvoiceDecision.from_reasons([DecisionReason.model_validate(_reason())])
    once = payload.model_dump_json()
    twice = InvoiceDecision.model_validate_json(once).model_dump_json()
    assert once == twice
    assert json.loads(once)["outcome"] == "NEEDS_REVIEW"


def test_invoice_decision_rejects_unknown_keys() -> None:
    with pytest.raises(ValidationError):
        InvoiceDecision.model_validate({"outcome": "ACCEPTED", "reasons": [], "accepted": True})


# --- the result binds to the source validation attempt ---------------


def test_result_binds_to_source_validation_id() -> None:
    vid = uuid.uuid4()
    result = DecidedInvoiceResult(
        source_validation_id=vid,
        decision=InvoiceDecision.from_reasons([]),
    )
    assert result.source_validation_id == vid


def test_result_rejects_unknown_keys() -> None:
    with pytest.raises(ValidationError):
        DecidedInvoiceResult.model_validate(
            {
                "source_validation_id": str(uuid.uuid4()),
                "decision": InvoiceDecision.from_reasons([]).model_dump(),
                "document_id": str(uuid.uuid4()),
            }
        )


# --- every DecisionReasonCode round-trips through the enum -----------


@pytest.mark.parametrize("code", list(DecisionReasonCode), ids=lambda c: c.value)
def test_every_reason_code_is_a_valid_string_enum_member(code: DecisionReasonCode) -> None:
    assert DecisionReasonCode(code.value) is code
