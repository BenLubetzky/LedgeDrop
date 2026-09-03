"""Tests for the internal invoice-validation data contract (Stage 5, step 3).

No database and no AI provider are involved - these exercise pure schema
behaviour and the boundary guarantees from ``docs/stage-5-validation.md`` §1.7.
"""

from __future__ import annotations

import json
import uuid
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.schemas.validation import (
    VALIDATION_FINDING_FIELD_NAMES,
    VALIDATION_RULE_CODES,
    FindingSeverity,
    InvoiceValidation,
    ValidatedInvoiceResult,
    ValidationFinding,
    ValidationRule,
    ValidationStatus,
    ValidationSummary,
)


def _finding(**overrides) -> dict:
    data = {
        "rule": "totals_do_not_reconcile",
        "severity": "warning",
        "field_path": None,
        "expected": Decimal("119.00"),
        "actual": Decimal("120.00"),
        "message": "The invoice totals do not reconcile.",
        "context": {"delta": Decimal("-1.00")},
    }
    data.update(overrides)
    return data


# --- enums are closed and exactly as specified --------------------------


def test_validation_status_members_are_exactly_three() -> None:
    assert {s.value for s in ValidationStatus} == {"PROCESSING", "COMPLETED", "FAILED"}


def test_no_decision_state_leaks_into_status() -> None:
    forbidden = {"ACCEPTED", "REJECTED", "NEEDS_REVIEW", "ESCALATED", "APPROVED"}
    assert forbidden.isdisjoint({s.value for s in ValidationStatus})


def test_finding_severity_members_are_exactly_three() -> None:
    assert {s.value for s in FindingSeverity} == {"error", "warning", "info"}


def test_validation_rule_catalogue_is_closed_and_complete() -> None:
    assert set(VALIDATION_RULE_CODES) == {
        "missing_required_field",
        "normalization_error",
        "due_date_before_invoice_date",
        "due_date_far_after_invoice_date",
        "invoice_date_in_future",
        "invoice_date_implausibly_old",
        "totals_do_not_reconcile",
        "line_item_amount_mismatch",
        "line_items_do_not_sum",
        "line_item_sum_not_checked",
        "low_confidence_critical_field",
        "critical_field_confidence_unavailable",
        "probable_duplicate_invoice",
        "high_value_invoice",
        "no_line_items",
    }
    assert len(VALIDATION_RULE_CODES) == len(set(VALIDATION_RULE_CODES))


def test_unknown_rule_code_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ValidationFinding.model_validate(_finding(rule="looks_fine_but_fake"))


# --- the finding has no decision vocabulary (spec §1.7) ----------------


def test_finding_field_set_is_exactly_the_contract() -> None:
    assert set(VALIDATION_FINDING_FIELD_NAMES) == {
        "rule",
        "severity",
        "field_path",
        "expected",
        "actual",
        "message",
        "context",
    }


@pytest.mark.parametrize(
    "decision_field",
    ["accepted", "action", "disposition", "resolution", "verdict", "score", "outcome"],
)
def test_finding_rejects_decision_fields(decision_field: str) -> None:
    with pytest.raises(ValidationError):
        ValidationFinding.model_validate(_finding(**{decision_field: "accept"}))


# --- field_path reuses the Stage 4 vocabulary -------------------------


@pytest.mark.parametrize(
    "path",
    [
        None,
        "invoice_number",
        "total_amount",
        "currency",
        "line_items.0.line_total",
        "line_items.12.unit_price",
    ],
)
def test_field_path_accepts_stage4_paths(path: str | None) -> None:
    assert ValidationFinding.model_validate(_finding(field_path=path)).field_path == path


@pytest.mark.parametrize(
    "path",
    [
        "not_a_field",
        "line_items",
        "line_items.0",
        "line_items.0.bogus",
        "line_items.01.line_total",  # leading zero
        "line_items.-1.line_total",
        "total_amount.sub",
        "LINE_ITEMS.0.line_total",
        "",
    ],
)
def test_field_path_rejects_malformed_paths(path: str) -> None:
    with pytest.raises(ValidationError):
        ValidationFinding.model_validate(_finding(field_path=path))


# --- expected / actual are client-safe scalars only ------------------


@pytest.mark.parametrize("value", [None, "2026-01-15", Decimal("0"), Decimal("-1234.50")])
def test_expected_actual_accept_string_decimal_or_null(value) -> None:
    finding = ValidationFinding.model_validate(_finding(expected=value, actual=value))
    assert finding.expected == value
    assert finding.actual == value


@pytest.mark.parametrize("value", [1.5, 3, True, [], {"x": 1}])
def test_expected_actual_reject_non_scalar_or_float(value) -> None:
    with pytest.raises(ValidationError):
        ValidationFinding.model_validate(_finding(expected=value))


def test_expected_actual_reject_non_finite_decimal() -> None:
    with pytest.raises(ValidationError):
        ValidationFinding.model_validate(_finding(actual=Decimal("NaN")))


def test_decimal_expected_serialises_as_json_string() -> None:
    finding = ValidationFinding.model_validate(_finding(expected=Decimal("119.00")))
    dumped = json.loads(finding.model_dump_json())
    assert dumped["expected"] == "119.00"
    assert dumped["context"]["delta"] == "-1.00"


# --- context rejects binary floats anywhere -------------------------


