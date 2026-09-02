"""Extraction orchestration (Stage 3, steps 5 and 6).

``ExtractionService`` drives one extraction attempt through the processing
lifecycle:

    UPLOADED / FAILED  ->  PROCESSING  ->  COMPLETED | FAILED

and guarantees:

* **PROCESSING is durable before work starts.** The attempt row and the
  document's ``PROCESSING`` status are committed *before* the result producer is
  called, so an interrupted run leaves a visible record rather than a document
  stuck mid-flight with nothing to show.
* **One active attempt per document.** A ``SELECT ... FOR UPDATE`` on the
  document row serialises concurrent starts; the database's partial unique index
  is the backstop.
* **Failure leaves the source intact.** A failed attempt only ever writes
  ``FAILED`` plus a client-safe ``failure_code`` / ``failure_message``. It never
  touches the stored PDF and never persists a partial result as a success.
* **History is preserved.** A retry always creates a new attempt
  (``attempt_number + 1``); earlier attempts are never mutated or deleted.

The result *producer* is injected. For steps 5-6 it is a hand-written function
or a fake; the real provider adapter (step 12) will supply one behind the
provider interface. The producer either returns something that validates as
:class:`InvoiceExtraction`, or raises: :class:`ExtractionError` for an expected,
already-safe failure (timeout, rate limit, "not an invoice"), or any other
exception for an unexpected technical failure.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError
from app.models.document import Document, DocumentStatus
from app.models.extraction import ExtractionAttempt, ExtractionStatus
from app.schemas.extraction import InvoiceExtraction
from app.services.processing.extraction import lifecycle
from app.services.processing.extraction.repository import ExtractionRepository
from app.services.processing.extraction.provider import ProviderResponse

logger = logging.getLogger("app.extraction")

# Client-safe fallback messages. Real diagnostics go to the log, never the row.
_GENERIC_FAILURE = "Extraction did not complete. Retry the extraction to try again."
_MALFORMED_FAILURE = "The extraction result did not match the required invoice schema."

ResultProducer = Callable[[Document], Awaitable[Any]]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ExtractionError(Exception):
    """Signal from a result producer that extraction failed in an expected way.

    ``code`` and ``message`` are already client-safe and are stored verbatim on
    the failed attempt. Any other exception type from a producer is treated as
    an unexpected technical failure and collapsed to a generic message.
    """

    def __init__(self, message: str, *, code: str = "EXTRACTION_FAILED") -> None:
        super().__init__(message)
        self.code = code
        self.safe_message = message


class ExtractionService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        repository: ExtractionRepository | None = None,
    ) -> None:
        self._session = session
        self._repo = repository or ExtractionRepository(session)

    # --- public API --------------------------------------------------------

    async def start( 
        self,
        document_id: uuid.UUID,
        *,
        produce: ResultProducer,
        provider_name: str,
        provider_model: str | None = None,
        raw_response: dict[str, Any] | None = None,
    ) -> ExtractionAttempt:
        """Run the first extraction for an ``UPLOADED`` document."""
        return await self._run(
            document_id,
            action="start",
            produce=produce,
            provider_name=provider_name,
            provider_model=provider_model,
            raw_response=raw_response,
        )

    async def retry(
        self,
        document_id: uuid.UUID,
        *,
        produce: ResultProducer,
        provider_name: str,
        provider_model: str | None = None,
        raw_response: dict[str, Any] | None = None,
    ) -> ExtractionAttempt:
        """Run a fresh extraction attempt for a document whose last one FAILED."""
        return await self._run(
            document_id,
            action="retry",
            produce=produce,
            provider_name=provider_name,
            provider_model=provider_model,
            raw_response=raw_response,
        )

    # --- orchestration -------------------------------------------------- --

    async def _run(
        self,
        document_id: uuid.UUID,
        *,
        action: lifecycle.Action,
        produce: ResultProducer,
        provider_name: str,
        provider_model: str | None,
        raw_response: dict[str, Any] | None,
    ) -> ExtractionAttempt:
        document = await self._lock_document(document_id)
        if document is None:
            raise NotFoundError("No document exists with that ID.", code="DOCUMENT_NOT_FOUND")

        lifecycle.ensure_document_can_extract(document.status, action=action)

        if await self._repo.active_for_document(document_id) is not None:
            raise ConflictError(
                "An extraction is already in progress for this document.",
                code="EXTRACTION_IN_PROGRESS",
            )

        if action == "retry":
            latest = await self._repo.latest_for_document(document_id)
            if latest is None or latest.status is not ExtractionStatus.FAILED:
                raise ConflictError(
                    "Only a document with a failed extraction attempt can be retried.",
                    code="EXTRACTION_NOT_FAILED",
                )

        attempt = self._repo.add_attempt(
            document_id=document_id,
            attempt_number=await self._repo.next_attempt_number(document_id),
            provider_name=provider_name,
            provider_model=provider_model,
        )
        document.status = DocumentStatus.PROCESSING
        try:
            await self._session.flush()
        except IntegrityError as exc:
            # Lost a race for the single active slot, or the attempt number.
            await self._session.rollback()
            raise ConflictError(
                "An extraction was started for this document concurrently.",
                code="EXTRACTION_IN_PROGRESS",
            ) from exc
        await self._session.commit()

        extraction_id = attempt.extraction_id

        try:
            produced = await produce(document)
        except ExtractionError as exc:
            logger.warning("extraction %s failed: %s", extraction_id, exc.safe_message)
            return await self._mark_failed(
                extraction_id, document_id, code=exc.code, message=exc.safe_message
            )
        except Exception:
            logger.exception("extraction %s raised an unexpected error", extraction_id)
            return await self._mark_failed(
                extraction_id, document_id, code="EXTRACTION_FAILED", message=_GENERIC_FAILURE
            )

        if isinstance(produced, ProviderResponse):
            if produced.raw_response is not None:
                raw_response = dict(produced.raw_response)
            produced = produced.payload

        try:
            # Revalidate model instances too. Pydantic's ``model_construct`` and
            # ``model_copy(update=...)`` intentionally bypass validation, so an
            # ``isinstance`` shortcut would let malformed data reach persistence.
            candidate = (
                produced.model_dump()
                if isinstance(produced, InvoiceExtraction)
                else produced
            )
            result = InvoiceExtraction.model_validate(candidate)
        except (ValidationError, AttributeError, TypeError):
            logger.warning("extraction %s produced schema-invalid output", extraction_id)
            return await self._mark_failed(
                extraction_id,
                document_id,
                code="MALFORMED_EXTRACTION",
                message=_MALFORMED_FAILURE,
            )

        return await self._complete(
            extraction_id, document_id, result, raw_response=raw_response
        )

    async def _complete(
        self,
        extraction_id: uuid.UUID,
        document_id: uuid.UUID,
        result: InvoiceExtraction,
        *,
        raw_response: dict[str, Any] | None,
    ) -> ExtractionAttempt:
        attempt = await self._repo.get(extraction_id)
        document = await self._session.get(Document, document_id)
        assert attempt is not None and document is not None  # committed moments ago

        try:
            self._repo.apply_result(attempt, result, raw_response=raw_response)
            lifecycle.ensure_attempt_transition(attempt.status, ExtractionStatus.COMPLETED)
            attempt.status = ExtractionStatus.COMPLETED
            attempt.completed_at = _utcnow()
            document.status = DocumentStatus.COMPLETED
            await self._session.flush()
            await self._session.commit()
        except Exception:
            await self._session.rollback()
            logger.exception("persisting completed extraction %s failed", extraction_id)
            return await self._mark_failed(
                extraction_id, document_id, code="EXTRACTION_FAILED", message=_GENERIC_FAILURE
            )
        return attempt

    async def _mark_failed(
        self,
        extraction_id: uuid.UUID,
        document_id: uuid.UUID,
        *,
        code: str,
        message: str,
    ) -> ExtractionAttempt:
        # Discard anything half-written by a failed completion, then record the
        # failure against the already-committed PROCESSING attempt.
        await self._session.rollback()
        attempt = await self._repo.get(extraction_id)
        document = await self._session.get(Document, document_id)
        assert attempt is not None and document is not None

        lifecycle.ensure_attempt_transition(attempt.status, ExtractionStatus.FAILED)
        attempt.status = ExtractionStatus.FAILED
        attempt.completed_at = _utcnow()
        attempt.failure_code = code
        attempt.failure_message = message
        document.status = DocumentStatus.FAILED

        await self._session.flush()
        await self._session.commit()
        return attempt

    async def _lock_document(self, document_id: uuid.UUID) -> Document | None:
        result = await self._session.execute(
            select(Document).where(Document.document_id == document_id).with_for_update()
        )
        return result.scalar_one_or_none()


__all__ = ["ExtractionService", "ExtractionError", "ResultProducer"]
