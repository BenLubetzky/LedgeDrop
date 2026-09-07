"""Tests for the Stage 7 review lifecycle guards (package 2).

Pure functions - no database. Mirrors ``tests/test_decision_lifecycle.py``.
"""

from __future__ import annotations

import uuid

import pytest

from app.core.errors import ConflictError
from app.models.decision import DecisionOutcome, DecisionStatus
from app.models.document import DocumentStatus
from app.schemas.review import ReviewAction
from app.services.processing.review import lifecycle


# --- document_status_for_action -----------------------------------


def test_document_status_for_action() -> None:
    assert (
        lifecycle.document_status_for_action(ReviewAction.APPROVE)
        is DocumentStatus.APPROVED
    )
    assert (
        lifecycle.document_status_for_action(ReviewAction.REJECT)
        is DocumentStatus.REJECTED
    )


# --- ensure_decision_can_be_reviewed -----------------------------


def test_completed_needs_review_and_unreviewed_is_allowed() -> None:
    lifecycle.ensure_decision_can_be_reviewed(
        decision_status=DecisionStatus.COMPLETED,
        decision_outcome=DecisionOutcome.NEEDS_REVIEW,
        already_reviewed=False,
    )


@pytest.mark.parametrize(
    ("status", "outcome"),
    [
        (DecisionStatus.PROCESSING, None),
        (DecisionStatus.FAILED, None),
        (DecisionStatus.COMPLETED, DecisionOutcome.ACCEPTED),
    ],
)
def test_non_reviewable_decisions_are_rejected(status, outcome) -> None:
    with pytest.raises(ConflictError) as err:
        lifecycle.ensure_decision_can_be_reviewed(
            decision_status=status,
            decision_outcome=outcome,
            already_reviewed=False,
        )
    assert err.value.code == "DECISION_NOT_REVIEWABLE"


def test_already_reviewed_decision_is_rejected() -> None:
    with pytest.raises(ConflictError) as err:
        lifecycle.ensure_decision_can_be_reviewed(
            decision_status=DecisionStatus.COMPLETED,
            decision_outcome=DecisionOutcome.NEEDS_REVIEW,
            already_reviewed=True,
        )
    assert err.value.code == "DECISION_ALREADY_REVIEWED"


# --- ensure_document_awaiting_review ---------------------------


def test_document_must_be_needs_review() -> None:
    lifecycle.ensure_document_awaiting_review(DocumentStatus.NEEDS_REVIEW)
    for other in (
        DocumentStatus.COMPLETED,
        DocumentStatus.APPROVED,
        DocumentStatus.REJECTED,
    ):
        with pytest.raises(ConflictError) as err:
            lifecycle.ensure_document_awaiting_review(other)
        assert err.value.code == "DECISION_NOT_REVIEWABLE"


# --- ensure_decision_is_current_source ------------------------


def _ids() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    return uuid.uuid4(), uuid.uuid4(), uuid.uuid4()


def test_current_chain_and_latest_decision_is_allowed() -> None:
    e, n, v = _ids()
    lifecycle.ensure_decision_is_current_source(
        decision_is_latest_for_validation=True,
        chain_extraction_id=e,
        chain_normalization_id=n,
        chain_validation_id=v,
        current_extraction_id=e,
        current_normalization_id=n,
        current_validation_id=v,
    )


@pytest.mark.parametrize("superseded", ["extraction", "normalization", "validation", "decision"])
def test_a_superseded_hop_is_stale(superseded: str) -> None:
    e, n, v = _ids()
    kwargs = dict(
        decision_is_latest_for_validation=True,
        chain_extraction_id=e,
        chain_normalization_id=n,
        chain_validation_id=v,
        current_extraction_id=e,
        current_normalization_id=n,
        current_validation_id=v,
    )
    if superseded == "extraction":
        kwargs["current_extraction_id"] = uuid.uuid4()
    elif superseded == "normalization":
        kwargs["current_normalization_id"] = uuid.uuid4()
    elif superseded == "validation":
        kwargs["current_validation_id"] = uuid.uuid4()
    else:
        kwargs["decision_is_latest_for_validation"] = False

    with pytest.raises(ConflictError) as err:
        lifecycle.ensure_decision_is_current_source(**kwargs)
    assert err.value.code == "STALE_DECISION_SOURCE"


def test_missing_current_chain_is_stale() -> None:
    e, n, v = _ids()
    with pytest.raises(ConflictError) as err:
        lifecycle.ensure_decision_is_current_source(
            decision_is_latest_for_validation=True,
            chain_extraction_id=e,
            chain_normalization_id=n,
            chain_validation_id=v,
            current_extraction_id=None,
            current_normalization_id=None,
            current_validation_id=None,
        )
    assert err.value.code == "STALE_DECISION_SOURCE"
