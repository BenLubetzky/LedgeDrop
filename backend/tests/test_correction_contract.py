"""Pure tests for the Stage 8 internal contract and the status matrix."""

from __future__ import annotations

import pytest

from app.models.decision import DecisionOutcome
from app.models.document import DocumentStatus
from app.schemas.correction import (
    CORRECTABLE_DOCUMENT_STATUSES,
    CorrectionFieldEntry,
    CorrectionOperation,
    CorrectionStatus,
    InvoiceCorrection,
)
from app.services.processing.correction.lifecycle import resolve_document_status


def _entry(**overrides) -> dict:
    base = {
        "operation": CorrectionOperation.SET_FIELD,
        "field_path": "total_amount",
        "previous_value": "1",
        "raw_value": "2",
        "normalized_value": "2",
        "error_code": None,
        "error_message": None,
    }
    base.update(overrides)
    return base


def test_status_and_operation_enums_are_closed() -> None:
    assert [s.value for s in CorrectionStatus] == ["PROCESSING", "COMPLETED", "FAILED"]
    assert [o.value for o in CorrectionOperation] == [
        "SET_FIELD",
        "ADD_LINE_ITEM",
        "REMOVE_LINE_ITEM",
    ]


def test_correctable_statuses_are_exactly_the_four_terminal_ones() -> None:
    assert CORRECTABLE_DOCUMENT_STATUSES == frozenset(
        {
            DocumentStatus.NEEDS_REVIEW,
            DocumentStatus.COMPLETED,
            DocumentStatus.APPROVED,
            DocumentStatus.REJECTED,
        }
    )


def test_reviewer_name_is_trimmed_and_bounded() -> None:
    correction = InvoiceCorrection(
        reviewer_name="  Dana  ", note=None, entries=[CorrectionFieldEntry(**_entry())]
    )
    assert correction.reviewer_name == "Dana"
    with pytest.raises(ValueError):
        InvoiceCorrection(reviewer_name="   ", note=None, entries=[])
    with pytest.raises(ValueError):
        InvoiceCorrection(
            reviewer_name="x" * 201, note=None, entries=[]
        )


def test_blank_note_is_rejected_but_absent_note_is_fine() -> None:
    with pytest.raises(ValueError):
        InvoiceCorrection(reviewer_name="Dana", note="   ", entries=[])
    assert InvoiceCorrection(reviewer_name="Dana", note=None, entries=[]).note is None


def test_error_code_and_message_must_travel_together() -> None:
    with pytest.raises(ValueError):
        CorrectionFieldEntry(**_entry(error_code="invalid_number"))
    entry = CorrectionFieldEntry(
        **_entry(normalized_value=None, error_code="invalid_number", error_message="bad")
    )
    assert entry.error_code is not None and entry.error_message == "bad"


def test_duplicate_field_paths_are_rejected() -> None:
    with pytest.raises(ValueError):
        InvoiceCorrection(
            reviewer_name="Dana",
            note=None,
            entries=[
                CorrectionFieldEntry(**_entry(field_path="total_amount")),
                CorrectionFieldEntry(**_entry(field_path="total_amount")),
            ],
        )


def test_line_add_remove_field_path_must_be_a_bare_line() -> None:
    with pytest.raises(ValueError):
        CorrectionFieldEntry(
            **_entry(
                operation=CorrectionOperation.ADD_LINE_ITEM,
                field_path="line_items.0.quantity",
                previous_value=None,
                raw_value=None,
                normalized_value=None,
            )
        )
    ok = CorrectionFieldEntry(
        **_entry(
            operation=CorrectionOperation.REMOVE_LINE_ITEM,
            field_path="line_items.3",
            previous_value="x | | |",
            raw_value=None,
            normalized_value=None,
        )
    )
    assert ok.field_path == "line_items.3"


def test_resolve_document_status_matches_the_pinned_matrix() -> None:
    A, R = DecisionOutcome.ACCEPTED, DecisionOutcome.NEEDS_REVIEW
    S = DocumentStatus
    expected = {
        (A, S.COMPLETED): S.COMPLETED,
        (A, S.NEEDS_REVIEW): S.APPROVED,
        (A, S.APPROVED): S.APPROVED,
        (A, S.REJECTED): S.APPROVED,
        (R, S.COMPLETED): S.NEEDS_REVIEW,
        (R, S.NEEDS_REVIEW): S.NEEDS_REVIEW,
        (R, S.APPROVED): S.NEEDS_REVIEW,
        (R, S.REJECTED): S.NEEDS_REVIEW,
    }
    for (outcome, origin), want in expected.items():
        assert (
            resolve_document_status(origin_status=origin, outcome=outcome) == want
        ), (outcome, origin)
