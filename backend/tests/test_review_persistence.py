"""Tests for the ``InvoiceReview`` <-> flat-row bridge (Stage 7, package 1).

Pure structural transformation - no database. Mirrors
``tests/test_decision_persistence.py``.
"""

from __future__ import annotations

import pytest

from app.schemas.review import InvoiceReview, ReviewAction
from app.schemas.review_persistence import (
    REVIEW_COLUMN_NAMES,
    invoice_review_from_row,
    review_row,
)


def _approve() -> InvoiceReview:
    return InvoiceReview.model_validate(
        {"action": "APPROVE", "reviewer_name": "Dana Ops", "note": None}
    )


def _reject() -> InvoiceReview:
    return InvoiceReview.model_validate(
        {"action": "REJECT", "reviewer_name": "Dana Ops", "note": "duplicate of INV-9"}
    )


def test_review_row_has_exactly_the_contract_columns() -> None:
    row = review_row(_reject())
    assert set(row) == set(REVIEW_COLUMN_NAMES)
    assert row["action"] is ReviewAction.REJECT
    assert row["reviewer_name"] == "Dana Ops"
    assert row["note"] == "duplicate of INV-9"


@pytest.mark.parametrize("review", [_approve(), _reject()])
def test_round_trip_through_a_mapping(review: InvoiceReview) -> None:
    rebuilt = invoice_review_from_row(review_row(review))
    assert rebuilt == review


def test_round_trip_through_an_orm_like_object() -> None:
    review = _reject()

    class _Row:
        def __init__(self, data: dict) -> None:
            self.__dict__.update(data)

    rebuilt = invoice_review_from_row(_Row(review_row(review)))
    assert rebuilt == review


def test_rebuild_re_enforces_the_note_rule() -> None:
    # A row that violates the REJECT-needs-a-note rule cannot be rebuilt into a
    # contract object, even though the dict itself is well formed.
    with pytest.raises(ValueError):
        invoice_review_from_row(
            {"action": "REJECT", "reviewer_name": "Dana Ops", "note": None}
        )
