"""Validation orchestration (Stage 5, step 11).

``ValidationService`` drives one validation attempt through the Stage 5
lifecycle::

    COMPLETED normalization  ->  PROCESSING  ->  COMPLETED | FAILED
    FAILED validation         ->  PROCESSING  ->  COMPLETED | FAILED   (explicit retry)

and guarantees:

* **PROCESSING is durable before work starts.** The attempt row is committed
  ``PROCESSING`` *before* the engine runs, so an interrupted run leaves a
  visible record rather than a silent gap.
* **One active attempt per source normalization.** A ``SELECT ... FOR UPDATE``
  on the source ``invoice_normalizations`` row serialises concurrent starts;
  the partial unique index on ``invoice_validations`` is the backstop.
* **A rule violation is not a technical failure.** The engine's findings are
  persisted inside the result and the attempt still ends ``COMPLETED``. Only an
  infrastructure problem - the source normalization cannot be read, the engine
  raises an unexpected exception, or a database write fails - ends an attempt
  ``FAILED`` with a client-safe ``failure_code`` / ``failure_message`` and no
  partial findings.
* **The source is never touched.** A validation attempt only reads the Stage 4
  normalization (and, through it, Stage 2-3 data); a failure leaves all of it
  intact and a retry is allowed.
* **History is preserved.** A retry always creates a new attempt
  (``attempt_number + 1``); earlier attempts are never mutated or deleted.

Validation makes **no AI call and no external-network call** - the engine is
pure, in-process, deterministic Python plus read-only SQL.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError
from app.models.normalization import NormalizationAttempt
from app.models.validation import ValidationAttempt, ValidationStatus
from app.schemas.validation import InvoiceValidation
from app.services.processing.validation import lifecycle
from app.services.processing.validation.engine import evaluate
from app.services.processing.validation.repository import ValidationRepository

logger = logging.getLogger("app.validation")

# Client-safe fallback for any technical failure. Real diagnostics go to the
# log, never onto the row.
_GENERIC_FAILURE = "Validation did not complete. Retry the validation to try again."
_FAILURE_CODE = "VALIDATION_FAILED"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ValidationService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        repository: ValidationRepository | None = None,
    ) -> None:
        self._session = session
        self._repo = repository or ValidationRepository(session)

    # --- public API -------------------------------------------------------

    async def start(self, normalization_id: uuid.UUID) -> ValidationAttempt:
        """Run the first validation for a ``COMPLETED`` normalization attempt."""
        return await self._run(normalization_id, action="start")

    async def retry(self, normalization_id: uuid.UUID) -> ValidationAttempt:
        """Run a fresh attempt for a normalization whose last validation FAILED."""
        return await self._run(normalization_id, action="retry")

    # --- orchestration ------------------------------------------------- --

    async def _run(
        self, normalization_id: uuid.UUID, *, action: lifecycle.Action
    ) -> ValidationAttempt:
        source = await self._lock_normalization(normalization_id)
        if source is None:
            raise NotFoundError(
                "No normalization exists with that ID.",
                code="NORMALIZATION_NOT_FOUND",
            )

        latest = await self._repo.latest_for_normalization(normalization_id)
        lifecycle.ensure_normalization_can_validate(
            source.status,
            latest.status if latest is not None else None,
            action=action,
        )

        if await self._repo.active_for_normalization(normalization_id) is not None:
            raise ConflictError(
                "A validation is already in progress for this normalization.",
                code="VALIDATION_IN_PROGRESS",
            )

        attempt = self._repo.add_attempt(
            normalization_id=normalization_id,
            attempt_number=await self._repo.next_attempt_number(normalization_id),
        )
        try:
            await self._session.flush()
        except IntegrityError as exc:
            # Lost a race for the single active slot, or the attempt number.
            await self._session.rollback()
            raise ConflictError(
                "A validation was started for this normalization concurrently.",
                code="VALIDATION_IN_PROGRESS",
            ) from exc
        await self._session.commit()

        validation_id = attempt.validation_id
        started_at = attempt.started_at

        try:
            result = await evaluate(
                self._session, normalization_id, started_at=started_at
            )
        except Exception:
            logger.exception(
                "validation %s raised while evaluating rules", validation_id
            )
            return await self._mark_failed(validation_id)

        return await self._complete(validation_id, result)

    async def _complete(
        self, validation_id: uuid.UUID, result: InvoiceValidation
    ) -> ValidationAttempt:
        attempt = await self._repo.get(validation_id)
        assert attempt is not None  # committed moments ago

        try:
            self._repo.apply_result(attempt, result)
            lifecycle.ensure_attempt_transition(
                attempt.status, ValidationStatus.COMPLETED
            )
            attempt.status = ValidationStatus.COMPLETED
            attempt.completed_at = _utcnow()
            await self._session.flush()
            await self._session.commit()
        except Exception:
            await self._session.rollback()
            logger.exception(
                "persisting completed validation %s failed", validation_id
            )
            return await self._mark_failed(validation_id)
        return attempt

    async def _mark_failed(self, validation_id: uuid.UUID) -> ValidationAttempt:
        # Discard anything half-written by a failed completion, then record the
        # failure against the already-committed PROCESSING attempt.
        await self._session.rollback()
        attempt = await self._repo.get(validation_id)
        assert attempt is not None

        lifecycle.ensure_attempt_transition(attempt.status, ValidationStatus.FAILED)
        attempt.status = ValidationStatus.FAILED
        attempt.completed_at = _utcnow()
        attempt.failure_code = _FAILURE_CODE
        attempt.failure_message = _GENERIC_FAILURE

        await self._session.flush()
        await self._session.commit()
        return attempt

    async def _lock_normalization(
        self, normalization_id: uuid.UUID
    ) -> NormalizationAttempt | None:
        result = await self._session.execute(
            select(NormalizationAttempt)
            .where(NormalizationAttempt.normalization_id == normalization_id)
            .with_for_update()
        )
        return result.scalar_one_or_none()


__all__ = ["ValidationService"]
