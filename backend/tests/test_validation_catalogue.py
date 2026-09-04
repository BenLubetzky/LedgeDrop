"""Tests for the formal Stage 5 rule catalogue (step 4).

No database and no AI provider - these exercise the static per-rule catalogue in
``app/schemas/validation_catalogue.py`` and check it stays faithful to
``docs/stage-5-validation.md`` §3 and to the step 3 contract.
"""

from __future__ import annotations

import re

import pytest

from app.schemas.normalization import NORMALIZED_SCALAR_FIELD_NAMES
from app.schemas.validation import (
    FindingSeverity,
    InvoiceValidation,
    ValidationFinding,
    ValidationRule,
)
from app.schemas.validation_catalogue import (
    FIELD_PATH_TEMPLATES,
    INPUT_TOKENS,
    RULE_CATALOGUE,
    RULE_MESSAGES,
    RULE_SPECS,
    RuleSpec,
    spec_for,
)

# --- §3 table, transcribed for cross-checking -------------------------

# Fixed severity per rule (``None`` = severity is decided per invoice).
_EXPECTED_SEVERITY: dict[str, FindingSeverity | None] = {
    "missing_required_field": FindingSeverity.ERROR,
    "normalization_error": None,
    "due_date_before_invoice_date": FindingSeverity.WARNING,
    "due_date_far_after_invoice_date": FindingSeverity.WARNING,
    "invoice_date_in_future": FindingSeverity.WARNING,
    "invoice_date_implausibly_old": FindingSeverity.WARNING,
    "totals_do_not_reconcile": FindingSeverity.WARNING,
    "line_item_amount_mismatch": FindingSeverity.WARNING,
    "line_items_do_not_sum": FindingSeverity.WARNING,
    "line_item_sum_not_checked": FindingSeverity.INFO,
    "low_confidence_critical_field": FindingSeverity.WARNING,
    "critical_field_confidence_unavailable": FindingSeverity.INFO,
    "probable_duplicate_invoice": FindingSeverity.WARNING,
    "high_value_invoice": FindingSeverity.INFO,
    "no_line_items": FindingSeverity.INFO,
}

# ``field_path`` shape per rule: ``None`` = invoice-level.
_EXPECTED_FIELD_PATH: dict[str, str | None] = {
    "missing_required_field": "<required field>",
    "normalization_error": "<errored field>",
    "due_date_before_invoice_date": "due_date",
    "due_date_far_after_invoice_date": "due_date",
    "invoice_date_in_future": "invoice_date",
    "invoice_date_implausibly_old": "invoice_date",
    "totals_do_not_reconcile": None,
    "line_item_amount_mismatch": "line_items.<i>.line_total",
    "line_items_do_not_sum": None,
    "line_item_sum_not_checked": None,
    "low_confidence_critical_field": "<critical field>",
    "critical_field_confidence_unavailable": "<critical field>",
    "probable_duplicate_invoice": None,
    "high_value_invoice": None,
    "no_line_items": None,
}


# --- the catalogue is closed and matches the rule enum ---------------


def test_catalogue_covers_every_rule_exactly_once_in_declaration_order() -> None:
    assert tuple(spec.rule for spec in RULE_SPECS) == tuple(ValidationRule)
    assert set(RULE_CATALOGUE) == set(ValidationRule)
    assert len(RULE_SPECS) == len(ValidationRule) == 15


def test_spec_for_returns_the_matching_spec() -> None:
    for rule in ValidationRule:
        assert spec_for(rule).rule is rule
    assert RULE_MESSAGES == {r: spec_for(r).message for r in ValidationRule}


def test_derived_catalogue_views_are_immutable() -> None:
    with pytest.raises(TypeError):
        RULE_CATALOGUE[ValidationRule.NO_LINE_ITEMS] = RULE_SPECS[0]  # type: ignore[index]
    with pytest.raises(TypeError):
        RULE_MESSAGES[ValidationRule.NO_LINE_ITEMS] = "Changed."  # type: ignore[index]


# --- severity shape -------------------------------------------------


@pytest.mark.parametrize("rule", list(ValidationRule), ids=lambda r: r.value)
def test_fixed_severity_matches_the_spec_table(rule: ValidationRule) -> None:
    assert spec_for(rule).severity == _EXPECTED_SEVERITY[rule.value]


def test_only_normalization_error_has_conditional_severity() -> None:
    conditional = [s.rule for s in RULE_SPECS if s.has_conditional_severity]
    assert conditional == [ValidationRule.NORMALIZATION_ERROR]


def test_severity_note_is_present_exactly_when_severity_is_conditional() -> None:
    for spec in RULE_SPECS:
        assert (spec.severity is None) == (spec.severity_note is not None)


# --- field_path shape ---------------------------------------------


@pytest.mark.parametrize("rule", list(ValidationRule), ids=lambda r: r.value)
def test_field_path_shape_matches_the_spec_table(rule: ValidationRule) -> None:
    assert spec_for(rule).field_path == _EXPECTED_FIELD_PATH[rule.value]


