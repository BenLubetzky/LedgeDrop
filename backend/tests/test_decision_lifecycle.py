"""Tests for the Stage 6 decision lifecycle guards (package 4).

Pure functions - no database, no AI. They encode §6.1's transitions
(``COMPLETED validation -> PROCESSING -> COMPLETED | FAILED``, and, on
explicit retry of a technical ``FAILED``, again
``PROCESSING -> COMPLETED | FAILED``), §6.2's document-status mapping, and
§6.4's stale-source guard.
"""

from __future__ import annotations

import uuid
from typing import cast

import pytest

from app.core.errors import ConflictError
from app.models.decision import DecisionOutcome, DecisionStatus
from app.models.document import DocumentStatus
from app.models.validation import ValidationStatus
from app.services.processing.extraction.lifecycle import ensure_document_can_extract
from app.services.processing.decision.lifecycle import (
    Action,
    document_status_for_outcome,
    ensure_attempt_transition,
    ensure_validation_can_decide,
    ensure_validation_is_current_source,
)

VAL_COMPLETED = ValidationStatus.COMPLETED
VAL_PROCESSING = ValidationStatus.PROCESSING
VAL_FAILED = ValidationStatus.FAILED

D_PROCESSING = DecisionStatus.PROCESSING
D_COMPLETED = DecisionStatus.COMPLETED
D_FAILED = DecisionStatus.FAILED


# --- ensure_validation_can_decide --------------------------------------


@pytest.mark.parametrize("action", ["start", "retry"])
@pytest.mark.parametrize("validation_status", [VAL_PROCESSING, VAL_FAILED])
def test_source_validation_must_be_completed(action, validation_status) -> None:
    with pytest.raises(ConflictError) as exc:
        ensure_validation_can_decide(validation_status, None, action=action)
    assert exc.value.code == "VALIDATION_NOT_COMPLETED"


@pytest.mark.parametrize("action", ["start", "retry"])
def test_active_decision_blocks_any_action(action) -> None:
    with pytest.raises(ConflictError) as exc:
        ensure_validation_can_decide(VAL_COMPLETED, D_PROCESSING, action=action)
    assert exc.value.code == "DECISION_IN_PROGRESS"


def test_start_is_allowed_when_no_prior_attempt() -> None:
    ensure_validation_can_decide(VAL_COMPLETED, None, action="start")


def test_start_rejected_when_already_decided() -> None:
    with pytest.raises(ConflictError) as exc:
        ensure_validation_can_decide(VAL_COMPLETED, D_COMPLETED, action="start")
    assert exc.value.code == "VALIDATION_ALREADY_DECIDED"


def test_start_rejected_when_last_attempt_failed() -> None:
    with pytest.raises(ConflictError) as exc:
        ensure_validation_can_decide(VAL_COMPLETED, D_FAILED, action="start")
    assert exc.value.code == "DECISION_FAILED"


def test_retry_is_allowed_only_after_a_failed_attempt() -> None:
    ensure_validation_can_decide(VAL_COMPLETED, D_FAILED, action="retry")


@pytest.mark.parametrize("latest", [None, D_COMPLETED])
def test_retry_rejected_when_last_attempt_is_not_failed(latest) -> None:
    with pytest.raises(ConflictError) as exc:
        ensure_validation_can_decide(VAL_COMPLETED, latest, action="retry")
    assert exc.value.code == "DECISION_NOT_FAILED"


def test_unknown_action_is_rejected_instead_of_being_treated_as_retry() -> None:
    with pytest.raises(ValueError, match="unknown decision action"):
        ensure_validation_can_decide(VAL_COMPLETED, D_FAILED, action=cast(Action, "unknown"))


def test_conflict_errors_are_client_safe_409() -> None:
    with pytest.raises(ConflictError) as exc:
        ensure_validation_can_decide(VAL_FAILED, None, action="start")
    assert exc.value.status_code == 409
    for token in ("/", "\\", "Traceback", "app."):
        assert token not in exc.value.message


# --- ensure_attempt_transition -------------------------------------


@pytest.mark.parametrize("new", [D_COMPLETED, D_FAILED])
def test_processing_may_become_completed_or_failed(new) -> None:
    ensure_attempt_transition(D_PROCESSING, new)


