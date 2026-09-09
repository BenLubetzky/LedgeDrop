"""Pure tests for the Stage 8 correction lifecycle guards (no database)."""

from __future__ import annotations

import uuid

import pytest

from app.core.errors import ConflictError
from app.models.decision import DecisionStatus
from app.models.document import DocumentStatus
from app.schemas.correction import CorrectionStatus
from app.services.processing.correction import lifecycle


def test_only_the_four_terminal_statuses_are_correctable() -> None:
    for ok in (
        DocumentStatus.NEEDS_REVIEW,
        DocumentStatus.COMPLETED,
        DocumentStatus.APPROVED,
        DocumentStatus.REJECTED,
    ):
        lifecycle.ensure_document_can_be_corrected(ok)
    for bad in (
        DocumentStatus.UPLOADED,
        DocumentStatus.PROCESSING,
        DocumentStatus.FAILED,
    ):
        with pytest.raises(ConflictError) as exc:
            lifecycle.ensure_document_can_be_corrected(bad)
        assert exc.value.code == "DOCUMENT_NOT_CORRECTABLE"


def test_source_decision_must_be_completed() -> None:
    lifecycle.ensure_source_decision_completed(DecisionStatus.COMPLETED)
    for bad in (None, DecisionStatus.PROCESSING, DecisionStatus.FAILED):
        with pytest.raises(ConflictError) as exc:
            lifecycle.ensure_source_decision_completed(bad)
        assert exc.value.code == "DECISION_NOT_COMPLETED"


def test_stale_source_is_detected_on_decision_or_normalization_mismatch() -> None:
    d, n = uuid.uuid4(), uuid.uuid4()
    lifecycle.ensure_decision_is_current_source(
        source_decision_id=d,
        current_decision_id=d,
        source_normalization_id=n,
        current_normalization_id=n,
    )
    # a None source_normalization_id skips that half of the check
    lifecycle.ensure_decision_is_current_source(
        source_decision_id=d,
        current_decision_id=d,
        source_normalization_id=None,
        current_normalization_id=uuid.uuid4(),
    )
    with pytest.raises(ConflictError) as exc:
        lifecycle.ensure_decision_is_current_source(
            source_decision_id=d,
            current_decision_id=uuid.uuid4(),
            source_normalization_id=None,
            current_normalization_id=None,
        )
    assert exc.value.code == "STALE_CORRECTION_SOURCE"
    with pytest.raises(ConflictError):
        lifecycle.ensure_decision_is_current_source(
            source_decision_id=d,
            current_decision_id=d,
            source_normalization_id=n,
            current_normalization_id=uuid.uuid4(),
        )


def test_one_active_correction_per_document() -> None:
    lifecycle.ensure_no_active_correction(None)
    with pytest.raises(ConflictError) as exc:
        lifecycle.ensure_no_active_correction(object())
    assert exc.value.code == "CORRECTION_IN_PROGRESS"


def test_retry_needs_a_failed_attempt() -> None:
    lifecycle.ensure_can_retry(CorrectionStatus.FAILED)
    for bad in (None, CorrectionStatus.PROCESSING, CorrectionStatus.COMPLETED):
        with pytest.raises(ConflictError) as exc:
            lifecycle.ensure_can_retry(bad)
        assert exc.value.code == "CORRECTION_NOT_FAILED"


def test_attempt_transitions_are_one_way() -> None:
    lifecycle.ensure_attempt_transition(
        CorrectionStatus.PROCESSING, CorrectionStatus.COMPLETED
    )
    lifecycle.ensure_attempt_transition(
        CorrectionStatus.PROCESSING, CorrectionStatus.FAILED
    )
    with pytest.raises(ValueError):
        lifecycle.ensure_attempt_transition(
            CorrectionStatus.COMPLETED, CorrectionStatus.PROCESSING
        )
    with pytest.raises(ValueError):
        lifecycle.ensure_attempt_transition(
            CorrectionStatus.FAILED, CorrectionStatus.COMPLETED
        )
