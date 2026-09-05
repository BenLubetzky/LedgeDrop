"""Tests for the Stage 5 validation lifecycle guards (step 10).

Pure functions - no database, no AI. They encode the §1.5 transitions:
``COMPLETED normalization -> PROCESSING -> COMPLETED | FAILED`` and, on explicit
retry of a technical ``FAILED``, again ``PROCESSING -> COMPLETED | FAILED``.
"""

from __future__ import annotations

from typing import cast

import pytest

from app.core.errors import ConflictError
from app.models.normalization import NormalizationStatus
from app.models.validation import ValidationStatus
from app.services.processing.validation.lifecycle import (
    Action,
    ensure_attempt_transition,
    ensure_normalization_can_validate,
)

N_COMPLETED = NormalizationStatus.COMPLETED
N_PROCESSING = NormalizationStatus.PROCESSING
N_FAILED = NormalizationStatus.FAILED

V_PROCESSING = ValidationStatus.PROCESSING
V_COMPLETED = ValidationStatus.COMPLETED
V_FAILED = ValidationStatus.FAILED


# --- ensure_normalization_can_validate --------------------------------


@pytest.mark.parametrize("action", ["start", "retry"])
@pytest.mark.parametrize("norm_status", [N_PROCESSING, N_FAILED])
def test_source_normalization_must_be_completed(action, norm_status) -> None:
    with pytest.raises(ConflictError) as exc:
        ensure_normalization_can_validate(norm_status, None, action=action)
    assert exc.value.code == "NORMALIZATION_NOT_COMPLETED"


@pytest.mark.parametrize("action", ["start", "retry"])
def test_active_validation_blocks_any_action(action) -> None:
    with pytest.raises(ConflictError) as exc:
        ensure_normalization_can_validate(N_COMPLETED, V_PROCESSING, action=action)
    assert exc.value.code == "VALIDATION_IN_PROGRESS"


def test_start_is_allowed_when_no_prior_attempt() -> None:
    ensure_normalization_can_validate(N_COMPLETED, None, action="start")


def test_start_rejected_when_already_completed() -> None:
    with pytest.raises(ConflictError) as exc:
        ensure_normalization_can_validate(N_COMPLETED, V_COMPLETED, action="start")
    assert exc.value.code == "NORMALIZATION_ALREADY_VALIDATED"


def test_start_rejected_when_last_attempt_failed() -> None:
    with pytest.raises(ConflictError) as exc:
        ensure_normalization_can_validate(N_COMPLETED, V_FAILED, action="start")
    assert exc.value.code == "VALIDATION_FAILED"


def test_retry_is_allowed_only_after_a_failed_attempt() -> None:
    ensure_normalization_can_validate(N_COMPLETED, V_FAILED, action="retry")


@pytest.mark.parametrize("latest", [None, V_COMPLETED])
def test_retry_rejected_when_last_attempt_is_not_failed(latest) -> None:
    with pytest.raises(ConflictError) as exc:
        ensure_normalization_can_validate(N_COMPLETED, latest, action="retry")
    assert exc.value.code == "VALIDATION_NOT_FAILED"


def test_unknown_action_is_rejected_instead_of_being_treated_as_retry() -> None:
    with pytest.raises(ValueError, match="unknown validation action"):
        ensure_normalization_can_validate(
            N_COMPLETED,
            V_FAILED,
            action=cast(Action, "unknown"),
        )


def test_conflict_errors_are_client_safe_409() -> None:
    with pytest.raises(ConflictError) as exc:
        ensure_normalization_can_validate(N_FAILED, None, action="start")
    assert exc.value.status_code == 409
    for token in ("/", "\\", "Traceback", "app."):
        assert token not in exc.value.message


# --- ensure_attempt_transition -------------------------------------


@pytest.mark.parametrize("new", [V_COMPLETED, V_FAILED])
def test_processing_may_become_completed_or_failed(new) -> None:
    ensure_attempt_transition(V_PROCESSING, new)


@pytest.mark.parametrize(
    ("current", "new"),
    [
        (V_PROCESSING, V_PROCESSING),
        (V_COMPLETED, V_FAILED),
        (V_COMPLETED, V_PROCESSING),
        (V_FAILED, V_COMPLETED),
        (V_FAILED, V_PROCESSING),
        (V_FAILED, V_FAILED),
        (V_COMPLETED, V_COMPLETED),
    ],
)
def test_illegal_transitions_raise_value_error(current, new) -> None:
    with pytest.raises(ValueError):
        ensure_attempt_transition(current, new)
