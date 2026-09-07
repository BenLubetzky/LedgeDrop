"""Internal human-review data contract (Stage 7, package 1).

This module is pure data definition. It describes the shape of a **human
resolution** of an invoice that Stage 6 routed to review: an action
(``APPROVE`` / ``REJECT``), who recorded it, and an optional or required note.
**No AI provider is called here, no external-network call is made, and there is
no database access.**

It is the *internal* contract - not the public API request/response (package 2)
and not a database row (:mod:`app.models.review`). The full boundary and the
pinned review policy live in ``docs/stage-7-review.md``.

Boundary (spec Part 1): **Stage 7 records a verdict on an existing Stage 6
decision; it does not recompute anything upstream and it never touches a
Stage 2-6 record.** A review is a single terminal event - there is no
``PROCESSING`` review, no technical ``FAILED`` review, and no retry chain, so
this contract carries no attempt-status field (contrast
:class:`app.schemas.decision.DecisionStatus`).

Pinned policy this contract enforces (spec Part 2):

* exactly two actions, ``APPROVE`` and ``REJECT``;
* a **required** ``reviewer_name`` - a free-text, explicitly *unverified*
  label (LedgerDrop has no authentication yet), trimmed and length-bounded,
  never used for authorization;
* a ``note`` that is **required and non-blank for ``REJECT``** and optional
  (but non-blank if given) for ``APPROVE``.

Unknown keys are rejected (``extra="forbid"``) on every model.
"""

from __future__ import annotations

import uuid
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.document import DocumentStatus

__all__ = [
    "ReviewAction",
    "InvoiceReview",
    "ReviewedInvoiceResult",
    "REVIEW_POLICY_VERSION",
    "DOCUMENT_STATUS_FOR_ACTION",
    "REVIEW_ACTIONS",
    "REVIEW_FIELD_NAMES",
    "REVIEWER_NAME_MAX_LENGTH",
    "NOTE_MAX_LENGTH",
]


# The named revision of the Part 2 review policy (note rule, attribution rule,
# status map). Stamped onto every persisted review at write time and never
# changed afterwards, so a historical resolution stays explicable even if the
# policy is retuned later - mirroring decision_catalogue.POLICY_VERSION.
REVIEW_POLICY_VERSION = "2026-09-06"

REVIEWER_NAME_MAX_LENGTH = 200
NOTE_MAX_LENGTH = 4000


class ReviewAction(str, Enum):
    """What a human decided about a queued invoice.

    Exactly two members. There is deliberately no "defer" / "request changes" /
    "escalate": the MVP queue is resolve-or-nothing.
    """

    APPROVE = "APPROVE"
    REJECT = "REJECT"


class InvoiceReview(BaseModel):
    """One recorded human resolution.

    Identity-free, mirroring :class:`app.schemas.decision.InvoiceDecision`: the
    link to the source decision attempt is on
    :class:`ReviewedInvoiceResult`, and the persistence identity (review id,
    policy version, timestamps) is added by :mod:`app.models.review`.
    """

    model_config = ConfigDict(extra="forbid")

    action: ReviewAction
    # A free-text, UNVERIFIED label - see the module docstring. Trimmed; the
    # trimmed value is what downstream code stores and returns.
    reviewer_name: str = Field(min_length=1, max_length=REVIEWER_NAME_MAX_LENGTH)
    # Required and non-blank for REJECT (see _check_note_required_for_reject);
    # optional for APPROVE, but a whitespace-only string is rejected rather
    # than kept as "".
    note: str | None = Field(default=None, max_length=NOTE_MAX_LENGTH)

    @field_validator("reviewer_name")
    @classmethod
    def _trim_reviewer_name(cls, value: str) -> str:
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("reviewer_name must not be blank")
        if len(trimmed) > REVIEWER_NAME_MAX_LENGTH:
            raise ValueError(
                f"reviewer_name must be at most {REVIEWER_NAME_MAX_LENGTH} characters"
            )
        return trimmed

    @field_validator("note")
    @classmethod
    def _trim_note(cls, value: str | None) -> str | None:
        if value is None:
            return None
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("note must not be blank when provided")
        if len(trimmed) > NOTE_MAX_LENGTH:
            raise ValueError(f"note must be at most {NOTE_MAX_LENGTH} characters")
        return trimmed

    @model_validator(mode="after")
    def _check_note_required_for_reject(self) -> "InvoiceReview":
        if self.action is ReviewAction.REJECT and self.note is None:
            raise ValueError("note is required when action is REJECT")
        return self


class ReviewedInvoiceResult(BaseModel):
    """An :class:`InvoiceReview` bound to the decision attempt it resolves.

    Mirrors :class:`app.schemas.decision.DecidedInvoiceResult`: it preserves the
    reference to the source decision attempt and nothing else.
    """

    model_config = ConfigDict(extra="forbid")

    source_decision_id: uuid.UUID
    review: InvoiceReview


# The pinned Part 1.3 document-status map. Kept here as data so the Package 2
# lifecycle code and the tests read one source of truth; the status write
# itself is Package 2. APPROVED / REJECTED are reachable only from
# NEEDS_REVIEW and only through Stage 7.
DOCUMENT_STATUS_FOR_ACTION: dict[ReviewAction, DocumentStatus] = {
    ReviewAction.APPROVE: DocumentStatus.APPROVED,
    ReviewAction.REJECT: DocumentStatus.REJECTED,
}


# Derived name tuples so the persistence layer and the package 2 API schemas
# cannot silently drift out of step with this contract.
REVIEW_ACTIONS: tuple[str, ...] = tuple(action.value for action in ReviewAction)
REVIEW_FIELD_NAMES: tuple[str, ...] = tuple(InvoiceReview.model_fields)
