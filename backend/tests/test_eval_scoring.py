"""Tests for the offline evaluation scoring harness (Stage 3, step 11).

The harness itself calls no provider; these feed it hand-built predictions.
"""

from __future__ import annotations

import pytest

from app.schemas.extraction import InvoiceExtraction
from evaluation.dataset import load_cases
from evaluation.scoring import score_case, score_run

CASES = {c.id: c for c in load_cases()}
SCORABLE = [c for c in load_cases() if c.expected is not None]


def _as_invoice(case_id: str) -> InvoiceExtraction:
    """The ground truth for a case, as a provider would return it (confidences aside)."""
    return InvoiceExtraction.model_validate(CASES[case_id].expected.as_contract_payload())


# --- perfect and empty predictions ------------------------------------


@pytest.mark.parametrize("case", SCORABLE, ids=lambda c: c.id)
def test_ground_truth_scores_itself_as_perfect(case) -> None:
    result = score_case(
        case.expected, _as_invoice(case.id), case_id=case.id, category=case.category
    )
    assert result.scalar_correct == result.scalar_total
    assert result.critical_correct == result.critical_total
    assert result.line_item_field_correct == result.line_item_field_total
    assert result.line_item_count_exact


def test_missing_prediction_counts_every_field_wrong() -> None:
    case = CASES["digital_basic"]
    result = score_case(case.expected, None, case_id=case.id, category=case.category)
    assert result.scored is True
    assert result.scalar_correct == 0
    assert result.line_item_field_correct == 0
    assert result.predicted_item_count == 0
    assert not result.line_item_count_exact


def test_missing_prediction_does_not_get_credit_for_expected_nulls() -> None:
    case = CASES["digital_incomplete"]
    result = score_case(case.expected, None, case_id=case.id, category=case.category)
    assert result.scalar_correct == 0
    assert result.line_item_field_correct == 0


# --- matching rules ------------------------------------------------


def _tweak(case_id: str, **scalar_overrides) -> InvoiceExtraction:
    payload = CASES[case_id].expected.as_contract_payload()
    for name, value in scalar_overrides.items():
        payload[name] = {"value": value, "confidence": None}
    return InvoiceExtraction.model_validate(payload)


def test_amounts_match_numerically_not_textually() -> None:
    case = CASES["digital_basic"]  # total_amount ground truth "1190.00"
    predicted = _tweak(case.id, total_amount="1190")
    result = score_case(case.expected, predicted, case_id=case.id, category=case.category)
    assert result.scalars["total_amount"].correct


def test_text_match_is_case_and_whitespace_insensitive() -> None:
    case = CASES["digital_basic"]
    predicted = _tweak(case.id, vendor_name="  northwind   TRADERS  gmbh ")
    result = score_case(case.expected, predicted, case_id=case.id, category=case.category)
    assert result.scalars["vendor_name"].correct


def test_dates_are_compared_verbatim() -> None:
    case = CASES["digital_basic"]  # invoice_date "2026-01-15"
    predicted = _tweak(case.id, invoice_date="15/01/2026")
    result = score_case(case.expected, predicted, case_id=case.id, category=case.category)
    assert not result.scalars["invoice_date"].correct


def test_value_against_null_is_wrong_in_both_directions() -> None:
    case = CASES["digital_incomplete"]  # due_date ground truth is None
    predicted = _tweak(case.id, due_date="2026-03-01")
    result = score_case(case.expected, predicted, case_id=case.id, category=case.category)
    assert not result.scalars["due_date"].correct
    # ...and a real value predicted as null is also wrong
    predicted2 = _tweak(case.id, total_amount=None)
    result2 = score_case(case.expected, predicted2, case_id=case.id, category=case.category)
    assert not result2.scalars["total_amount"].correct


def test_short_predicted_line_item_list_is_penalised() -> None:
    case = CASES["digital_multi_line_items"]  # three items in ground truth
    payload = case.expected.as_contract_payload()
    payload["line_items"] = payload["line_items"][:1]
    predicted = InvoiceExtraction.model_validate(payload)

    result = score_case(case.expected, predicted, case_id=case.id, category=case.category)
    assert result.expected_item_count == 3
    assert result.predicted_item_count == 1
    assert not result.line_item_count_exact
    # rows 2 and 3 have no prediction -> their fields are wrong
    assert result.line_item_field_correct < result.line_item_field_total


def test_extra_predicted_line_item_is_penalised_in_field_accuracy() -> None:
    case = CASES["digital_basic"]
    payload = case.expected.as_contract_payload()
    payload["line_items"].append(payload["line_items"][0])
    predicted = InvoiceExtraction.model_validate(payload)

    result = score_case(case.expected, predicted, case_id=case.id, category=case.category)
    assert result.line_item_field_correct == len(result.line_items[0])
    assert result.line_item_field_total == 2 * len(result.line_items[0])
    assert not result.line_item_count_exact


# --- run-level aggregation ---------------------------------------


def test_score_run_perfect_predictions() -> None:
    predictions = {c.id: _as_invoice(c.id) for c in SCORABLE}
    report = score_run(predictions)

    assert report.field_accuracy == 1.0
    assert report.critical_field_accuracy == 1.0
    assert report.line_item_field_accuracy == 1.0
    assert report.line_item_count_exact_rate == 1.0
    # the two unusable cases are recorded but not scored
    assert len(report.scored) == len(SCORABLE)
    assert {r.case_id for r in report.results if not r.scored} == {"low_quality", "not_an_invoice"}
    assert "not_invoice" not in report.by_category


def test_score_run_discriminates_a_fixed_wrong_prediction() -> None:
    from app.services.processing.extraction.fake import deterministic_invoice_payload

    fixed = InvoiceExtraction.model_validate(deterministic_invoice_payload("00000000-0000-0000-0000-000000000000"))
    report = score_run({c.id: fixed for c in SCORABLE})

    assert report.field_accuracy < 0.5
    assert report.critical_field_accuracy < 0.5
    assert "field accuracy" in report.format_summary()


def test_confidence_is_measured_separately_from_value_accuracy() -> None:
    case = CASES["digital_basic"]
    payload = case.expected.as_contract_payload()
    for name, field in payload.items():
        if name == "line_items":
            for item in field:
                for value in item.values():
                    value["confidence"] = "0.80"
        else:
            field["confidence"] = "0.80"
    report = score_run({case.id: InvoiceExtraction.model_validate(payload)}, cases=[case])

    assert report.field_accuracy == 1.0
    assert report.confidence_coverage == 1.0
    assert report.confidence_brier_score == pytest.approx(0.04)


def test_absent_confidence_reports_null_calibration_result() -> None:
    case = CASES["digital_basic"]
    report = score_run({case.id: _as_invoice(case.id)}, cases=[case])
    assert report.confidence_coverage == 0.0
    assert report.confidence_brier_score is None
