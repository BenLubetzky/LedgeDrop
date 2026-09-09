"""Correction orchestration (Stage 8, package 2).

``CorrectionService`` drives one correction attempt through the Stage 8
lifecycle (``docs/stage-8-corrections.md`` Part 1.4)::

    correctable document -> PROCESSING -> COMPLETED | FAILED
    FAILED correction   -> (retry) -> PROCESSING -> COMPLETED | FAILED

and guarantees:

* **PROCESSING is durable before work starts.** The ``invoice_corrections``
  row and its field entries are committed before the projection, the new
  normalization attempt, and the re-run validation / decision are attempted.
* **No Stage 2-7 row or stored file is mutated.** The corrected canonical
  values are a *new* ``invoice_normalizations`` attempt (Part 3.5); the only
  existing-row write is ``documents.status`` (Part 2.4).
* **A NEEDS_REVIEW re-decision is a success**, not a ``FAILED`` correction.
  ``FAILED`` is a technical fault only, is client-safe, leaves
  ``documents.status`` no further advanced than the last committed sub-stage
  left it, keeps any partial downstream attempts (each retryable on its own
  endpoint), and is itself retryable from the stored submission.
* **One active attempt per document** - a ``SELECT ... FOR UPDATE`` on the
  ``documents`` row plus the partial unique index on ``invoice_corrections``.

Deciding whether to auto-accept is the pinned Part 2.4 matrix
(``lifecycle.resolve_document_status``); the re-run validation and decision
are the existing Stage 5 / Stage 6 services, unchanged. **No AI or
external-network call anywhere.**
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError, UnprocessableEntityError
from app.models.correction import CorrectionAttempt, CorrectionStatus
from app.models.decision import DecisionStatus
from app.models.document import Document, DocumentStatus
from app.models.validation import ValidationStatus
from app.schemas.correction import CORRECTION_POLICY_VERSION, InvoiceCorrection
from app.schemas.normalization_persistence import normalized_invoice_from_attempt
from app.services.processing.correction import lifecycle
from app.services.processing.correction.projection import (
    SubmittedInvoice,
    merge_correction,
)
from app.services.processing.correction.repository import CorrectionRepository
from app.services.processing.decision import DecisionService
from app.services.processing.decision.repository import DecisionRepository
from app.services.processing.extraction.repository import ExtractionRepository
from app.services.processing.normalization.repository import NormalizationRepository
from app.services.processing.validation import ValidationService
from app.services.processing.validation.repository import ValidationRepository

logger = logging.getLogger("app.correction")

_GENERIC_FAILURE = "The correction did not complete. Retry it to try again."
_FAILURE_CODE = "CORRECTION_FAILED"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class CorrectionInput:
    """Everything a submit needs beyond the target document id."""

    submitted: SubmittedInvoice
    reviewer_name: str
    note: str | None
    source_decision_id: uuid.UUID | None
    source_normalization_id: uuid.UUID | None
    manual_review_requested: bool = False


class CorrectionService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        repository: CorrectionRepository | None = None,
    ) -> None:
        self._session = session
        self._repo = repository or CorrectionRepository(session)
        self._extractions = ExtractionRepository(session)
        self._normalizations = NormalizationRepository(session)
        self._validations = ValidationRepository(session)
        self._decisions = DecisionRepository(session)

    # --- public API -----------------------------------------------------

    async def submit(
        self, document_id: uuid.UUID, data: CorrectionInput
    ) -> CorrectionAttempt:
        """Record and run a reviewer correction for a correctable document."""
        document = await self._lock_document(document_id)
        if document is None:
            raise NotFoundError(
                "No document exists with that ID.", code="DOCUMENT_NOT_FOUND"
            )
        lifecycle.ensure_document_can_be_corrected(document.status)
        return await self._execute(document, data, enforce_stale_check=True)

    async def retry(
        self, document_id: uuid.UUID, correction_id: uuid.UUID
    ) -> CorrectionAttempt:
        """Run a fresh correction attempt from a technically failed one."""
        document = await self._lock_document(document_id)
        if document is None:
            raise NotFoundError(
                "No document exists with that ID.", code="DOCUMENT_NOT_FOUND"
            )
        failed = await self._repo.get_for_document(document_id, correction_id)
        if failed is None:
            raise NotFoundError(
                "No correction with that ID exists for this document.",
                code="CORRECTION_NOT_FOUND",
            )
        lifecycle.ensure_can_retry(failed.status)
        lifecycle.ensure_document_can_be_corrected(document.status)
        latest = await self._repo.latest_for_document(document_id)
        if latest is None or latest.correction_id != failed.correction_id:
            raise ConflictError(
                "This failed correction has been superseded by a newer attempt.",
                code="STALE_CORRECTION_SOURCE",
            )
        payload = failed.submitted_payload
        data = CorrectionInput(
            submitted=SubmittedInvoice(
                scalars=dict(payload.get("scalars", {})),
                line_items=[dict(item) for item in payload.get("line_items", [])],
            ),
            reviewer_name=failed.reviewer_name,
            note=failed.note,
            source_decision_id=None,
            source_normalization_id=None,
            manual_review_requested=bool(payload.get("manual_review_requested", False)),
        )
        return await self._execute(
            document,
            data,
            enforce_stale_check=False,
            retry_source_normalization_id=failed.source_normalization_id,
            retry_source_decision_id=failed.source_decision_id,
        )

    # --- orchestration ------------------------------------------------- --

    async def _execute(
        self,
        document: Document,
        data: CorrectionInput,
        *,
        enforce_stale_check: bool,
        retry_source_normalization_id: uuid.UUID | None = None,
        retry_source_decision_id: uuid.UUID | None = None,
    ) -> CorrectionAttempt:
        document_id = document.document_id
        origin_status = document.status

        extraction = await self._extractions.latest_for_document(document_id)
        normalization = (
            await self._normalizations.get(retry_source_normalization_id)
            if retry_source_normalization_id is not None
            else (
                await self._normalizations.latest_for_extraction(
                    extraction.extraction_id
                )
                if extraction is not None
                else None
            )
        )
        validation = (
            await self._validations.latest_for_normalization(
                normalization.normalization_id
            )
            if normalization is not None
            else None
        )
        decision = (
            await self._decisions.get(retry_source_decision_id)
            if retry_source_decision_id is not None
            else (
                await self._decisions.latest_for_validation(validation.validation_id)
                if validation is not None
                else None
            )
        )
        lifecycle.ensure_source_decision_completed(
            decision.status if decision is not None else None
        )
        assert extraction is not None and normalization is not None and decision is not None

        if enforce_stale_check:
            if data.source_decision_id is None:
                raise UnprocessableEntityError(
                    "source_decision_id is required.", code="VALIDATION_ERROR"
                )
            lifecycle.ensure_decision_is_current_source(
                source_decision_id=data.source_decision_id,
                current_decision_id=decision.decision_id,
                source_normalization_id=data.source_normalization_id,
                current_normalization_id=normalization.normalization_id,
            )

        lifecycle.ensure_no_active_correction(
            await self._repo.active_for_document(document_id)
        )

        base = normalized_invoice_from_attempt(normalization)
        try:
            merged, entries = merge_correction(base, data.submitted)
        except Exception as exc:  # noqa: BLE001 - a caller-data problem, not a fault
            raise UnprocessableEntityError(
                "The submitted correction could not be applied to this invoice.",
                code="CORRECTION_INVALID",
            ) from exc
        if not entries:
            raise UnprocessableEntityError(
                "The submitted invoice is identical to the current data; "
                "nothing to correct.",
                code="CORRECTION_EMPTY",
            )
        correction = InvoiceCorrection(
            reviewer_name=data.reviewer_name, note=data.note, entries=entries
        )
        submitted_payload = {
            "scalars": dict(data.submitted.scalars),
            "line_items": [dict(item) for item in data.submitted.line_items],
            "manual_review_requested": data.manual_review_requested,
        }

        attempt = self._repo.add_attempt(
            document_id=document_id,
            attempt_number=await self._repo.next_attempt_number(document_id),
            source_normalization_id=normalization.normalization_id,
            source_decision_id=decision.decision_id,
            correction=correction,
            submitted_payload=submitted_payload,
            policy_version=CORRECTION_POLICY_VERSION,
            origin_document_status=origin_status,
            submitted_at=_utcnow(),
        )
        try:
            await self._session.flush()
        except IntegrityError as exc:
            await self._session.rollback()
            raise ConflictError(
                "A correction was started for this document concurrently.",
                code="CORRECTION_IN_PROGRESS",
            ) from exc
        await self._session.commit()

        correction_id = attempt.correction_id
        extraction_id = normalization.extraction_id

        # --- projection -> new normalization attempt ---------------------
        try:
            projection = self._repo.persist_projection(
                extraction_id=extraction_id,
                attempt_number=await self._normalizations.next_attempt_number(
                    extraction_id
                ),
                correction_id=correction_id,
                merged=merged,
            )
            await self._session.flush()
            new_normalization_id = projection.normalization_id
            attempt = await self._reload(correction_id)
            self._repo.record_partial(
                attempt, resulting_normalization_id=new_normalization_id
            )
            await self._session.commit()
        except Exception:
            logger.exception("correction %s failed while projecting", correction_id)
            return await self._mark_failed(correction_id)

        # --- re-run validation ----------------------------------------
        try:
            validation_attempt = await ValidationService(self._session).start(
                new_normalization_id
            )
        except Exception:
            logger.exception("correction %s failed re-running validation", correction_id)
            return await self._mark_failed(correction_id)
        attempt = await self._reload(correction_id)
        self._repo.record_partial(
            attempt, resulting_validation_id=validation_attempt.validation_id
        )
        await self._session.commit()
        if validation_attempt.status is ValidationStatus.FAILED:
            return await self._mark_failed(correction_id)

        # --- re-run decision ----------------------------------------
        try:
            decision_attempt = await DecisionService(self._session).start(
                validation_attempt.validation_id,
                manual_review_requested=data.manual_review_requested,
            )
        except Exception:
            logger.exception("correction %s failed re-running decision", correction_id)
            return await self._mark_failed(correction_id)
        if decision_attempt.status is DecisionStatus.FAILED:
            return await self._mark_failed(correction_id)

        outcome = decision_attempt.outcome
        assert outcome is not None  # COMPLETED decision has an outcome

        # --- resolve document status + finalize -----------------------
        try:
            locked = await self._lock_document(document_id)
            assert locked is not None
            new_status: DocumentStatus = lifecycle.resolve_document_status(
                origin_status=origin_status, outcome=outcome
            )
            locked.status = new_status
            attempt = await self._reload(correction_id)
            lifecycle.ensure_attempt_transition(
                attempt.status, CorrectionStatus.COMPLETED
            )
            self._repo.finalize(
                attempt,
                resulting_normalization_id=new_normalization_id,
                resulting_validation_id=validation_attempt.validation_id,
                resulting_decision_id=decision_attempt.decision_id,
                resulting_outcome=outcome,
                resulting_document_status=new_status,
            )
            await self._session.flush()
            await self._session.commit()
        except Exception:
            await self._session.rollback()
            logger.exception("correction %s failed while finalizing", correction_id)
            return await self._mark_failed(correction_id)

        return await self._reload(correction_id)

    async def _mark_failed(self, correction_id: uuid.UUID) -> CorrectionAttempt:
        await self._session.rollback()
        attempt = await self._reload(correction_id)
        lifecycle.ensure_attempt_transition(attempt.status, CorrectionStatus.FAILED)
        self._repo.mark_failed(
            attempt, code=_FAILURE_CODE, message=_GENERIC_FAILURE
        )
        await self._session.flush()
        await self._session.commit()
        return attempt

    async def _reload(self, correction_id: uuid.UUID) -> CorrectionAttempt:
        attempt = await self._repo.get(correction_id)
        assert attempt is not None  # committed moments ago
        return attempt

    async def _lock_document(self, document_id: uuid.UUID) -> Document | None:
        result = await self._session.execute(
            select(Document)
            .where(Document.document_id == document_id)
            .with_for_update()
        )
        return result.scalar_one_or_none()


__all__ = ["CorrectionService", "CorrectionInput"]
