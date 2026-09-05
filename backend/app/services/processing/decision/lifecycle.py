"""Stage 6 decision lifecycle - the valid status transitions (package 4).

Decision-attempt lifecycle::

    COMPLETED validation -> (start) -> PROCESSING -> COMPLETED
                                                \\-> FAILED
    FAILED decision      -> (retry) -> PROCESSING -> COMPLETED | FAILED

* ``PROCESSING`` is set only when a decision attempt actually begins.
* A ``NEEDS_REVIEW`` outcome is **data about the invoice** - it completes the
  attempt exactly like ``ACCEPTED`` and never makes the attempt ``FAILED``.
  Only a technical fault (the validation attempt cannot be read, the engine
  raises, a database write fails) yields ``FAILED``, with a client-safe
  ``failure_code`` / ``failure_message`` and no partial reasons.
* At most one active (``PROCESSING``) decision attempt exists per source
  validation attempt.
* A ``COMPLETED`` decision attempt is terminal; there is no re-decide route
  (Stage 5 has no re-validate route, so there is nothing new to decide
  from). ``retry`` is only for a technically ``FAILED`` attempt.
* A decision can only be based on the document's *current* processing chain
  (``docs/stage-6-decision.md`` §6.4) - a validation whose extraction or
  normalization has been superseded is a stale source, never a decidable one.
* No Stage 2-5 record and no stored file is ever changed by a decision - not
  on success and not on failure. The one thing Stage 6 *does* write is
  ``documents.status``, and only to ``NEEDS_REVIEW`` (§6.2).

The guard functions raise :class:`ConflictError` for an illegal *caller-driven*
transition (something a client asked for) and :class:`ValueError` for an illegal
*internal* one (a bug in the orchestration).
"""

from __future__ import annotations

import uuid
from typing import Literal

from app.core.errors import ConflictError
from app.models.decision import DecisionOutcome, DecisionStatus
from app.models.document import DocumentStatus
from app.models.validation import ValidationStatus

Action = Literal["start", "retry"]

# Terminal decision status reachable from PROCESSING.
_ATTEMPT_TRANSITIONS: dict[DecisionStatus, frozenset[DecisionStatus]] = {
    DecisionStatus.PROCESSING: frozenset({DecisionStatus.COMPLETED, DecisionStatus.FAILED}),
    DecisionStatus.COMPLETED: frozenset(),
    DecisionStatus.FAILED: frozenset(),
}

# The §6.2 mapping. ``None`` means "leave documents.status unchanged" - the
# ACCEPTED case, where COMPLETED already carries the right (extraction-only)
# meaning, and the technical-failure case, handled by never calling this at
# all when an attempt ends FAILED (see DecisionService._mark_failed).
_DOCUMENT_STATUS_FOR_OUTCOME: dict[DecisionOutcome, DocumentStatus | None] = {
    DecisionOutcome.ACCEPTED: None,
    DecisionOutcome.NEEDS_REVIEW: DocumentStatus.NEEDS_REVIEW,
}


def document_status_for_outcome(outcome: DecisionOutcome) -> DocumentStatus | None:
    """The §6.2 document-status write for a completed decision, or ``None``.

    ``None`` means "write nothing" - the document's status is left exactly as
    it already was.
    """
    return _DOCUMENT_STATUS_FOR_OUTCOME[outcome]


def ensure_validation_can_decide(
    validation_status: ValidationStatus,
    latest_decision_status: DecisionStatus | None,
    *,
    action: Action,
) -> None:
    """Raise :class:`ConflictError` unless ``action`` may begin now.

    Gating combines the source validation attempt's status with the newest
    decision attempt for that validation. The error code lets the API
    translate the reason without re-deriving it:

    * ``VALIDATION_NOT_COMPLETED`` - the source validation is not
      ``COMPLETED`` (it is ``PROCESSING`` or technically ``FAILED``), so
      there is nothing stable to decide.
    * ``DECISION_IN_PROGRESS`` - a decision attempt is already ``PROCESSING``.
    * ``VALIDATION_ALREADY_DECIDED`` - a ``start`` when a ``COMPLETED``
      decision attempt already exists (a completed attempt is not re-run,
      regardless of its outcome).
    * ``DECISION_FAILED`` - a ``start`` when the latest attempt is a
      technical failure; retry it instead of starting a new one.
    * ``DECISION_NOT_FAILED`` - a ``retry`` when the latest attempt is not a
      technical failure, or there is no attempt yet.
    """
    if action not in ("start", "retry"):
        raise ValueError(f"unknown decision action: {action!r}")

    if validation_status is not ValidationStatus.COMPLETED:
        raise ConflictError(
            "The source validation has not completed; it cannot be decided yet.",
            code="VALIDATION_NOT_COMPLETED",
        )

    if latest_decision_status is DecisionStatus.PROCESSING:
        raise ConflictError(
            "A decision is already in progress for this validation.",
            code="DECISION_IN_PROGRESS",
        )

    if action == "start":
        if latest_decision_status is DecisionStatus.COMPLETED:
            raise ConflictError(
                "This validation has already been decided.",
                code="VALIDATION_ALREADY_DECIDED",
            )
        if latest_decision_status is DecisionStatus.FAILED:
            raise ConflictError(
                "This validation's last decision failed; retry it instead of "
                "starting a new one.",
                code="DECISION_FAILED",
            )
        return

    # action == "retry"
    if latest_decision_status is not DecisionStatus.FAILED:
        raise ConflictError(
            "Only a decision attempt that failed technically can be retried.",
            code="DECISION_NOT_FAILED",
        )


def ensure_validation_is_current_source(
    *,
    chain_extraction_id: uuid.UUID,
    chain_normalization_id: uuid.UUID,
    current_extraction_id: uuid.UUID | None,
    current_normalization_id: uuid.UUID | None,
) -> None:
    """Raise :class:`ConflictError` if the validation's chain is superseded.

    ``chain_extraction_id`` / ``chain_normalization_id`` name the extraction
    and normalization the validation under decision actually came from;
    ``current_extraction_id`` / ``current_normalization_id`` name the
    document's latest extraction attempt and that extraction's latest
    normalization attempt, regardless of status. A decision may only be based
    on the document's current chain (§6.4) - not reachable through today's
    API (a document has at most one ever-``COMPLETED`` extraction,
    normalization, and validation), but checked anyway as a defensive
    invariant.
    """
    if (
        chain_extraction_id != current_extraction_id
        or chain_normalization_id != current_normalization_id
    ):
        raise ConflictError(
            "This validation is no longer the document's current result; a "
            "decision cannot be based on it.",
            code="STALE_VALIDATION_SOURCE",
        )


def ensure_attempt_transition(current: DecisionStatus, new: DecisionStatus) -> None:
    """Guard an internal attempt-status change; raise ``ValueError`` if invalid."""
    if new not in _ATTEMPT_TRANSITIONS.get(current, frozenset()):
        raise ValueError(f"illegal decision-attempt transition: {current.value} -> {new.value}")


__all__ = [
    "Action",
    "document_status_for_outcome",
    "ensure_validation_can_decide",
    "ensure_validation_is_current_source",
    "ensure_attempt_transition",
]
