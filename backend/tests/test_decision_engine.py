"""Tests for the Stage 6 deterministic decision engine (package 3).

No database and no AI provider - :func:`decide` is a pure function of an
``InvoiceValidation`` and a boolean, checked here against the full Part 2
policy table (mirroring ``tests/test_decision_catalogue.py``'s independent
transcription) end to end, plus determinism, no-mutation, and the 1:1
finding <-> reason correspondence later packages rely on.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.schemas.decision import DecisionOutcome, DecisionReasonCode
from app.schemas.decision_catalogue import MANUAL_REVIEW_MESSAGE
from app.schemas.validation import (
    FindingSeverity,
    InvoiceValidation,
    ValidationFinding,
    ValidationRule,
)
from app.schemas.validation_catalogue import RULE_MESSAGES
from app.services.processing.decision.engine import (
    decide,
    manual_review_reason,
    reason_for_finding,
)

# Independent transcription of Part 2 §2.2, matching
# tests/test_decision_catalogue.py's _EXPECTED_TRIGGERS_REVIEW - a bug that
# swapped engine wiring should be caught here even if the catalogue itself
# were (wrongly) internally consistent.
_EXPECTED_TRIGGERS_REVIEW: dict[ValidationRule, bool] = {
    ValidationRule.MISSING_REQUIRED_FIELD: True,
    ValidationRule.NORMALIZATION_ERROR: True,
    ValidationRule.DUE_DATE_BEFORE_INVOICE_DATE: True,
    ValidationRule.DUE_DATE_FAR_AFTER_INVOICE_DATE: True,
    ValidationRule.INVOICE_DATE_IN_FUTURE: True,
    ValidationRule.INVOICE_DATE_IMPLAUSIBLY_OLD: True,
    ValidationRule.TOTALS_DO_NOT_RECONCILE: True,
    ValidationRule.LINE_ITEM_AMOUNT_MISMATCH: True,
    ValidationRule.LINE_ITEMS_DO_NOT_SUM: True,
    ValidationRule.LINE_ITEM_SUM_NOT_CHECKED: False,
    ValidationRule.LOW_CONFIDENCE_CRITICAL_FIELD: True,
    ValidationRule.CRITICAL_FIELD_CONFIDENCE_UNAVAILABLE: False,
    ValidationRule.PROBABLE_DUPLICATE_INVOICE: True,
    ValidationRule.HIGH_VALUE_INVOICE: True,
    ValidationRule.NO_LINE_ITEMS: False,
}


def _finding(rule: ValidationRule, **overrides) -> ValidationFinding:
    data = {
        "rule": rule,
        "severity": FindingSeverity.WARNING,
        "field_path": None,
        "expected": None,
        "actual": None,
        "message": RULE_MESSAGES[rule],
        "context": {},
    }
    data.update(overrides)
    return ValidationFinding.model_validate(data)


# --- clean invoice -----------------------------------------------------


def test_clean_invoice_with_no_findings_and_no_manual_request_is_accepted() -> None:
    result = decide(InvoiceValidation.from_findings([]))
    assert result.outcome is DecisionOutcome.ACCEPTED
    assert result.reasons == []


# --- the full rule -> reason matrix, end to end -------------------------


@pytest.mark.parametrize("rule", list(ValidationRule), ids=lambda r: r.value)
def test_a_single_finding_decides_per_the_policy_table(rule: ValidationRule) -> None:
    finding = _finding(rule, field_path=None)
    result = decide(InvoiceValidation.from_findings([finding]))

    expected_gates = _EXPECTED_TRIGGERS_REVIEW[rule]
    assert len(result.reasons) == 1
    reason = result.reasons[0]
    assert reason.code is DecisionReasonCode(rule.value)
    assert reason.triggers_review is expected_gates
    assert reason.source_rule is rule
    assert reason.message == RULE_MESSAGES[rule]
    assert (
        result.outcome is DecisionOutcome.NEEDS_REVIEW
        if expected_gates
        else result.outcome is DecisionOutcome.ACCEPTED
    )


# --- field_path and message pass-through / independence ----------------


def test_field_path_is_carried_through_unchanged() -> None:
    finding = _finding(ValidationRule.LOW_CONFIDENCE_CRITICAL_FIELD, field_path="vendor_name")
    (reason,) = decide(InvoiceValidation.from_findings([finding])).reasons
    assert reason.field_path == "vendor_name"


def test_invoice_level_finding_has_a_null_field_path() -> None:
    finding = _finding(ValidationRule.TOTALS_DO_NOT_RECONCILE, field_path=None)
    (reason,) = decide(InvoiceValidation.from_findings([finding])).reasons
    assert reason.field_path is None


def test_reason_message_comes_from_the_policy_not_the_finding() -> None:
    # Even if a finding somehow carried different text, the decision reason
    # must still read exactly like the fixed Part 2 policy sentence.
    finding = _finding(
        ValidationRule.NO_LINE_ITEMS, message="a completely different sentence"
    )
    (reason,) = decide(InvoiceValidation.from_findings([finding])).reasons
    assert reason.message == RULE_MESSAGES[ValidationRule.NO_LINE_ITEMS]
    assert reason.message != "a completely different sentence"


def test_engine_ignores_severity_expected_actual_and_context() -> None:
    finding = _finding(
        ValidationRule.TOTALS_DO_NOT_RECONCILE,
        severity=FindingSeverity.WARNING,
        expected=Decimal("119.00"),
        actual=Decimal("120.00"),
        context={"delta": Decimal("-1.00"), "tolerance": Decimal("0.01")},
    )
    (reason,) = decide(InvoiceValidation.from_findings([finding])).reasons
    assert not hasattr(reason, "severity")
    assert not hasattr(reason, "expected")
    assert not hasattr(reason, "context")


# --- conflicting / mixed findings ---------------------------------------


def test_conflicting_findings_all_survive_and_any_gate_forces_review() -> None:
    findings = [
        _finding(ValidationRule.NO_LINE_ITEMS),  # non-gating
        _finding(ValidationRule.HIGH_VALUE_INVOICE),  # gating
        _finding(ValidationRule.CRITICAL_FIELD_CONFIDENCE_UNAVAILABLE, field_path="total_amount"),  # non-gating
    ]
    result = decide(InvoiceValidation.from_findings(findings))
    assert result.outcome is DecisionOutcome.NEEDS_REVIEW
    assert len(result.reasons) == 3
    assert [r.code for r in result.reasons] == [
        DecisionReasonCode.NO_LINE_ITEMS,
        DecisionReasonCode.HIGH_VALUE_INVOICE,
        DecisionReasonCode.CRITICAL_FIELD_CONFIDENCE_UNAVAILABLE,
    ]
    # order preserved exactly, no reordering by severity or "worst first"
    assert [r.triggers_review for r in result.reasons] == [False, True, False]


def test_only_non_gating_findings_are_accepted_but_reasons_are_kept() -> None:
    findings = [
        _finding(ValidationRule.NO_LINE_ITEMS),
        _finding(ValidationRule.CRITICAL_FIELD_CONFIDENCE_UNAVAILABLE, field_path="invoice_number"),
        _finding(ValidationRule.LINE_ITEM_SUM_NOT_CHECKED),
    ]
    result = decide(InvoiceValidation.from_findings(findings))
    assert result.outcome is DecisionOutcome.ACCEPTED
    assert len(result.reasons) == 3
    assert all(not r.triggers_review for r in result.reasons)


# --- duplicate / high-value (explicit business-risk elevations) --------


def test_probable_duplicate_invoice_forces_review() -> None:
    finding = _finding(ValidationRule.PROBABLE_DUPLICATE_INVOICE)
    result = decide(InvoiceValidation.from_findings([finding]))
    assert result.outcome is DecisionOutcome.NEEDS_REVIEW
    assert result.reasons[0].triggers_review is True


def test_high_value_invoice_forces_review_despite_stage5_info_severity() -> None:
    finding = _finding(ValidationRule.HIGH_VALUE_INVOICE, severity=FindingSeverity.INFO)
    result = decide(InvoiceValidation.from_findings([finding]))
    assert result.outcome is DecisionOutcome.NEEDS_REVIEW
    assert result.reasons[0].triggers_review is True


# --- missing / low confidence --------------------------------------------


def test_low_confidence_critical_field_forces_review() -> None:
    finding = _finding(ValidationRule.LOW_CONFIDENCE_CRITICAL_FIELD, field_path="total_amount")
    result = decide(InvoiceValidation.from_findings([finding]))
    assert result.outcome is DecisionOutcome.NEEDS_REVIEW


def test_unavailable_confidence_alone_does_not_force_review() -> None:
    finding = _finding(
        ValidationRule.CRITICAL_FIELD_CONFIDENCE_UNAVAILABLE, field_path="vendor_name"
    )
    result = decide(InvoiceValidation.from_findings([finding]))
    assert result.outcome is DecisionOutcome.ACCEPTED
    assert result.reasons[0].triggers_review is False
    assert result.reasons[0].code is DecisionReasonCode.CRITICAL_FIELD_CONFIDENCE_UNAVAILABLE


def test_unavailable_confidence_on_every_critical_field_still_accepts_a_clean_invoice() -> None:
    # The realistic OpenAI/GPT-5-mini shape: every critical field present,
    # none with confidence - must not defeat automatic acceptance.
    findings = [
        _finding(ValidationRule.CRITICAL_FIELD_CONFIDENCE_UNAVAILABLE, field_path=field)
        for field in ("invoice_number", "invoice_date", "vendor_name", "currency", "total_amount")
    ]
    result = decide(InvoiceValidation.from_findings(findings))
    assert result.outcome is DecisionOutcome.ACCEPTED
    assert len(result.reasons) == 5


# --- manual review requests ----------------------------------------------


def test_manual_review_alone_forces_review_with_one_reason() -> None:
    result = decide(InvoiceValidation.from_findings([]), manual_review_requested=True)
    assert result.outcome is DecisionOutcome.NEEDS_REVIEW
    assert len(result.reasons) == 1
    reason = result.reasons[0]
    assert reason.code is DecisionReasonCode.MANUAL_REVIEW_REQUESTED
    assert reason.triggers_review is True
    assert reason.source_rule is None
    assert reason.field_path is None
    assert reason.message == MANUAL_REVIEW_MESSAGE


def test_manual_review_is_appended_last_after_rule_derived_reasons() -> None:
    findings = [_finding(ValidationRule.NO_LINE_ITEMS), _finding(ValidationRule.HIGH_VALUE_INVOICE)]
    result = decide(InvoiceValidation.from_findings(findings), manual_review_requested=True)
    assert [r.code for r in result.reasons] == [
        DecisionReasonCode.NO_LINE_ITEMS,
        DecisionReasonCode.HIGH_VALUE_INVOICE,
        DecisionReasonCode.MANUAL_REVIEW_REQUESTED,
    ]


def test_manual_review_defaults_to_not_requested() -> None:
    result = decide(InvoiceValidation.from_findings([]))
    assert result.outcome is DecisionOutcome.ACCEPTED
    assert result.reasons == []


def test_manual_review_false_is_the_same_as_omitting_it() -> None:
    findings = [_finding(ValidationRule.NO_LINE_ITEMS)]
    with_default = decide(InvoiceValidation.from_findings(findings))
    with_explicit_false = decide(
        InvoiceValidation.from_findings(findings), manual_review_requested=False
    )
    assert with_default == with_explicit_false


@pytest.mark.parametrize("invalid", [0, 1, "", "false", None])
def test_manual_review_flag_is_strictly_boolean(invalid: object) -> None:
    with pytest.raises(TypeError, match="manual_review_requested must be a bool"):
        decide(InvoiceValidation.from_findings([]), manual_review_requested=invalid)  # type: ignore[arg-type]


# --- the 1:1, order-preserving correspondence ---------------------------


def test_every_finding_produces_exactly_one_reason_in_order() -> None:
    findings = [
        _finding(ValidationRule.MISSING_REQUIRED_FIELD, field_path="currency"),
        _finding(ValidationRule.NO_LINE_ITEMS),
        _finding(ValidationRule.PROBABLE_DUPLICATE_INVOICE),
    ]
    result = decide(InvoiceValidation.from_findings(findings))
    assert len(result.reasons) == len(findings)
    for finding, reason in zip(findings, result.reasons):
        assert reason.source_rule is finding.rule
        assert reason.field_path == finding.field_path


def test_reason_for_finding_matches_decides_per_finding_reason() -> None:
    finding = _finding(ValidationRule.TOTALS_DO_NOT_RECONCILE)
    direct = reason_for_finding(finding)
    (via_decide,) = decide(InvoiceValidation.from_findings([finding])).reasons
    assert direct == via_decide


def test_manual_review_reason_matches_decides_manual_reason() -> None:
    direct = manual_review_reason()
    (via_decide,) = decide(
        InvoiceValidation.from_findings([]), manual_review_requested=True
    ).reasons
    assert direct == via_decide


# --- determinism and no mutation -----------------------------------------


def test_decide_is_deterministic_across_equal_but_distinct_inputs() -> None:
    findings_a = [_finding(ValidationRule.HIGH_VALUE_INVOICE)]
    findings_b = [_finding(ValidationRule.HIGH_VALUE_INVOICE)]
    assert decide(InvoiceValidation.from_findings(findings_a)) == decide(
        InvoiceValidation.from_findings(findings_b)
    )


def test_decide_does_not_mutate_its_input() -> None:
    findings = [_finding(ValidationRule.NO_LINE_ITEMS), _finding(ValidationRule.HIGH_VALUE_INVOICE)]
    validation = InvoiceValidation.from_findings(findings)
    before = validation.model_copy(deep=True)

    decide(validation, manual_review_requested=True)

    assert validation == before


def test_repeated_calls_return_equal_results() -> None:
    findings = [_finding(ValidationRule.LOW_CONFIDENCE_CRITICAL_FIELD, field_path="total_amount")]
    validation = InvoiceValidation.from_findings(findings)
    first = decide(validation)
    second = decide(validation)
    assert first == second
    assert first is not second
