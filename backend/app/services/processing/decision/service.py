"""Decision orchestration (Stage 6, package 4).

``DecisionService`` drives one decision attempt through the Stage 6
lifecycle::

    COMPLETED validation  ->  PROCESSING  ->  COMPLETED | FAILED

and guarantees the same properties Stage 3/4/5's services do, extended with
the document-status write and the stale-source guard
(``docs/stage-6-decision.md`` Part 6):

* **PROCESSING is durable before work starts.** The attempt row is committed
  ``PROCESSING`` *before* the engine runs, so an interrupted run leaves a
  visible record rather than a silent gap.
* **One active attempt per source validation.** A ``SELECT ... FOR UPDATE``
  on the source ``invoice_validations`` row (and, in the same query, the
  owning ``documents`` row) serialises concurrent starts; the partial unique
  index on ``invoice_decisions`` is the backstop.
* **A review outcome is not a technical failure.** The engine's reasons are
  persisted inside the result and the attempt still ends ``COMPLETED``. Only
  an infrastructure problem - the source validation cannot be read, the
  engine raises an unexpected exception, or a database write fails - ends an
  attempt ``FAILED`` with a client-safe ``failure_code`` / ``failure_message``
  and no partial reasons.
* **The source is never touched.** A decision attempt only reads the Stage 5
  validation (and, through it, Stage 2-4 data); a failure leaves all of it
  intact and a retry is allowed.
* **History is preserved.** A retry always creates a new attempt
  (``attempt_number + 1``); earlier attempts are never mutated or deleted.
* **The document's current chain is protected.** A decision can only be
  based on the document's latest extraction/normalization (§6.4); only a
  ``NEEDS_REVIEW`` outcome ever changes ``documents.status`` (§6.2) - an
  ``ACCEPTED`` outcome leaves it ``COMPLETED``, and a technical failure
  leaves it untouched.

Deciding makes **no AI call and no external-network call** - the engine is
pure, in-process, deterministic Python; only the source lookups and the
persistence writes touch the database.

``manual_review_requested`` (spec §2.4) is a per-attempt input: ``start`` and
``retry`` both accept it and pass it straight to
:func:`app.services.processing.decision.engine.decide`. It only ever *adds* a
``manual_review_requested`` reason to the attempt it drives; it never removes
or downgrades a rule-derived reason and there is no "force accept". A
``retry`` builds a fresh attempt, so the flag must be supplied again if it is
still wanted - the previous (failed) attempt persisted nothing.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError
from app.models.decision import DecisionAttempt, DecisionStatus
from app.models.document import Document
from app.models.extraction import ExtractionAttempt
from app.models.normalization import NormalizationAttempt
from app.models.validation import ValidationAttempt
from app.schemas.decision import InvoiceDecision
from app.schemas.decision_catalogue import POLICY_VERSION
from app.schemas.validation_persistence import invoice_validation_from_rows
from app.services.processing.decision import lifecycle
from app.services.processing.decision.engine import decide
from app.services.processing.decision.repository import DecisionRepository
from app.services.processing.extraction.repository import ExtractionRepository
from app.services.processing.normalization.repository import NormalizationRepository
from app.services.processing.validation.repository import ValidationRepository

logger = logging.getLogger("app.decision")

# Client-safe fallback for any technical failure. Real diagnostics go to the
# log, never onto the row.
_GENERIC_FAILURE = "Decision did not complete. Retry the decision to try again."
_FAILURE_CODE = "DECISION_FAILED"

_SourceChain = tuple[ValidationAttempt, Document, uuid.UUID, uuid.UUID]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class DecisionService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        repository: DecisionRepository | None = None,
    ) -> None:
        self._session = session
        self._repo = repository or DecisionRepository(session)
        self._extraction_repo = ExtractionRepository(session)
        self._normalization_repo = NormalizationRepository(session)
        self._validation_repo = ValidationRepository(session)

    # --- public API -------------------------------------------------------

    async def start(
        self, validation_id: uuid.UUID, *, manual_review_requested: bool = False
    ) -> DecisionAttempt:
        """Run the first decision for a ``COMPLETED`` validation attempt."""
        return await self._run(
            validation_id,
            action="start",
            manual_review_requested=manual_review_requested,
        )

    async def retry(
        self, validation_id: uuid.UUID, *, manual_review_requested: bool = False
    ) -> DecisionAttempt:
        """Run a fresh attempt for a validation whose last decision FAILED."""
        return await self._run(
            validation_id,
            action="retry",
            manual_review_requested=manual_review_requested,
        )

    # --- orchestration ------------------------------------------------- --

    async def _run(
        self,
        validation_id: uuid.UUID,
        *,
        action: lifecycle.Action,
        manual_review_requested: bool = False,
    ) -> DecisionAttempt:
        # Validate caller input before locking the source or durably creating
        # an attempt. The pure engine has the same strict guard, but reaching
        # it happens only after PROCESSING is committed; treating an invalid
        # value such as ``"false"`` as a technical decision failure would
        # fabricate a retryable audit row for a caller error.
        if type(manual_review_requested) is not bool:
            raise TypeError("manual_review_requested must be a bool")

        source = await self._lock_source(validation_id)
        if source is None:
            raise NotFoundError(
                "No validation exists with that ID.", code="VALIDATION_NOT_FOUND"
            )
        validation, document, chain_extraction_id, chain_normalization_id = source

        latest = await self._repo.latest_for_validation(validation_id)
        lifecycle.ensure_validation_can_decide(
            validation.status,
            latest.status if latest is not None else None,
            action=action,
        )

        await self._ensure_current_source(
            document.document_id, chain_extraction_id, chain_normalization_id
        )

        if await self._repo.active_for_validation(validation_id) is not None:
            raise ConflictError(
                "A decision is already in progress for this validation.",
                code="DECISION_IN_PROGRESS",
            )

        attempt = self._repo.add_attempt(
            validation_id=validation_id,
            attempt_number=await self._repo.next_attempt_number(validation_id),
            policy_version=POLICY_VERSION,
        )
        try:
            await self._session.flush()
        except IntegrityError as exc:
            # Lost a race for the single active slot, or the attempt number.
            await self._session.rollback()
            raise ConflictError(
                "A decision was started for this validation concurrently.",
                code="DECISION_IN_PROGRESS",
            ) from exc
        await self._session.commit()

        decision_id = attempt.decision_id
        document_id = document.document_id

        try:
            result, source_finding_ids = await self._evaluate(
                validation_id, manual_review_requested=manual_review_requested
            )
        except Exception:
            logger.exception("decision %s raised while evaluating", decision_id)
            return await self._mark_failed(decision_id)

        return await self._complete(decision_id, document_id, result, source_finding_ids)

    async def _ensure_current_source(
        self,
        document_id: uuid.UUID,
        chain_extraction_id: uuid.UUID,
        chain_normalization_id: uuid.UUID,
    ) -> None:
        """Raise if the validation under decision is not the document's current result."""
        current_extraction = await self._extraction_repo.latest_for_document(document_id)
        current_normalization = (
            await self._normalization_repo.latest_for_extraction(
                current_extraction.extraction_id
            )
            if current_extraction is not None
            else None
        )
        lifecycle.ensure_validation_is_current_source(
            chain_extraction_id=chain_extraction_id,
            chain_normalization_id=chain_normalization_id,
            current_extraction_id=(
                current_extraction.extraction_id if current_extraction is not None else None
            ),
            current_normalization_id=(
                current_normalization.normalization_id
                if current_normalization is not None
                else None
            ),
        )

    async def _evaluate(
        self, validation_id: uuid.UUID, *, manual_review_requested: bool = False
    ) -> tuple[InvoiceDecision, list[uuid.UUID | None]]:
        """Re-read the completed validation and decide it. Makes no writes.

        The findings come back ``position``-ordered (the relationship's own
        ordering), so ``source_finding_ids`` lines up index-for-index with
        ``decide``'s reasons without any extra matching logic (spec Part 5).
        When ``manual_review_requested`` is set, ``decide`` appends exactly one
        more reason with no finding behind it, so a trailing ``None`` is added
        to keep the two sequences the same length for
        :func:`app.schemas.decision_persistence.reason_rows`.
        """
        validation_attempt = await self._validation_repo.get(validation_id)
        assert validation_attempt is not None  # confirmed COMPLETED moments ago
        validation = invoice_validation_from_rows(validation_attempt.findings)
        result = decide(validation, manual_review_requested=manual_review_requested)
        source_finding_ids: list[uuid.UUID | None] = [
            row.validation_finding_id for row in validation_attempt.findings
        ]
        if manual_review_requested:
            source_finding_ids.append(None)
        return result, source_finding_ids

    async def _complete(
        self,
        decision_id: uuid.UUID,
        document_id: uuid.UUID,
        result: InvoiceDecision,
        source_finding_ids: list[uuid.UUID | None],
    ) -> DecisionAttempt:
        attempt = await self._repo.get(decision_id)
        assert attempt is not None  # committed moments ago

        try:
            # The source was current when the PROCESSING attempt was created,
            # but that transaction released its document lock before the pure
            # evaluator ran. Lock and validate the chain again in the same
            # transaction that writes the outcome, so a newer chain cannot
            # appear in that interval and then be overwritten by this stale
            # decision's document-status update.
            source = await self._lock_source(attempt.validation_id)
            if source is None:
                raise RuntimeError("decision source disappeared before completion")
            _, document, chain_extraction_id, chain_normalization_id = source
            if document.document_id != document_id:
                raise RuntimeError("decision source document changed before completion")
            await self._ensure_current_source(
                document_id, chain_extraction_id, chain_normalization_id
            )

            self._repo.apply_result(attempt, result, source_finding_ids)
            lifecycle.ensure_attempt_transition(attempt.status, DecisionStatus.COMPLETED)
            attempt.status = DecisionStatus.COMPLETED
            attempt.completed_at = _utcnow()
            new_status = lifecycle.document_status_for_outcome(result.outcome)
            if new_status is not None:
                document.status = new_status
            await self._session.flush()
            await self._session.commit()
        except Exception:
            await self._session.rollback()
            logger.exception("persisting completed decision %s failed", decision_id)
            return await self._mark_failed(decision_id)
        return attempt

    async def _mark_failed(self, decision_id: uuid.UUID) -> DecisionAttempt:
        # Discard anything half-written by a failed completion, then record the
        # failure against the already-committed PROCESSING attempt.
        # documents.status is deliberately left untouched (§6.2): a technical
        # decision failure is not a fact about the invoice.
        await self._session.rollback()
        attempt = await self._repo.get(decision_id)
        assert attempt is not None

        lifecycle.ensure_attempt_transition(attempt.status, DecisionStatus.FAILED)
        attempt.status = DecisionStatus.FAILED
        attempt.completed_at = _utcnow()
        attempt.failure_code = _FAILURE_CODE
        attempt.failure_message = _GENERIC_FAILURE

        await self._session.flush()
        await self._session.commit()
        return attempt

    async def _lock_source(self, validation_id: uuid.UUID) -> _SourceChain | None:
        """Resolve and lock the validation's owning document, in one query.

        Returns ``(validation, document, chain_extraction_id,
        chain_normalization_id)``, or ``None`` if no such validation exists.
        ``FOR UPDATE OF`` names only ``ValidationAttempt`` and ``Document``,
        so Postgres locks those two rows and not the joined
        extraction/normalization rows: the validation lock serialises
        concurrent decision starts (mirroring Stage 5's own source lock), and
        the document lock keeps the §6.4 staleness check and any later
        document-status write consistent with each other.
        """
        result = await self._session.execute(
            select(ValidationAttempt, Document, ExtractionAttempt.extraction_id)
            .join(
                NormalizationAttempt,
                NormalizationAttempt.normalization_id == ValidationAttempt.normalization_id,
            )
            .join(
                ExtractionAttempt,
                ExtractionAttempt.extraction_id == NormalizationAttempt.extraction_id,
            )
            .join(Document, Document.document_id == ExtractionAttempt.document_id)
            .where(ValidationAttempt.validation_id == validation_id)
            .with_for_update(of=[ValidationAttempt, Document])
        )
        row = result.one_or_none()
        if row is None:
            return None
        validation, document, extraction_id = row
        return validation, document, extraction_id, validation.normalization_id


__all__ = ["DecisionService"]
