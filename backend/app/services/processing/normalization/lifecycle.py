"""Stage 4 normalization lifecycle - the valid status transitions.

Normalization-attempt lifecycle::

    COMPLETED extraction -> (start) -> PROCESSING -> COMPLETED
                                                 \\-> FAILED
    FAILED normalization -> (retry) -> PROCESSING -> COMPLETED | FAILED

* ``PROCESSING`` is set only when a normalization attempt actually begins.
* A field-level normalization error is *data about the invoice*; it never makes
  an attempt ``FAILED``. Only a technical failure does.
* At most one active (``PROCESSING``) attempt exists per source extraction.
* The source ``invoice_extractions`` row and the original PDF are never changed
  by normalization - not on success and not on failure.

The guard functions raise :class:`ConflictError` for an illegal *caller-driven*
transition (something a client asked for) and :class:`ValueError` for an illegal
*internal* one (a bug in the orchestration).
"""

from __future__ import annotations

from typing import Literal

from app.core.errors import ConflictError
from app.models.extraction import ExtractionStatus
from app.models.normalization import NormalizationStatus

Action = Literal["start", "retry"]

# Terminal normalization status reachable from PROCESSING.
_ATTEMPT_TRANSITIONS: dict[NormalizationStatus, frozenset[NormalizationStatus]] = {
    NormalizationStatus.PROCESSING: frozenset(
        {NormalizationStatus.COMPLETED, NormalizationStatus.FAILED}
    ),
    NormalizationStatus.COMPLETED: frozenset(),
    NormalizationStatus.FAILED: frozenset(),
}


def ensure_extraction_can_normalize(
    extraction_status: ExtractionStatus,
    latest_attempt_status: NormalizationStatus | None,
    *,
    action: Action,
) -> None:
    """Raise :class:`ConflictError` unless ``action`` may begin now.

    Gating combines the source extraction's status with the newest
    normalization attempt for that extraction. The error code lets the API
    translate the reason without re-deriving it:

    * ``EXTRACTION_NOT_COMPLETED`` - the source extraction has not completed, so
      there is nothing stable to normalize.
    * ``NORMALIZATION_IN_PROGRESS`` - an attempt is already ``PROCESSING``.
    * ``EXTRACTION_ALREADY_NORMALIZED`` - a ``start`` when a ``COMPLETED``
      attempt already exists (a completed attempt is not retried).
    * ``NORMALIZATION_FAILED`` - a ``start`` when the latest attempt is a
      technical failure; retry it instead of starting a new one.
    * ``NORMALIZATION_NOT_FAILED`` - a ``retry`` when the latest attempt is not
      a technical failure, or there is no attempt yet.
    """
    if extraction_status is not ExtractionStatus.COMPLETED:
        raise ConflictError(
            "The source extraction has not completed; it cannot be normalized yet.",
            code="EXTRACTION_NOT_COMPLETED",
        )

    if latest_attempt_status is NormalizationStatus.PROCESSING:
        raise ConflictError(
            "A normalization is already in progress for this extraction.",
            code="NORMALIZATION_IN_PROGRESS",
        )

    if action == "start":
        if latest_attempt_status is NormalizationStatus.COMPLETED:
            raise ConflictError(
                "This extraction has already been normalized.",
                code="EXTRACTION_ALREADY_NORMALIZED",
            )
        if latest_attempt_status is NormalizationStatus.FAILED:
            raise ConflictError(
                "This extraction's last normalization failed; retry it instead of "
                "starting a new one.",
                code="NORMALIZATION_FAILED",
            )
        return

    # action == "retry"
    if latest_attempt_status is not NormalizationStatus.FAILED:
        raise ConflictError(
            "Only a normalization attempt that failed technically can be retried.",
            code="NORMALIZATION_NOT_FAILED",
        )


def ensure_attempt_transition(
    current: NormalizationStatus, new: NormalizationStatus
) -> None:
    """Guard an internal attempt-status change; raise ``ValueError`` if invalid."""
    if new not in _ATTEMPT_TRANSITIONS.get(current, frozenset()):
        raise ValueError(
            f"illegal normalization-attempt transition: {current.value} -> {new.value}"
        )


__all__ = [
    "Action",
    "ensure_extraction_can_normalize",
    "ensure_attempt_transition",
]
