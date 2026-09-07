"""Review orchestration (Stage 7, package 2).

``ReviewService.submit`` records one human resolution of a Stage 6
``NEEDS_REVIEW`` decision and applies the single authorised document-status
transition, atomically:

    COMPLETED, NEEDS_REVIEW decision  ->  one invoice_reviews row
                                          + documents.status NEEDS_REVIEW -> APPROVED | REJECTED

Guarantees, mirroring the Stage 3-6 services where they apply:

* **Source locking.** A ``SELECT ... FOR UPDATE`` on the target
  ``invoice_decisions`` row *and* the owning ``documents`` row serialises
  concurrent submits: the first writer commits the review and the terminal
  status; a second submit then re-reads, sees the review, and gets
  ``DECISION_ALREADY_REVIEWED``. The ``UNIQUE (decision_id)`` constraint on
  ``invoice_reviews`` is the backstop if two submits somehow race past the
  lock.
* **Only a reviewable decision is touched.** The decision must be a
  ``COMPLETED`` attempt with outcome ``NEEDS_REVIEW`` that is still the
  document's current chain; a Stage 6 ``ACCEPTED`` result cannot be overridden
  here, and a stale chain is refused (``STALE_DECISION_SOURCE``).
* **No attempt lifecycle, safe retries.** A review is a single terminal event.
  There is no ``PROCESSING``/``FAILED`` review row and no retry route: if the
  write fails for a technical reason the whole transaction rolls back - no
  review row, ``documents.status`` still ``NEEDS_REVIEW`` - and the client
  simply calls submit again. A *successful* review is terminal; it cannot be
  amended or re-opened.
* **The source is never mutated.** Submitting only reads the decision chain and
  writes one new ``invoice_reviews`` row plus ``documents.status`` (and its
  ``updated_at``). No Stage 2-6 row and no stored PDF changes.

There is **no AI call and no external-network call** - only the source lookups
and the two writes touch the database.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError
from app.models.decision import DecisionAttempt
from app.models.document import Document
from app.models.extraction import ExtractionAttempt
from app.models.normalization import NormalizationAttempt
from app.models.review import ReviewRecord
from app.models.validation import ValidationAttempt
from app.schemas.review import REVIEW_POLICY_VERSION, InvoiceReview
from app.services.processing.decision.repository import DecisionRepository
from app.services.processing.extraction.repository import ExtractionRepository
from app.services.processing.normalization.repository import NormalizationRepository
from app.services.processing.review import lifecycle
from app.services.processing.review.repository import ReviewRepository
from app.services.processing.validation.repository import ValidationRepository

logger = logging.getLogger("app.review")

# (decision, owning document, chain normalization id, chain extraction id)
_Source = tuple[DecisionAttempt, Document, uuid.UUID, uuid.UUID]


class ReviewService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        repository: ReviewRepository | None = None,
    ) -> None:
        self._session = session
        self._repo = repository or ReviewRepository(session)
        self._decision_repo = DecisionRepository(session)
        self._extraction_repo = ExtractionRepository(session)
        self._normalization_repo = NormalizationRepository(session)
        self._validation_repo = ValidationRepository(session)

    async def submit(
        self, decision_id: uuid.UUID, review: InvoiceReview
    ) -> ReviewRecord:
        """Record ``review`` against ``decision_id`` and move the document.

        Raises :class:`NotFoundError` (``DECISION_NOT_FOUND``) for an unknown
        decision and :class:`ConflictError` for ``DECISION_NOT_REVIEWABLE`` /
        ``DECISION_ALREADY_REVIEWED`` / ``STALE_DECISION_SOURCE``. Any other
        (technical) failure rolls the transaction back and propagates - nothing
        is persisted, so the caller can safely retry.
        """
        source = await self._lock_source(decision_id)
        if source is None:
            raise NotFoundError(
                "No decision exists with that ID.", code="DECISION_NOT_FOUND"
            )
        decision, document, chain_normalization_id, chain_extraction_id = source

        already_reviewed = await self._repo.exists_for_decision(decision_id)
        lifecycle.ensure_decision_can_be_reviewed(
            decision_status=decision.status,
            decision_outcome=decision.outcome,
            already_reviewed=already_reviewed,
        )
        lifecycle.ensure_document_awaiting_review(document.status)

        latest_decision = await self._decision_repo.latest_for_validation(
            decision.validation_id
        )
        await self._ensure_current_source(
            document.document_id,
            chain_extraction_id=chain_extraction_id,
            chain_normalization_id=chain_normalization_id,
            chain_validation_id=decision.validation_id,
            decision_is_latest=(
                latest_decision is not None
                and latest_decision.decision_id == decision_id
            ),
        )

        record = self._repo.add(
            decision_id=decision_id,
            review=review,
            policy_version=REVIEW_POLICY_VERSION,
        )
        document.status = lifecycle.document_status_for_action(review.action)

        try:
            await self._session.flush()
        except IntegrityError as exc:
            await self._session.rollback()
            raise ConflictError(
                "This decision has already been reviewed.",
                code="DECISION_ALREADY_REVIEWED",
            ) from exc
        except Exception:
            await self._session.rollback()
            logger.exception("submitting review for decision %s failed", decision_id)
            raise
        try:
            await self._session.commit()
        except Exception:
            await self._session.rollback()
            logger.exception("committing review for decision %s failed", decision_id)
            raise
        return record

    async def _ensure_current_source(
        self,
        document_id: uuid.UUID,
        *,
        chain_extraction_id: uuid.UUID,
        chain_normalization_id: uuid.UUID,
        chain_validation_id: uuid.UUID,
        decision_is_latest: bool,
    ) -> None:
        current_extraction = await self._extraction_repo.latest_for_document(document_id)
        current_normalization = (
            await self._normalization_repo.latest_for_extraction(
                current_extraction.extraction_id
            )
            if current_extraction is not None
            else None
        )
        current_validation = (
            await self._validation_repo.latest_for_normalization(
                current_normalization.normalization_id
            )
            if current_normalization is not None
            else None
        )
        lifecycle.ensure_decision_is_current_source(
            decision_is_latest_for_validation=decision_is_latest,
            chain_extraction_id=chain_extraction_id,
            chain_normalization_id=chain_normalization_id,
            chain_validation_id=chain_validation_id,
            current_extraction_id=(
                current_extraction.extraction_id
                if current_extraction is not None
                else None
            ),
            current_normalization_id=(
                current_normalization.normalization_id
                if current_normalization is not None
                else None
            ),
            current_validation_id=(
                current_validation.validation_id
                if current_validation is not None
                else None
            ),
        )

    async def _lock_source(self, decision_id: uuid.UUID) -> _Source | None:
        """Resolve and lock the decision's row and its owning document.

        ``FOR UPDATE OF`` names only ``DecisionAttempt`` and ``Document`` so
        Postgres locks exactly those two rows: the decision lock serialises
        concurrent submits, and the document lock keeps the staleness check and
        the ``documents.status`` write consistent with each other.
        """
        result = await self._session.execute(
            select(
                DecisionAttempt,
                Document,
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
            .where(DecisionAttempt.decision_id == decision_id)
            .with_for_update(of=[DecisionAttempt, Document])
        )
        row = result.one_or_none()
        if row is None:
            return None
        decision, document, normalization_id, extraction_id = row
        return decision, document, normalization_id, extraction_id


__all__ = ["ReviewService"]