@pytest.mark.parametrize(
    ("current", "new"),
    [
        (D_PROCESSING, D_PROCESSING),
        (D_COMPLETED, D_FAILED),
        (D_COMPLETED, D_PROCESSING),
        (D_FAILED, D_COMPLETED),
        (D_FAILED, D_PROCESSING),
        (D_FAILED, D_FAILED),
        (D_COMPLETED, D_COMPLETED),
    ],
)
def test_illegal_transitions_raise_value_error(current, new) -> None:
    with pytest.raises(ValueError):
        ensure_attempt_transition(current, new)


# --- document_status_for_outcome (§6.2) --------------------------------


def test_accepted_outcome_leaves_document_status_unchanged() -> None:
    assert document_status_for_outcome(DecisionOutcome.ACCEPTED) is None


def test_needs_review_outcome_maps_to_needs_review_status() -> None:
    assert document_status_for_outcome(DecisionOutcome.NEEDS_REVIEW) is DocumentStatus.NEEDS_REVIEW


def test_mapping_covers_every_outcome() -> None:
    for outcome in DecisionOutcome:
        document_status_for_outcome(outcome)  # must not raise


# --- ensure_validation_is_current_source (§6.4) ------------------------


def test_matching_chain_is_accepted() -> None:
    extraction_id = uuid.uuid4()
    normalization_id = uuid.uuid4()
    ensure_validation_is_current_source(
        chain_extraction_id=extraction_id,
        chain_normalization_id=normalization_id,
        current_extraction_id=extraction_id,
        current_normalization_id=normalization_id,
    )


def test_superseded_extraction_is_rejected() -> None:
    normalization_id = uuid.uuid4()
    with pytest.raises(ConflictError) as exc:
        ensure_validation_is_current_source(
            chain_extraction_id=uuid.uuid4(),
            chain_normalization_id=normalization_id,
            current_extraction_id=uuid.uuid4(),  # a different, newer extraction
            current_normalization_id=normalization_id,
        )
    assert exc.value.code == "STALE_VALIDATION_SOURCE"
    assert exc.value.status_code == 409


def test_superseded_normalization_is_rejected() -> None:
    extraction_id = uuid.uuid4()
    with pytest.raises(ConflictError) as exc:
        ensure_validation_is_current_source(
            chain_extraction_id=extraction_id,
            chain_normalization_id=uuid.uuid4(),
            current_extraction_id=extraction_id,
            current_normalization_id=uuid.uuid4(),  # a different, newer normalization
        )
    assert exc.value.code == "STALE_VALIDATION_SOURCE"


def test_no_current_chain_at_all_is_rejected() -> None:
    with pytest.raises(ConflictError) as exc:
        ensure_validation_is_current_source(
            chain_extraction_id=uuid.uuid4(),
            chain_normalization_id=uuid.uuid4(),
            current_extraction_id=None,
            current_normalization_id=None,
        )
    assert exc.value.code == "STALE_VALIDATION_SOURCE"


# --- extraction guard compatibility (§6.3) -----------------------------


def test_a_needs_review_document_cannot_start_extraction() -> None:
    # Confirms §6.3 by exercising the real Stage 3 guard: NEEDS_REVIEW is not
    # in START_FROM, so a start already falls through to the generic
    # DOCUMENT_NOT_EXTRACTABLE with no code change needed in Stage 3.
    with pytest.raises(ConflictError) as exc:
        ensure_document_can_extract(DocumentStatus.NEEDS_REVIEW, action="start")
    assert exc.value.code == "DOCUMENT_NOT_EXTRACTABLE"


def test_a_needs_review_document_cannot_retry_extraction() -> None:
    # NEEDS_REVIEW is not in RETRY_FROM either, so a retry is rejected too -
    # via EXTRACTION_NOT_FAILED (the retry branch's own code for "not
    # FAILED"), a different but equally safe 409 than the start case.
    with pytest.raises(ConflictError) as exc:
        ensure_document_can_extract(DocumentStatus.NEEDS_REVIEW, action="retry")
    assert exc.value.code == "EXTRACTION_NOT_FAILED"
