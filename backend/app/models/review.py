"""Human-review persistence model (Stage 7, package 1).

One table, ``invoice_reviews`` - one row per **terminal human resolution** of a
Stage 6 decision that came out ``NEEDS_REVIEW``. Unlike every earlier stage
there is no attempt/retry lifecycle: a review is a single event that either
commits whole or never happens, so there is no ``status`` column, no
``PROCESSING`` / ``FAILED`` state, and no ``attempt_number``. The natural key is
just ``decision_id``, and a ``UNIQUE`` constraint on it enforces *at most one
review per decision* - a second (or concurrent) resolution loses the race
(spec Part 2.7).

There is no child table: a review carries no structured reason list of its own.
The evidence a reviewer acted on - validation findings, ordered decision
reasons - already lives on the ``invoice_validation_findings`` /
``invoice_decision_reasons`` rows and is shown read-only by the Package 3 UI.

``policy_version`` records which named revision of the Part 2 review policy
(:data:`app.schemas.review.REVIEW_POLICY_VERSION`) was in effect, stamped once
at write time and never changed - so a historical resolution stays explicable
after the policy is retuned later.

This row never modifies the Stage 2-6 records it derives from. The ORM class is
``ReviewRecord`` to keep it distinct from the Pydantic contract type
:class:`app.schemas.review.InvoiceReview`; the table is ``invoice_reviews``.

The full boundary and the pinned review policy live in
``docs/stage-7-review.md``. The column types here enforce only structural parts
(the note rule, the non-blank reviewer name, one-review-per-decision); the
lifecycle guards and the ``documents.status`` write are the package 2 service.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    Enum,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base
from app.schemas.review import (
    NOTE_MAX_LENGTH,
    REVIEWER_NAME_MAX_LENGTH,
    ReviewAction,
)

if TYPE_CHECKING:
    from app.models.decision import DecisionAttempt

__all__ = ["ReviewAction", "ReviewRecord"]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# reviewer_name must not be whitespace-only, and note must be present and
# non-blank for a REJECT while staying optional (but non-blank if given) for an
# APPROVE. Every branch tests IS [NOT] NULL explicitly before comparing, so a
# NULL cannot make the constraint vacuously pass.
_REVIEWER_NAME_NOT_BLANK = "length(btrim(reviewer_name)) > 0"
_NOTE_SHAPE = (
    "(action = 'REJECT' AND note IS NOT NULL AND length(btrim(note)) > 0) OR "
    "(action = 'APPROVE' AND (note IS NULL OR length(btrim(note)) > 0))"
)


class ReviewRecord(Base):
    __tablename__ = "invoice_reviews"

    __table_args__ = (
        # At most one review per decision (spec Part 2.7). A concurrent second
        # INSERT loses the race here.
        UniqueConstraint("decision_id", name="uq_invoice_reviews_decision_id"),
        CheckConstraint(
            _REVIEWER_NAME_NOT_BLANK, name="reviewer_name_not_blank"
        ),
        CheckConstraint(_NOTE_SHAPE, name="note_shape"),
    )

    review_id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)

    # Source Stage 6 attempt. CASCADE so deleting a document (which cascades
    # through extraction -> normalization -> validation -> decision) also
    # clears the derived review; review code never writes back to this row.
    decision_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("invoice_decisions.decision_id", ondelete="CASCADE"),
        nullable=False,
    )

    action: Mapped[ReviewAction] = mapped_column(
        Enum(
            ReviewAction,
            name="review_action",
            native_enum=True,
            validate_strings=True,
        ),
        nullable=False,
    )

    # A free-text, UNVERIFIED label (LedgerDrop has no authentication yet).
    # Stored for the audit trail only; never used for authorization.
    reviewer_name: Mapped[str] = mapped_column(
        String(REVIEWER_NAME_MAX_LENGTH), nullable=False
    )

    # Required and non-blank for REJECT, optional for APPROVE (see _NOTE_SHAPE).
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    # The REVIEW_POLICY_VERSION in effect when this resolution was recorded.
    # Stamped once; never changed.
    policy_version: Mapped[str] = mapped_column(String(32), nullable=False)

    # The moment the resolution was recorded - the audit timestamp. Distinct
    # from created_at only in intent; both are set once at INSERT.
    reviewed_at: Mapped[datetime] = mapped_column(nullable=False, default=_utcnow)
    created_at: Mapped[datetime] = mapped_column(nullable=False, default=_utcnow)
    # No updated_at: the row is immutable by design (spec Part 2.7). Omitting
    # the column makes an accidental in-place update stand out in review.

    source_decision: Mapped["DecisionAttempt"] = relationship(
        back_populates="review"
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<ReviewRecord {self.review_id} decision={self.decision_id} "
            f"{self.action}>"
        )
