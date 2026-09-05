"""Tests for the formal Stage 6 decision-reason catalogue (package 1).

No database and no AI provider - these exercise the static policy table in
``app/schemas/decision_catalogue.py`` and check it stays faithful to
``docs/stage-6-decision.md`` Part 2.
"""

from __future__ import annotations

import re

import pytest

from app.schemas.decision import DecisionReason, DecisionReasonCode
from app.schemas.decision_catalogue import (
    MANUAL_REVIEW_MESSAGE,
    REASON_POLICIES,
    REASON_POLICY_BY_CODE,
    REASON_POLICY_BY_RULE,
    ReasonPolicy,
    manual_review_policy,
    policy_for_rule,
)
from app.schemas.validation import ValidationRule
from app.schemas.validation_catalogue import RULE_MESSAGES

# --- Part 2 table, transcribed for cross-checking ----------------------

# Whether a finding for this rule, by itself, requires human review.
_EXPECTED_TRIGGERS_REVIEW: dict[str, bool] = {
    "missing_required_field": True,
    "normalization_error": True,
    "due_date_before_invoice_date": True,
    "due_date_far_after_invoice_date": True,
    "invoice_date_in_future": True,
    "invoice_date_implausibly_old": True,
    "totals_do_not_reconcile": True,
    "line_item_amount_mismatch": True,
    "line_items_do_not_sum": True,
    "line_item_sum_not_checked": False,
    "low_confidence_critical_field": True,
    "critical_field_confidence_unavailable": False,
    "probable_duplicate_invoice": True,
    "high_value_invoice": True,
    "no_line_items": False,
}

_NON_GATING_RULES = {
    ValidationRule.LINE_ITEM_SUM_NOT_CHECKED,
    ValidationRule.CRITICAL_FIELD_CONFIDENCE_UNAVAILABLE,
    ValidationRule.NO_LINE_ITEMS,
}


# --- the catalogue is closed and matches both enums -------------------


def test_catalogue_has_exactly_one_entry_per_decision_reason_code() -> None:
    assert tuple(policy.code for policy in REASON_POLICIES) == tuple(DecisionReasonCode)
    assert set(REASON_POLICY_BY_CODE) == set(DecisionReasonCode)
    assert len(REASON_POLICIES) == len(DecisionReasonCode) == 16


def test_catalogue_covers_every_validation_rule_exactly_once() -> None:
    assert set(REASON_POLICY_BY_RULE) == set(ValidationRule)
    assert len(REASON_POLICY_BY_RULE) == len(ValidationRule) == 15


def test_policy_for_rule_returns_the_matching_policy() -> None:
    for rule in ValidationRule:
        policy = policy_for_rule(rule)
        assert policy.source_rule is rule
        assert policy.code.value == rule.value


def test_manual_review_policy_has_no_source_rule_and_always_gates() -> None:
    policy = manual_review_policy()
    assert policy.code is DecisionReasonCode.MANUAL_REVIEW_REQUESTED
    assert policy.source_rule is None
    assert policy.triggers_review is True
    assert policy.message == MANUAL_REVIEW_MESSAGE


def test_derived_catalogue_views_are_immutable() -> None:
    with pytest.raises(TypeError):
        REASON_POLICY_BY_CODE[DecisionReasonCode.NO_LINE_ITEMS] = REASON_POLICIES[0]  # type: ignore[index]
    with pytest.raises(TypeError):
        REASON_POLICY_BY_RULE[ValidationRule.NO_LINE_ITEMS] = REASON_POLICIES[0]  # type: ignore[index]


# --- the gating decision itself ---------------------------------------


@pytest.mark.parametrize("rule", list(ValidationRule), ids=lambda r: r.value)
def test_triggers_review_matches_the_policy_table(rule: ValidationRule) -> None:
    assert policy_for_rule(rule).triggers_review == _EXPECTED_TRIGGERS_REVIEW[rule.value]


def test_non_gating_rules_are_exactly_the_documented_three() -> None:
    non_gating = {policy.source_rule for policy in REASON_POLICIES if not policy.triggers_review}
    assert non_gating == _NON_GATING_RULES


def test_high_value_invoice_is_deliberately_gating() -> None:
    assert policy_for_rule(ValidationRule.HIGH_VALUE_INVOICE).triggers_review is True


def test_gating_rules_are_every_other_rule_plus_manual_review() -> None:
    gating_codes = {policy.code for policy in REASON_POLICIES if policy.triggers_review}
    non_gating_codes = {ValidationRule(r).value for r in _NON_GATING_RULES}
    all_codes = {p.code.value for p in REASON_POLICIES}
    assert {c.value for c in gating_codes} == all_codes - non_gating_codes
    assert DecisionReasonCode.MANUAL_REVIEW_REQUESTED in gating_codes


# --- message text --------------------------------------------------


@pytest.mark.parametrize("rule", list(ValidationRule), ids=lambda r: r.value)
def test_rule_derived_message_matches_the_stage5_catalogue(rule: ValidationRule) -> None:
    # Stage 6 never invents new client text for a rule-derived reason - it
    # reuses the Stage 5 sentence verbatim.
    assert policy_for_rule(rule).message == RULE_MESSAGES[rule]


_POLICY_NUMBER = re.compile(r"\d")


@pytest.mark.parametrize("policy", REASON_POLICIES, ids=lambda p: p.code.value)
def test_message_is_a_client_safe_single_sentence(policy: ReasonPolicy) -> None:
    message = policy.message
    assert message == message.strip()
    assert "\n" not in message
    assert message.endswith(".")
    assert message[0].isupper()
    for marker in ("/", "\\", "app.", "Traceback", "self.", "__"):
        assert marker not in message


@pytest.mark.parametrize("policy", REASON_POLICIES, ids=lambda p: p.code.value)
def test_no_policy_number_is_baked_into_the_message(policy: ReasonPolicy) -> None:
    assert not _POLICY_NUMBER.search(policy.message), (policy.code, policy.message)


# --- every policy entry can drive a contract-valid reason -------------


@pytest.mark.parametrize("policy", REASON_POLICIES, ids=lambda p: p.code.value)
def test_policy_produces_a_valid_decision_reason(policy: ReasonPolicy) -> None:
    reason = DecisionReason.model_validate(
        {
            "code": policy.code.value,
            "triggers_review": policy.triggers_review,
            "source_rule": policy.source_rule.value if policy.source_rule else None,
            "field_path": None,
            "message": policy.message,
        }
    )
    assert reason.code is policy.code
