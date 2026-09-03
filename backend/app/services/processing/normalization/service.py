"""Normalization orchestration (Stage 4, step 11).

``NormalizationService`` drives one normalization attempt through the Stage 4
lifecycle::

    COMPLETED extraction  ->  PROCESSING  ->  COMPLETED | FAILED
    FAILED normalization   ->  PROCESSING  ->  COMPLETED | FAILED   (explicit retry)

and guarantees:

* **PROCESSING is durable before work starts.** The attempt row is committed
  ``PROCESSING`` *before* the deterministic engine runs, so an interrupted run
  leaves a visible record rather than a silent gap.
* **One active attempt per source extraction.** A ``SELECT ... FOR UPDATE`` on
  the source ``invoice_extractions`` row serialises concurrent starts; the
  partial unique index on ``invoice_normalizations`` is the backstop.
* **A field error is not a technical failure.** The engine's
  :class:`~app.schemas.normalization.NormalizationError` entries are persisted
  inside the result and the attempt still ends ``COMPLETED``. Only an
  infrastructure problem - the source extraction cannot be read, a database
  write fails, or the engine raises an unexpected exception - ends an attempt
  ``FAILED`` with a client-safe ``failure_code`` / ``failure_message``.
* **The source is never touched.** A normalization attempt only reads the Stage
  3 extraction and the original PDF; a failure leaves both intact and a retry
  is allowed.
* **History is preserved.** A retry always creates a new attempt
  (``attempt_number + 1``); earlier attempts are never mutated or deleted.

Normalization makes **no AI call and no external-network call** - the engine is
pure, in-process, deterministic Python.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError
from app.models.extraction import ExtractionAttempt
from app.models.normalization import NormalizationAttempt, NormalizationStatus
from app.schemas.extraction_persistence import invoice_extraction_from_attempt
from app.schemas.normalization import NormalizedInvoice
from app.services.processing.extraction.repository import ExtractionRepository
from app.services.processing.normalization import lifecycle
from app.services.processing.normalization.engine import normalize_extraction
from app.services.processing.normalization.repository import NormalizationRepository

logger = logging.getLogger("app.normalization")

# Client-safe fallback for any technical failure. Real diagnostics go to the
# log, never onto the row.
_GENERIC_FAILURE = (
    "Normalization did not complete. Retry the normalization to try again."
)
_FAILURE_CODE = "NORMALIZATION_FAILED"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class NormalizationService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        repository: NormalizationRepository | None = None,
        extraction_repository: ExtractionRepository | None = None,
    ) -> None:
        self._session = session
        self._repo = repository or NormalizationRepository(session)
        self._extractions = extraction_repository or ExtractionRepository(session)

    # --- public API -------------------------------------------------------

    async def start(self, extraction_id: uuid.UUID) -> NormalizationAttempt:
        """Run the first normalization for a ``COMPLETED`` extraction."""
        return await self._run(extraction_id, action="start")

    async def retry(self, extraction_id: uuid.UUID) -> NormalizationAttempt:
        """Run a fresh attempt for an extraction whose last normalization FAILED."""
        return await self._run(extraction_id, action="retry")

    # --- orchestration ------------------------------------------------- --

    async def _run(
        self, extraction_id: uuid.UUID, *, action: lifecycle.Action
    ) -> NormalizationAttempt:
        source = await self._lock_extraction(extraction_id)
        if source is None:
            raise NotFoundError(
                "No extraction exists with that ID.", code="EXTRACTION_NOT_FOUND"
            )

        latest = await self._repo.latest_for_extraction(extraction_id)
        lifecycle.ensure_extraction_can_normalize(
            source.status,
            latest.status if latest is not None else None,
            action=action,
        )

        if await self._repo.active_for_extraction(extraction_id) is not None:
            raise ConflictError(
                "A normalization is already in progress for this extraction.",
                code="NORMALIZATION_IN_PROGRESS",
            )

        attempt = self._repo.add_attempt(
            extraction_id=extraction_id,
            attempt_number=await self._repo.next_attempt_number(extraction_id),
        )
        try:
            await self._session.flush()
        except IntegrityError as exc:
            # Lost a race for the single active slot, or the attempt number.
            await self._session.rollback()
            raise ConflictError(
                "A normalization was started for this extraction concurrently.",
                code="NORMALIZATION_IN_PROGRESS",
            ) from exc
        await self._session.commit()

        normalization_id = attempt.normalization_id

        try:
            stored = await self._extractions.get(extraction_id)
            assert stored is not None  # locked and present moments ago
            contract = invoice_extraction_from_attempt(stored)
            normalized = normalize_extraction(contract)
        except Exception:
            logger.exception(
                "normalization %s raised while producing a result", normalization_id
            )
            return await self._mark_failed(normalization_id)

        return await self._complete(normalization_id, normalized)

    async def _complete(
        self, normalization_id: uuid.UUID, normalized: NormalizedInvoice
    ) -> NormalizationAttempt:
        attempt = await self._repo.get(normalization_id)
        assert attempt is not None  # committed moments ago

        try:
            self._repo.apply_result(attempt, normalized)
            lifecycle.ensure_attempt_transition(
                attempt.status, NormalizationStatus.COMPLETED
            )
            attempt.status = NormalizationStatus.COMPLETED
            attempt.completed_at = _utcnow()
            await self._session.flush()
            await self._session.commit()
        except Exception:
            await self._session.rollback()
            logger.exception(
                "persisting completed normalization %s failed", normalization_id
            )
            return await self._mark_failed(normalization_id)
        return attempt

    async def _mark_failed(
        self, normalization_id: uuid.UUID
    ) -> NormalizationAttempt:
        # Discard anything half-written by a failed completion, then record the
        # failure against the already-committed PROCESSING attempt.
        await self._session.rollback()
        attempt = await self._repo.get(normalization_id)
        assert attempt is not None

        lifecycle.ensure_attempt_transition(
            attempt.status, NormalizationStatus.FAILED
        )
        attempt.status = NormalizationStatus.FAILED
        attempt.completed_at = _utcnow()
        attempt.failure_code = _FAILURE_CODE
        attempt.failure_message = _GENERIC_FAILURE

        await self._session.flush()
        await self._session.commit()
        return attempt

    async def _lock_extraction(
        self, extraction_id: uuid.UUID
    ) -> ExtractionAttempt | None:
        result = await self._session.execute(
            select(ExtractionAttempt)
            .where(ExtractionAttempt.extraction_id == extraction_id)
            .with_for_update()
        )
        return result.scalar_one_or_none()


__all__ = ["NormalizationService"]
