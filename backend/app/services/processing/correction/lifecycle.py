"""Stage 8 correction lifecycle - the valid transitions and the status matrix.

Correction-attempt lifecycle::

    correctable document -> (submit) -> PROCESSING -> COMPLETED
                                                 \\-> FAILED
    FAILED correction    -> (retry)  -> PROCESSING -> COMPLETED | FAILED

* ``PROCESSING`` is set only when a correction attempt actually begins, and
  the ``invoice_corrections`` row + its field entries are committed before any
  downstream (projection / validation / decision) work runs.
* A ``NEEDS_REVIEW`` re-decision is **data about the invoice** - it completes
  the attempt exactly like ``ACCEPTED``. Only a technical fault yields
  ``FAILED``.
* At most one ``PROCESSING`` correction attempt exists per document.
* No Stage 2-7 record and no stored file is ever changed. The one existing-row
  write is ``documents.status`` (Part 2.4), applied by the service.

Guards raise :class:`ConflictError` for an illegal caller-driven transition and
:class:`ValueError` for an illegal internal one.
"""

from __future__ import annotations

import uuid
from typing import Literal

from app.core.errors import ConflictError
from app.models.decision import DecisionOutcome, DecisionStatus
from app.models.document import DocumentStatus
from app.schemas.correction import CORRECTABLE_DOCUMENT_STATUSES, CorrectionStatus

Action = Literal["submit", "retry"]

_ATTEMPT_TRANSITIONS: dict[CorrectionStatus, frozenset[CorrectionStatus]] = {
    CorrectionStatus.PROCESSING: frozenset(
        {CorrectionStatus.COMPLETED, CorrectionStatus.FAILED}
    ),
    CorrectionStatus.COMPLETED: frozenset(),
    CorrectionStatus.FAILED: frozenset(),
}

# The Part 2.4 auto-accept / return-to-review matrix, as data so the service
# and the tests read one source of truth. Keyed by (outcome, origin_status).
_STATUS_MATRIX: dict[tuple[DecisionOutcome, DocumentStatus], DocumentStatus] = {
    (DecisionOutcome.ACCEPTED, DocumentStatus.COMPLETED): DocumentStatus.COMPLETED,
    (DecisionOutcome.ACCEPTED, DocumentStatus.NEEDS_REVIEW): DocumentStatus.APPROVED,
    (DecisionOutcome.ACCEPTED, DocumentStatus.APPROVED): DocumentStatus.APPROVED,
    (DecisionOutcome.ACCEPTED, DocumentStatus.REJECTED): DocumentStatus.APPROVED,
    (DecisionOutcome.NEEDS_REVIEW, DocumentStatus.COMPLETED): DocumentStatus.NEEDS_REVIEW,
    (DecisionOutcome.NEEDS_REVIEW, DocumentStatus.NEEDS_REVIEW): DocumentStatus.NEEDS_REVIEW,
    (DecisionOutcome.NEEDS_REVIEW, DocumentStatus.APPROVED): DocumentStatus.NEEDS_REVIEW,
    (DecisionOutcome.NEEDS_REVIEW, DocumentStatus.REJECTED): DocumentStatus.NEEDS_REVIEW,
}


def ensure_document_can_be_corrected(document_status: DocumentStatus) -> None:
    """Raise ``409 DOCUMENT_NOT_CORRECTABLE`` unless the status is correctable."""
    if document_status not in CORRECTABLE_DOCUMENT_STATUSES:
        raise ConflictError(
            "This document cannot be corrected in its current state.",
            code="DOCUMENT_NOT_CORRECTABLE",
        )


def ensure_source_decision_completed(decision_status: DecisionStatus | None) -> None:
    """Raise ``409 DECISION_NOT_COMPLETED`` unless the current chain has a
    ``COMPLETED`` decision to anchor and re-run from."""
    if decision_status is not DecisionStatus.COMPLETED:
        raise ConflictError(
            "This document has no completed decision to correct yet.",
            code="DECISION_NOT_COMPLETED",
        )


def ensure_decision_is_current_source(
    *,
    source_decision_id: uuid.UUID,
    current_decision_id: uuid.UUID | None,
    source_normalization_id: uuid.UUID | None,
    current_normalization_id: uuid.UUID | None,
) -> None:
    """Raise ``409 STALE_CORRECTION_SOURCE`` if the submitted anchor is not the
    document's current chain."""
    if source_decision_id != current_decision_id or (
        source_normalization_id is not None
        and source_normalization_id != current_normalization_id
    ):
        raise ConflictError(
            "This invoice has been corrected or reprocessed since you loaded it; "
            "reload the latest data and try again.",
            code="STALE_CORRECTION_SOURCE",
        )


def ensure_no_active_correction(active: object | None) -> None:
    """Raise ``409 CORRECTION_IN_PROGRESS`` if a correction is already running."""
    if active is not None:
        raise ConflictError(
            "A correction is already in progress for this document.",
            code="CORRECTION_IN_PROGRESS",
        )


def ensure_can_retry(latest_status: CorrectionStatus | None) -> None:
    """Raise ``409 CORRECTION_NOT_FAILED`` unless the latest attempt failed
    technically."""
    if latest_status is not CorrectionStatus.FAILED:
        raise ConflictError(
            "Only a correction that failed technically can be retried.",
            code="CORRECTION_NOT_FAILED",
        )


def ensure_attempt_transition(current: CorrectionStatus, new: CorrectionStatus) -> None:
    """Guard an internal attempt-status change; raise ``ValueError`` if invalid."""
    if new not in _ATTEMPT_TRANSITIONS.get(current, frozenset()):
        raise ValueError(
            f"illegal correction-attempt transition: {current.value} -> {new.value}"
        )


def resolve_document_status(
    *, origin_status: DocumentStatus, outcome: DecisionOutcome
) -> DocumentStatus:
    """The Part 2.4 document-status write for a completed correction re-run."""
    try:
        return _STATUS_MATRIX[(outcome, origin_status)]
    except KeyError as exc:  # pragma: no cover - origin_status is pre-validated
        raise ValueError(
            f"no status matrix cell for outcome={outcome} origin={origin_status}"
        ) from exc


__all__ = [
    "Action",
    "ensure_document_can_be_corrected",
    "ensure_source_decision_completed",
    "ensure_decision_is_current_source",
    "ensure_no_active_correction",
    "ensure_can_retry",
    "ensure_attempt_transition",
    "resolve_document_status",
]
