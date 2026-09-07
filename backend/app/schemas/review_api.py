"""Request and public response schemas for the Stage 7 review API (package 2).

Three shapes:

* :class:`ReviewSubmitRequest` - the body for ``POST .../decisions/{did}/review``.
  It *is* the internal contract (:class:`app.schemas.review.InvoiceReview`):
  the same closed ``action``, the same trimmed/bounded ``reviewer_name``, and
  the same "``note`` required for ``REJECT``" rule, with unknown keys rejected.
* :class:`InvoiceReviewResult` - the client-facing view of one recorded
  resolution. It exposes the action, who recorded it (an explicitly
  *unverified* label), the optional note, the policy version, and the
  timestamps - and the owning ``document_id`` so a caller can follow the
  resolution back to the document. No internal diagnostics, paths, hashes, or
  raw payloads.
* :class:`ReviewQueueEntry` - one row of the pending-review queue: the document
  awaiting a decision, when it was flagged, the ordered Stage 6 decision
  reasons that explain why (reused verbatim), and the chain ids needed to
  build the decision-scoped review URL.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from pydantic import AwareDatetime, BaseModel, ConfigDict, field_serializer

from app.models.review import ReviewAction
from app.schemas.decision import InvoiceDecision
from app.schemas.decision_persistence import invoice_decision_from_rows
from app.schemas.review import InvoiceReview


def _utc_z(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class ReviewSubmitRequest(InvoiceReview):
    """Body for ``POST .../decisions/{decision_id}/review``.

    Inherits every field and validator from
    :class:`app.schemas.review.InvoiceReview` (including ``extra="forbid"``):
    ``action`` is ``APPROVE`` or ``REJECT``; ``reviewer_name`` is required,
    trimmed, and 1-200 chars; ``note`` is required and non-blank for a
    ``REJECT`` and optional (but non-blank if given) for an ``APPROVE``. An
    empty body is rejected - a review must at least name an action and a
    reviewer.
    """


class InvoiceReviewResult(BaseModel):
    """Client-facing view of one recorded human resolution."""

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    review_id: uuid.UUID
    decision_id: uuid.UUID
    document_id: uuid.UUID
    action: ReviewAction
    reviewer_name: str
    note: str | None
    policy_version: str
    reviewed_at: AwareDatetime
    created_at: AwareDatetime

    @field_serializer("reviewed_at", "created_at")
    def _serialize_utc(self, value: datetime | None) -> str | None:
        return _utc_z(value)

    @classmethod
    def from_record(
        cls, record: object, *, document_id: uuid.UUID
    ) -> "InvoiceReviewResult":
        """Build the public result from a persisted ``ReviewRecord``.

        ``document_id`` is supplied by the caller - the review row itself only
        stores ``decision_id``; the API layer already knows the document from
        the request path or resolves it alongside the record.
        """
        return cls.model_validate(
            {
                "review_id": record.review_id,
                "decision_id": record.decision_id,
                "document_id": document_id,
                "action": record.action,
                "reviewer_name": record.reviewer_name,
                "note": record.note,
                "policy_version": record.policy_version,
                "reviewed_at": record.reviewed_at,
                "created_at": record.created_at,
            }
        )


class ReviewQueueEntry(BaseModel):
    """One pending item in the human-review queue."""

    model_config = ConfigDict(extra="forbid")

    document_id: uuid.UUID
    original_filename: str
    uploaded_at: AwareDatetime

    extraction_id: uuid.UUID
    normalization_id: uuid.UUID
    validation_id: uuid.UUID
    decision_id: uuid.UUID

    decided_at: AwareDatetime
    decision: InvoiceDecision

    @field_serializer("uploaded_at", "decided_at")
    def _serialize_utc(self, value: datetime | None) -> str | None:
        return _utc_z(value)

    @classmethod
    def from_row(
        cls,
        document: object,
        decision: object,
        normalization_id: uuid.UUID,
        extraction_id: uuid.UUID,
    ) -> "ReviewQueueEntry":
        """Build a queue entry from one ``ReviewRepository.queue`` row.

        ``decision.reasons`` are already loaded (the query eager-loads them);
        they are rebuilt into the Stage 6 contract - and re-validated - so the
        entry shows exactly why the invoice was flagged, in Stage 5 order.
        """
        return cls.model_validate(
            {
                "document_id": document.document_id,
                "original_filename": document.original_filename,
                "uploaded_at": document.uploaded_at,
                "extraction_id": extraction_id,
                "normalization_id": normalization_id,
                "validation_id": decision.validation_id,
                "decision_id": decision.decision_id,
                "decided_at": decision.completed_at,
                "decision": invoice_decision_from_rows(decision.reasons),
            }
        )


__all__ = ["ReviewSubmitRequest", "InvoiceReviewResult", "ReviewQueueEntry"]
