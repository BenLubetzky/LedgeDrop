"""Stage 5 validation lifecycle - the valid status transitions (step 10).

Validation-attempt lifecycle::

    COMPLETED normalization -> (start) -> PROCESSING -> COMPLETED
                                                   \\-> FAILED
    FAILED validation       -> (retry) -> PROCESSING -> COMPLETED | FAILED

* ``PROCESSING`` is set only when a validation attempt actually begins.
* A rule violation is **data about the invoice** - it produces a finding on a
  ``COMPLETED`` attempt and never makes the attempt ``FAILED``. Only a technical
  fault (the normalization attempt cannot be read, a rule function raises, a
  database write fails) yields ``FAILED``, with a client-safe
  ``failure_code`` / ``failure_message`` and no partial findings.
* At most one active (``PROCESSING``) validation attempt exists per source
  normalization attempt.
* A ``COMPLETED`` validation attempt is terminal; there is no re-validate route
  in the MVP (Stage 4 does not re-normalize a completed result). ``retry`` is
  only for a technically ``FAILED`` attempt.
* No Stage 2-4 record and no stored file is ever changed by validation - not on
  success and not on failure.

The guard functions raise :class:`ConflictError` for an illegal *caller-driven*
transition (something a client asked for) and :class:`ValueError` for an illegal
*internal* one (a bug in the orchestration).
"""

from __future__ import annotations

from typing import Literal

from app.core.errors import ConflictError
from app.models.normalization import NormalizationStatus
from app.models.validation import ValidationStatus

Action = Literal["start", "retry"]

# Terminal validation status reachable from PROCESSING.
_ATTEMPT_TRANSITIONS: dict[ValidationStatus, frozenset[ValidationStatus]] = {
    ValidationStatus.PROCESSING: frozenset(
        {ValidationStatus.COMPLETED, ValidationStatus.FAILED}
    ),
    ValidationStatus.COMPLETED: frozenset(),
    ValidationStatus.FAILED: frozenset(),
}


def ensure_normalization_can_validate(
    normalization_status: NormalizationStatus,
    latest_validation_status: ValidationStatus | None,
    *,
    action: Action,
) -> None:
    """Raise :class:`ConflictError` unless ``action`` may begin now.

    Gating combines the source normalization attempt's status with the newest
    validation attempt for that normalization. The error code lets the API
    translate the reason without re-deriving it:

    * ``NORMALIZATION_NOT_COMPLETED`` - the source normalization is not
      ``COMPLETED`` (it is ``PROCESSING`` or technically ``FAILED``), so there
      is nothing stable to validate.
    * ``VALIDATION_IN_PROGRESS`` - a validation attempt is already
      ``PROCESSING``.
    * ``NORMALIZATION_ALREADY_VALIDATED`` - a ``start`` when a ``COMPLETED``
      validation attempt already exists (a completed attempt is not re-run).
    * ``VALIDATION_FAILED`` - a ``start`` when the latest attempt is a technical
      failure; retry it instead of starting a new one.
    * ``VALIDATION_NOT_FAILED`` - a ``retry`` when the latest attempt is not a
      technical failure, or there is no attempt yet.
    """
    if action not in ("start", "retry"):
        raise ValueError(f"unknown validation action: {action!r}")

    if normalization_status is not NormalizationStatus.COMPLETED:
        raise ConflictError(
            "The source normalization has not completed; it cannot be validated yet.",
            code="NORMALIZATION_NOT_COMPLETED",
        )

    if latest_validation_status is ValidationStatus.PROCESSING:
        raise ConflictError(
            "A validation is already in progress for this normalization.",
            code="VALIDATION_IN_PROGRESS",
        )

    if action == "start":
        if latest_validation_status is ValidationStatus.COMPLETED:
            raise ConflictError(
                "This normalization has already been validated.",
                code="NORMALIZATION_ALREADY_VALIDATED",
            )
        if latest_validation_status is ValidationStatus.FAILED:
            raise ConflictError(
                "This normalization's last validation failed; retry it instead of "
                "starting a new one.",
                code="VALIDATION_FAILED",
            )
        return

    # action == "retry"
    if latest_validation_status is not ValidationStatus.FAILED:
        raise ConflictError(
            "Only a validation attempt that failed technically can be retried.",
            code="VALIDATION_NOT_FAILED",
        )


def ensure_attempt_transition(
    current: ValidationStatus, new: ValidationStatus
) -> None:
    """Guard an internal attempt-status change; raise ``ValueError`` if invalid."""
    if new not in _ATTEMPT_TRANSITIONS.get(current, frozenset()):
        raise ValueError(
            f"illegal validation-attempt transition: {current.value} -> {new.value}"
        )


__all__ = [
    "Action",
    "ensure_normalization_can_validate",
    "ensure_attempt_transition",
]