def test_context_accepts_decimal_int_and_nested_structures() -> None:
    context = {
        "threshold": Decimal("10000"),
        "currency": "EUR",
        "line_count": 3,
        "matches": [{"document_id": str(uuid.uuid4()), "normalization_id": str(uuid.uuid4())}],
    }
    assert ValidationFinding.model_validate(_finding(context=context)).context == context


@pytest.mark.parametrize(
    "context",
    [
        {"delta": 1.5},
        {"nested": {"delta": 2.0}},
        {"items": [{"amount": 3.0}]},
        {"items": [1, 2, 3.5]},
    ],
)
def test_context_rejects_binary_float(context: dict) -> None:
    with pytest.raises(ValidationError):
        ValidationFinding.model_validate(_finding(context=context))


def test_context_rejects_non_string_keys() -> None:
    with pytest.raises(ValidationError):
        ValidationFinding.model_validate(_finding(context={1: "x"}))


@pytest.mark.parametrize(
    "value",
    [Decimal("NaN"), Decimal("Infinity"), {"not-json"}, ("tuple",)],
)
def test_context_rejects_non_finite_or_non_json_values(value) -> None:
    with pytest.raises(ValidationError):
        ValidationFinding.model_validate(_finding(context={"value": value}))


# --- message and strictness -----------------------------------------


@pytest.mark.parametrize("message", ["", "   ", "\t\n"])
def test_message_must_be_non_blank(message: str) -> None:
    with pytest.raises(ValidationError):
        ValidationFinding.model_validate(_finding(message=message))


def test_finding_rejects_unknown_keys() -> None:
    with pytest.raises(ValidationError):
        ValidationFinding.model_validate(_finding(severity_note="high"))


def test_finding_requires_every_field_present() -> None:
    incomplete = _finding()
    del incomplete["context"]
    with pytest.raises(ValidationError):
        ValidationFinding.model_validate(incomplete)


# --- summary is counts only ----------------------------------------


def test_summary_fields_are_only_counts() -> None:
    assert set(ValidationSummary.model_fields) == {"total", "error", "warning", "info"}
    for field in ValidationSummary.model_fields.values():
        assert field.annotation is int


def test_summary_total_must_match_parts() -> None:
    with pytest.raises(ValidationError):
        ValidationSummary(total=5, error=1, warning=1, info=1)


@pytest.mark.parametrize("value", [True, "1", 1.0])
def test_summary_counts_are_strict_integers(value) -> None:
    with pytest.raises(ValidationError):
        ValidationSummary(total=value, error=0, warning=0, info=0)


def test_summary_from_findings_tallies_by_severity() -> None:
    findings = [
        ValidationFinding.model_validate(_finding(rule="missing_required_field", severity="error", field_path="currency", expected=None, actual=None, context={})),
        ValidationFinding.model_validate(_finding(severity="warning")),
        ValidationFinding.model_validate(_finding(rule="high_value_invoice", severity="info", expected=None, actual=None, context={})),
        ValidationFinding.model_validate(_finding(rule="no_line_items", severity="info", field_path=None, expected=None, actual=None, context={})),
    ]
    summary = ValidationSummary.from_findings(findings)
    assert (summary.total, summary.error, summary.warning, summary.info) == (4, 1, 1, 2)


# --- InvoiceValidation ties the summary to the findings ------------


def test_invoice_validation_accepts_empty_findings() -> None:
    payload = InvoiceValidation.from_findings([])
    assert payload.findings == []
    assert payload.summary.total == 0


def test_invoice_validation_rejects_summary_that_disagrees_with_findings() -> None:
    finding = ValidationFinding.model_validate(_finding())
    with pytest.raises(ValidationError):
        InvoiceValidation(
            findings=[finding],
            summary=ValidationSummary(total=0, error=0, warning=0, info=0),
        )


def test_invoice_validation_json_shape_is_stable() -> None:
    # ``expected`` / ``actual`` / ``context`` are polymorphic display values:
    # a Decimal serializes to a JSON string and re-loads as that string (the
    # typed value is reconstructed from persistence columns later, not by
    # re-parsing this JSON). What must stay stable is the serialized shape.
    payload = InvoiceValidation.from_findings([ValidationFinding.model_validate(_finding())])
    once = payload.model_dump_json()
    twice = InvoiceValidation.model_validate_json(once).model_dump_json()
    assert once == twice
    assert json.loads(once)["summary"] == {"total": 1, "error": 0, "warning": 1, "info": 0}


def test_invoice_validation_rejects_unknown_keys() -> None:
    with pytest.raises(ValidationError):
        InvoiceValidation.model_validate(
            {"findings": [], "summary": {"total": 0, "error": 0, "warning": 0, "info": 0}, "passed": True}
        )


# --- the result binds to the source normalization attempt ---------


def test_result_binds_to_source_normalization_id() -> None:
    nid = uuid.uuid4()
    result = ValidatedInvoiceResult(
        source_normalization_id=nid,
        validation=InvoiceValidation.from_findings([]),
    )
    assert result.source_normalization_id == nid


def test_result_rejects_unknown_keys() -> None:
    with pytest.raises(ValidationError):
        ValidatedInvoiceResult.model_validate(
            {
                "source_normalization_id": str(uuid.uuid4()),
                "validation": InvoiceValidation.from_findings([]).model_dump(),
                "document_id": str(uuid.uuid4()),
            }
        )
