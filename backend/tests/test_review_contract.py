"""Tests for the internal human-review contract (Stage 7, package 1).

Pure Pydantic - no database, no AI, no network. Mirrors
``tests/test_decision_contract.py``.
"""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from app.models.document import DocumentStatus
from app.schemas.review import (
    DOCUMENT_STATUS_FOR_ACTION,
    NOTE_MAX_LENGTH,
    REVIEW_ACTIONS,
    REVIEW_POLICY_VERSION,
    REVIEWER_NAME_MAX_LENGTH,
    InvoiceReview,
    ReviewAction,
    ReviewedInvoiceResult,
)


def _review(**overrides) -> dict:
    base = {"action": "APPROVE", "reviewer_name": "Dana Ops", "note": None}
    base.update(overrides)
    return base


# --- ReviewAction ------------------------------------------------------


def test_review_action_has_exactly_two_members() -> None:
    assert {a.value for a in ReviewAction} == {"APPROVE", "REJECT"}
    assert REVIEW_ACTIONS == ("APPROVE", "REJECT")


# --- reviewer_name ---------------------------------------------------


def test_reviewer_name_is_trimmed() -> None:
    review = InvoiceReview.model_validate(_review(reviewer_name="  Dana Ops  "))
    assert review.reviewer_name == "Dana Ops"


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_blank_reviewer_name_is_rejected(blank: str) -> None:
    with pytest.raises(ValidationError):
        InvoiceReview.model_validate(_review(reviewer_name=blank))


def test_over_length_reviewer_name_is_rejected() -> None:
    with pytest.raises(ValidationError):
        InvoiceReview.model_validate(
            _review(reviewer_name="x" * (REVIEWER_NAME_MAX_LENGTH + 1))
        )


# --- note ------------------------------------------------------------


def test_reject_requires_a_note() -> None:
    with pytest.raises(ValidationError):
        InvoiceReview.model_validate(_review(action="REJECT", note=None))


def test_reject_with_a_note_is_valid_and_trimmed() -> None:
    review = InvoiceReview.model_validate(
        _review(action="REJECT", note="  wrong vendor  ")
    )
    assert review.action is ReviewAction.REJECT
    assert review.note == "wrong vendor"


def test_approve_without_a_note_is_valid() -> None:
    review = InvoiceReview.model_validate(_review(action="APPROVE", note=None))
    assert review.note is None


def test_approve_with_a_blank_note_is_rejected_not_coerced() -> None:
    with pytest.raises(ValidationError):
        InvoiceReview.model_validate(_review(action="APPROVE", note="   "))


def test_reject_with_a_blank_note_is_rejected() -> None:
    with pytest.raises(ValidationError):
        InvoiceReview.model_validate(_review(action="REJECT", note="   "))


def test_over_length_note_is_rejected() -> None:
    with pytest.raises(ValidationError):
        InvoiceReview.model_validate(
            _review(action="REJECT", note="x" * (NOTE_MAX_LENGTH + 1))
        )


# --- unknown keys / result wrapper ---------------------------------


def test_unknown_key_is_rejected() -> None:
    with pytest.raises(ValidationError):
        InvoiceReview.model_validate(_review(reviewer_id="u-1"))


def test_reviewed_invoice_result_carries_only_source_and_review() -> None:
    result = ReviewedInvoiceResult.model_validate(
        {
            "source_decision_id": str(uuid.uuid4()),
            "review": _review(),
        }
    )
    assert isinstance(result.source_decision_id, uuid.UUID)
    assert result.review.action is ReviewAction.APPROVE
    with pytest.raises(ValidationError):
        ReviewedInvoiceResult.model_validate(
            {
                "source_decision_id": str(uuid.uuid4()),
                "review": _review(),
                "document_id": str(uuid.uuid4()),
            }
        )


# --- policy constants ----------------------------------------------


def test_document_status_for_action_maps_both_actions_and_nothing_else() -> None:
    assert DOCUMENT_STATUS_FOR_ACTION == {
        ReviewAction.APPROVE: DocumentStatus.APPROVED,
        ReviewAction.REJECT: DocumentStatus.REJECTED,
    }


def test_policy_version_is_a_non_empty_string() -> None:
    assert isinstance(REVIEW_POLICY_VERSION, str) and REVIEW_POLICY_VERSION