def test_field_path_is_null_a_literal_scalar_or_a_known_template() -> None:
    allowed = set(NORMALIZED_SCALAR_FIELD_NAMES) | FIELD_PATH_TEMPLATES
    for spec in RULE_SPECS:
        assert spec.field_path is None or spec.field_path in allowed
        assert spec.is_invoice_level == (spec.field_path is None)


# --- inputs -----------------------------------------------------


def test_every_input_token_is_a_scalar_name_or_a_known_token() -> None:
    known = INPUT_TOKENS | set(NORMALIZED_SCALAR_FIELD_NAMES)
    for spec in RULE_SPECS:
        assert spec.inputs, spec.rule
        assert set(spec.inputs) <= known, spec.rule


def test_rules_that_read_a_policy_set_declare_the_policy_token() -> None:
    # The catalogue names its policy dependency without embedding the value.
    assert "required_field_policy" in spec_for(ValidationRule.MISSING_REQUIRED_FIELD).inputs
    assert "confidence_threshold" in spec_for(ValidationRule.LOW_CONFIDENCE_CRITICAL_FIELD).inputs
    assert "high_value_policy" in spec_for(ValidationRule.HIGH_VALUE_INVOICE).inputs


# --- skip conditions --------------------------------------------


def test_always_running_rules_have_no_skip_conditions() -> None:
    always = {ValidationRule.MISSING_REQUIRED_FIELD, ValidationRule.NORMALIZATION_ERROR}
    for spec in RULE_SPECS:
        if spec.rule in always:
            assert spec.skip_when == ()
        else:
            assert spec.skip_when, spec.rule


def test_skip_conditions_are_non_blank_strings() -> None:
    for spec in RULE_SPECS:
        for condition in spec.skip_when:
            assert isinstance(condition, str) and condition.strip()


# --- context keys ---------------------------------------------


def test_context_keys_are_unique_per_spec() -> None:
    for spec in RULE_SPECS:
        assert len(set(spec.context_keys)) == len(spec.context_keys)


def test_threshold_rules_record_the_threshold_they_used() -> None:
    # §2: a threshold-based finding must carry the constant it applied.
    assert "threshold" in spec_for(ValidationRule.HIGH_VALUE_INVOICE).context_keys
    assert "threshold" in spec_for(ValidationRule.LOW_CONFIDENCE_CRITICAL_FIELD).context_keys
    assert "tolerance" in spec_for(ValidationRule.TOTALS_DO_NOT_RECONCILE).context_keys
    assert set(spec_for(ValidationRule.PROBABLE_DUPLICATE_INVOICE).context_keys) == {
        "matches",
        "tolerance",
    }


# --- message text ------------------------------------------------

_POLICY_NUMBER = re.compile(r"\d")


@pytest.mark.parametrize("spec", RULE_SPECS, ids=lambda s: s.rule.value)
def test_message_is_a_client_safe_single_sentence(spec: RuleSpec) -> None:
    message = spec.message
    assert message == message.strip()
    assert "\n" not in message
    assert message.endswith(".")
    assert message[0].isupper()
    # client-safe: no path, no obvious internal marker
    for marker in ("/", "\\", "app.", "Traceback", "self.", "__"):
        assert marker not in message


@pytest.mark.parametrize("spec", RULE_SPECS, ids=lambda s: s.rule.value)
def test_no_policy_number_is_baked_into_client_or_behaviour_text(spec: RuleSpec) -> None:
    # ⚠ policy values (0.70, 365, 10 years, 10000, ...) live only in the doc
    # until step 8. The client-facing message and the skip conditions - the
    # parts that would carry a threshold if any part did - must not bake in a
    # tunable number.
    assert not _POLICY_NUMBER.search(spec.message), (spec.rule, spec.message)
    for condition in spec.skip_when:
        assert not _POLICY_NUMBER.search(condition), (spec.rule, condition)


def test_messages_are_distinct() -> None:
    assert len({spec.message for spec in RULE_SPECS}) == len(RULE_SPECS)


# --- each spec can drive a contract-valid finding ------------------


def _sample_field_path(shape: str | None) -> str | None:
    if shape is None:
        return None
    if shape in NORMALIZED_SCALAR_FIELD_NAMES:
        return shape
    # resolve the angle-bracket templates the way step 8 will
    return {
        "<required field>": "total_amount",
        "<errored field>": "subtotal",
        "<critical field>": "vendor_name",
        "line_items.<i>.line_total": "line_items.0.line_total",
    }[shape]


@pytest.mark.parametrize("spec", RULE_SPECS, ids=lambda s: s.rule.value)
def test_spec_produces_a_valid_validation_finding(spec: RuleSpec) -> None:
    severity = spec.severity or FindingSeverity.WARNING
    finding = ValidationFinding.model_validate(
        {
            "rule": spec.rule.value,
            "severity": severity.value,
            "field_path": _sample_field_path(spec.field_path),
            "expected": None,
            "actual": None,
            "message": spec.message,
            "context": {key: 1 for key in spec.context_keys},
        }
    )
    assert finding.rule is spec.rule
    # and it aggregates into an InvoiceValidation without complaint
    InvoiceValidation.from_findings([finding])
