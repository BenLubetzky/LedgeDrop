"""Stage 7 review lifecycle - the guards that gate a human resolution (package 2).

A review is a **single terminal event**, not an attempt with a status::

    COMPLETED, NEEDS_REVIEW decision  ->  (submit APPROVE | REJECT)  ->  one review row
                                                                        + documents.status
                                                                          NEEDS_REVIEW -> APPROVED | REJECTED

So, unlike Stage 3-6, there is no ``PROCESSING`` review, no technical
``FAILED`` review row, and no retry route: a resolution either commits whole or
never happened, and "retry" is simply calling submit again (nothing was
written). The guards here therefore answer one question - *may this decision be
resolved right now?* - and nothing about attempt transitions.

* ``DECISION_NOT_REVIEWABLE`` - the target decision attempt is not a
  ``COMPLETED`` decision whose ``outcome`` is ``NEEDS_REVIEW`` (it is still
  ``PROCESSING``, it failed technically, or it was ``ACCEPTED``). A Stage 6
  ``ACCEPTED`` result is deliberately **not** overridable here.
* ``DECISION_ALREADY_REVIEWED`` - a review row already exists for this
  decision. One review per decision (``docs/stage-7-review.md`` Part 2.7); a
  resolved item is terminal.
* ``STALE_DECISION_SOURCE`` - the decision is no longer the document's current
  extraction -> normalization -> validation -> decision chain. Defensive: not
  reachable through today's API (a document has at most one ever-``COMPLETED``
  chain), but checked so the invariant is explicit, mirroring Stage 6's
  ``STALE_VALIDATION_SOURCE``.

Every guard raises :class:`ConflictError` (HTTP 409) for an illegal
caller-driven request; there is no internal-transition guard because there is
no internal transition.
"""

from __future__ import annotations

import uuid

from app.core.errors import ConflictError
from app.models.decision import DecisionOutcome, DecisionStatus
from app.models.document import DocumentStatus
from app.schemas.review import DOCUMENT_STATUS_FOR_ACTION, ReviewAction

__all__ = [
    "ensure_decision_can_be_reviewed",
    "ensure_decision_is_current_source",
    "ensure_document_awaiting_review",
    "document_status_for_action",
]


def document_status_for_action(action: ReviewAction) -> DocumentStatus:
    """The terminal ``documents.status`` a resolution writes (spec Part 1.3)."""
    return DOCUMENT_STATUS_FOR_ACTION[action]


def ensure_decision_can_be_reviewed(
    *,
    decision_status: DecisionStatus,
    decision_outcome: DecisionOutcome | None,
    already_reviewed: bool,
) -> None:
    """Raise unless this decision attempt may enter human review now."""
    if (
        decision_status is not DecisionStatus.COMPLETED
        or decision_outcome is not DecisionOutcome.NEEDS_REVIEW
    ):
        raise ConflictError(
            "This decision is not awaiting human review.",
            code="DECISION_NOT_REVIEWABLE",
        )
    if already_reviewed:
        raise ConflictError(
            "This decision has already been reviewed.",
            code="DECISION_ALREADY_REVIEWED",
        )


def ensure_document_awaiting_review(document_status: DocumentStatus) -> None:
    """Raise unless the owning document is still ``NEEDS_REVIEW``.

    A completed, unreviewed ``NEEDS_REVIEW`` decision should always sit on a
    ``NEEDS_REVIEW`` document; this catches an inconsistent state (for example
    a document already ``APPROVED``/``REJECTED`` with no review row) rather
    than silently writing a second terminal status.
    """
    if document_status is not DocumentStatus.NEEDS_REVIEW:
        raise ConflictError(
            "This document is not awaiting review.",
            code="DECISION_NOT_REVIEWABLE",
        )


def ensure_decision_is_current_source(
    *,
    decision_is_latest_for_validation: bool,
    chain_extraction_id: uuid.UUID,
    chain_normalization_id: uuid.UUID,
    chain_validation_id: uuid.UUID,
    current_extraction_id: uuid.UUID | None,
    current_normalization_id: uuid.UUID | None,
    current_validation_id: uuid.UUID | None,
) -> None:
    """Raise :class:`ConflictError` if the decision's chain has been superseded.

    ``chain_*`` name the extraction, normalization, and validation the decision
    under review actually derives from; ``current_*`` name the document's
    latest extraction attempt, that extraction's latest normalization, and
    that normalization's latest validation (regardless of status).
    ``decision_is_latest_for_validation`` guards the last hop - a decision that
    a newer attempt has superseded is stale too.
    """
    if not decision_is_latest_for_validation or (
        chain_extraction_id != current_extraction_id
        or chain_normalization_id != current_normalization_id
        or chain_validation_id != current_validation_id
    ):
        raise ConflictError(
            "This decision is no longer the document's current result; it "
            "cannot be reviewed.",
            code="STALE_DECISION_SOURCE",
        )
