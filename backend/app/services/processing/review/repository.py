"""Data access for human-review records (Stage 7, packages 1 and 2).

``ReviewRepository`` is the only place that reads or writes the
``invoice_reviews`` table. It owns no transaction boundary: it stages objects
on the session and runs queries, and the package 2 service decides when to
flush and commit - mirroring
:class:`app.services.processing.decision.repository.DecisionRepository`.

Writing a review goes through :mod:`app.schemas.review_persistence`, so the
flat column layout is only ever derived from a validated
:class:`~app.schemas.review.InvoiceReview` in one place. The source
``invoice_decisions`` row - and every Stage 2-6 row it transitively derives
from - is never written back to.

The package 2 review *queue* query spans ``documents`` and the whole current
processing chain in addition to this table.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime, timezone

from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased, selectinload

from app.models.decision import DecisionAttempt, DecisionOutcome, DecisionStatus
from app.models.document import Document, DocumentStatus
from app.models.extraction import ExtractionAttempt
from app.models.normalization import NormalizationAttempt
from app.models.review import ReviewRecord
from app.models.validation import ValidationAttempt
from app.schemas.review import InvoiceReview
from app.schemas.review_persistence import review_row

# One review-queue row: the document awaiting review, its unreviewed
# NEEDS_REVIEW decision (with its reasons loaded), and the two intermediate
# chain ids the API needs to build the decision-scoped review URL.
QueueRow = tuple[Document, DecisionAttempt, uuid.UUID, uuid.UUID]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ReviewRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # --- reads --------------------------------------------------------- --

    async def get(self, review_id: uuid.UUID) -> ReviewRecord | None:
        """One review by id."""
        result = await self._session.execute(
            select(ReviewRecord).where(ReviewRecord.review_id == review_id)
        )
        return result.scalar_one_or_none()

    async def get_for_decision(
        self, decision_id: uuid.UUID, review_id: uuid.UUID
    ) -> ReviewRecord | None:
        """One review by id, but only if it resolves ``decision_id``."""
        result = await self._session.execute(
            select(ReviewRecord).where(
                ReviewRecord.review_id == review_id,
                ReviewRecord.decision_id == decision_id,
            )
        )
        return result.scalar_one_or_none()

    async def get_by_decision(
        self, decision_id: uuid.UUID
    ) -> ReviewRecord | None:
        """The one review for a decision, if it has been resolved.

        At most one can exist - a unique constraint on
        ``invoice_reviews.decision_id`` enforces it at the database level.
        """
        result = await self._session.execute(
            select(ReviewRecord).where(ReviewRecord.decision_id == decision_id)
        )
        return result.scalar_one_or_none()

    async def exists_for_decision(self, decision_id: uuid.UUID) -> bool:
        """Whether a decision has already been resolved."""
        result = await self._session.execute(
            select(exists().where(ReviewRecord.decision_id == decision_id))
        )
        return bool(result.scalar())

    async def get_with_document(
        self, review_id: uuid.UUID
    ) -> tuple[ReviewRecord, uuid.UUID] | None:
        """One review by id, paired with its owning ``document_id``.

        The document is four joins up the chain (decision -> validation ->
        normalization -> extraction -> document); the review record itself
        stores only ``decision_id``.
        """
        result = await self._session.execute(
            select(ReviewRecord, ExtractionAttempt.document_id)
            .join(
                DecisionAttempt,
                DecisionAttempt.decision_id == ReviewRecord.decision_id,
            )
            .join(
                ValidationAttempt,
                ValidationAttempt.validation_id == DecisionAttempt.validation_id,
            )
            .join(
                NormalizationAttempt,
                NormalizationAttempt.normalization_id
                == ValidationAttempt.normalization_id,
            )
            .join(
                ExtractionAttempt,
                ExtractionAttempt.extraction_id == NormalizationAttempt.extraction_id,
            )
            .where(ReviewRecord.review_id == review_id)
        )
        row = result.one_or_none()
        return None if row is None else (row[0], row[1])

    async def queue(
        self, *, limit: int = 50, offset: int = 0
    ) -> Sequence[QueueRow]:
        """Documents awaiting a human decision, oldest decision first (FIFO).

        Eligibility (spec Part 2.5): the document is ``NEEDS_REVIEW``, its
        *current* decision attempt is ``COMPLETED`` with outcome
        ``NEEDS_REVIEW``, and no review row references that decision yet.
        Every hop is constrained to its greatest attempt number so historical
        decisions never appear as actionable queue entries. The submit path
        repeats this check under a lock because the chain may change after the
        queue is read.
        """
        current_extraction = aliased(ExtractionAttempt)
        current_normalization = aliased(NormalizationAttempt)
        current_validation = aliased(ValidationAttempt)
        current_decision = aliased(DecisionAttempt)

        stmt = (
            select(
                Document,
                DecisionAttempt,
                ValidationAttempt.normalization_id,
                NormalizationAttempt.extraction_id,
            )
            .join(
                ValidationAttempt,
                ValidationAttempt.validation_id == DecisionAttempt.validation_id,
            )
            .join(
                NormalizationAttempt,
                NormalizationAttempt.normalization_id
                == ValidationAttempt.normalization_id,
            )
            .join(
                ExtractionAttempt,
                ExtractionAttempt.extraction_id == NormalizationAttempt.extraction_id,
            )
            .join(Document, Document.document_id == ExtractionAttempt.document_id)
            .where(
                DecisionAttempt.status == DecisionStatus.COMPLETED,
                DecisionAttempt.outcome == DecisionOutcome.NEEDS_REVIEW,
                Document.status == DocumentStatus.NEEDS_REVIEW,
                ~exists().where(ReviewRecord.decision_id == DecisionAttempt.decision_id),
                ExtractionAttempt.attempt_number
                == select(func.max(current_extraction.attempt_number))
                .where(current_extraction.document_id == Document.document_id)
                .scalar_subquery(),
                NormalizationAttempt.attempt_number
                == select(func.max(current_normalization.attempt_number))
                .where(
                    current_normalization.extraction_id
                    == ExtractionAttempt.extraction_id
                )
                .scalar_subquery(),
                ValidationAttempt.attempt_number
                == select(func.max(current_validation.attempt_number))
                .where(
                    current_validation.normalization_id
                    == NormalizationAttempt.normalization_id
                )
                .scalar_subquery(),
                DecisionAttempt.attempt_number
                == select(func.max(current_decision.attempt_number))
                .where(
                    current_decision.validation_id == ValidationAttempt.validation_id
                )
                .scalar_subquery(),
            )
            .order_by(DecisionAttempt.completed_at.asc(), DecisionAttempt.decision_id.asc())
            .options(selectinload(DecisionAttempt.reasons))
            .limit(limit)
            .offset(offset)
        )
        result = await self._session.execute(stmt)
        return [tuple(row) for row in result.all()]

    # --- writes ------------------------------------------------------- --

    def add(
        self,
        *,
        decision_id: uuid.UUID,
        review: InvoiceReview,
        policy_version: str,
        reviewed_at: datetime | None = None,
    ) -> ReviewRecord:
        """Build a review row from a validated contract and stage it.

        ``policy_version`` is stamped now and never changed afterwards
        (spec Part 2.7). The caller flushes; a second ``add`` for the same
        decision - or a concurrent one - surfaces as an
        :class:`~sqlalchemy.exc.IntegrityError` on the unique
        ``decision_id`` constraint at flush time, which the package 2 service
        turns into a client-safe ``409``.
        """
        when = reviewed_at or _utcnow()
        record = ReviewRecord(
            decision_id=decision_id,
            policy_version=policy_version,
            reviewed_at=when,
            created_at=when,
            **review_row(review),
        )
        self._session.add(record)
        return record


__all__ = ["ReviewRepository"]
