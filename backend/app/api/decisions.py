"""Decision API routes (Stage 6, package 5).

Every path hangs off a completed Stage 5 validation attempt:

* ``POST /documents/{id}/extractions/{eid}/normalizations/{nid}/validations/{vid}/decisions``
  start the first decision
* ``POST .../decisions/retry``   run a new attempt after a technical failure
* ``GET  .../decisions``          every attempt, newest first
* ``GET  .../decisions/latest``   the most recent attempt
* ``GET  .../decisions/{did}``    one specific attempt

``404`` for an unknown document (``DOCUMENT_NOT_FOUND``), extraction
(``EXTRACTION_NOT_FOUND``), normalization (``NORMALIZATION_NOT_FOUND``),
validation (``VALIDATION_NOT_FOUND``), or decision id (``DECISION_NOT_FOUND``);
``409`` when the validation cannot legally transition
(``VALIDATION_NOT_COMPLETED``, ``DECISION_IN_PROGRESS``,
``VALIDATION_ALREADY_DECIDED``, ``DECISION_FAILED``, ``DECISION_NOT_FAILED``,
``STALE_VALIDATION_SOURCE``). A decision that *runs* but hits a technical
failure is still a ``201`` - the attempt was created; its ``status`` is
``FAILED`` and ``failure_code`` / ``failure_message`` (both client-safe) say
why. A ``NEEDS_REVIEW`` outcome is **not** a failure - it is a ``COMPLETED``
attempt whose ``outcome`` is ``NEEDS_REVIEW``, and it is the point at which the
owning document moves to ``NEEDS_REVIEW`` (``docs/stage-6-decision.md`` §6.2).

There is no injected provider: deciding is deterministic and offline. Stage
2-5 endpoints are unchanged.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, get_decision_service
from app.core.errors import NotFoundError
from app.models.document import Document
from app.models.validation import ValidationAttempt
from app.schemas.decision_api import DecisionStartRequest, InvoiceDecisionResult
from app.services.processing.decision import DecisionService
from app.services.processing.decision.repository import DecisionRepository
from app.services.processing.extraction.repository import ExtractionRepository
from app.services.processing.normalization.repository import NormalizationRepository
from app.services.processing.validation.repository import ValidationRepository

router = APIRouter(prefix="/documents", tags=["decisions"])

_BASE = (
    "/{document_id}/extractions/{extraction_id}"
    "/normalizations/{normalization_id}/validations/{validation_id}/decisions"
)


async def _validation_or_404(
    db: AsyncSession,
    document_id: uuid.UUID,
    extraction_id: uuid.UUID,
    normalization_id: uuid.UUID,
    validation_id: uuid.UUID,
) -> ValidationAttempt:
    """Resolve the source validation, distinguishing which link in the
    document -> extraction -> normalization -> validation chain is unknown."""
    if await db.get(Document, document_id) is None:
        raise NotFoundError(
            "No document exists with that ID.", code="DOCUMENT_NOT_FOUND"
        )
    extraction = await ExtractionRepository(db).get_for_document(
        document_id, extraction_id
    )
    if extraction is None:
        raise NotFoundError(
            "No extraction attempt with that ID exists for this document.",
            code="EXTRACTION_NOT_FOUND",
        )
    normalization = await NormalizationRepository(db).get_for_extraction(
        extraction_id, normalization_id
    )
    if normalization is None:
        raise NotFoundError(
            "No normalization attempt with that ID exists for this extraction.",
            code="NORMALIZATION_NOT_FOUND",
        )
    validation = await ValidationRepository(db).get_for_normalization(
        normalization_id, validation_id
    )
    if validation is None:
        raise NotFoundError(
            "No validation attempt with that ID exists for this normalization.",
            code="VALIDATION_NOT_FOUND",
        )
    return validation


@router.post(
    _BASE,
    response_model=InvoiceDecisionResult,
    status_code=status.HTTP_201_CREATED,
    summary="Start a decision for a completed validation",
)
async def start_decision(
    document_id: uuid.UUID,
    extraction_id: uuid.UUID,
    normalization_id: uuid.UUID,
    validation_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    service: Annotated[DecisionService, Depends(get_decision_service)],
    body: DecisionStartRequest | None = None,
) -> InvoiceDecisionResult:
    await _validation_or_404(
        db, document_id, extraction_id, normalization_id, validation_id
    )
    manual_review = body.manual_review_requested if body is not None else False
    attempt = await service.start(
        validation_id, manual_review_requested=manual_review
    )
    return InvoiceDecisionResult.from_attempt(attempt)


@router.post(
    _BASE + "/retry",
    response_model=InvoiceDecisionResult,
    status_code=status.HTTP_201_CREATED,
    summary="Run a new decision attempt after a technical failure",
)
async def retry_decision(
    document_id: uuid.UUID,
    extraction_id: uuid.UUID,
    normalization_id: uuid.UUID,
    validation_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    service: Annotated[DecisionService, Depends(get_decision_service)],
    body: DecisionStartRequest | None = None,
) -> InvoiceDecisionResult:
    await _validation_or_404(
        db, document_id, extraction_id, normalization_id, validation_id
    )
    manual_review = body.manual_review_requested if body is not None else False
    attempt = await service.retry(
        validation_id, manual_review_requested=manual_review
    )
    return InvoiceDecisionResult.from_attempt(attempt)


@router.get(
    _BASE,
    response_model=list[InvoiceDecisionResult],
    summary="List every decision attempt for a validation, newest first",
)
async def list_decisions(
    document_id: uuid.UUID,
    extraction_id: uuid.UUID,
    normalization_id: uuid.UUID,
    validation_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[InvoiceDecisionResult]:
    await _validation_or_404(
        db, document_id, extraction_id, normalization_id, validation_id
    )
    attempts = await DecisionRepository(db).list_for_validation(validation_id)
    return [InvoiceDecisionResult.from_attempt(a) for a in reversed(list(attempts))]


@router.get(
    _BASE + "/latest",
    response_model=InvoiceDecisionResult,
    summary="Get the most recent decision attempt for a validation",
)
async def latest_decision(
    document_id: uuid.UUID,
    extraction_id: uuid.UUID,
    normalization_id: uuid.UUID,
    validation_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> InvoiceDecisionResult:
    await _validation_or_404(
        db, document_id, extraction_id, normalization_id, validation_id
    )
    attempt = await DecisionRepository(db).latest_for_validation(validation_id)
    if attempt is None:
        raise NotFoundError(
            "This validation has no decision attempts yet.",
            code="DECISION_NOT_FOUND",
        )
    return InvoiceDecisionResult.from_attempt(attempt)


@router.get(
    _BASE + "/{decision_id}",
    response_model=InvoiceDecisionResult,
    summary="Get one specific decision attempt",
)
async def get_decision(
    document_id: uuid.UUID,
    extraction_id: uuid.UUID,
    normalization_id: uuid.UUID,
    validation_id: uuid.UUID,
    decision_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> InvoiceDecisionResult:
    await _validation_or_404(
        db, document_id, extraction_id, normalization_id, validation_id
    )
    attempt = await DecisionRepository(db).get_for_validation(
        validation_id, decision_id
    )
    if attempt is None:
        raise NotFoundError(
            "No decision attempt with that ID exists for this validation.",
            code="DECISION_NOT_FOUND",
        )
    return InvoiceDecisionResult.from_attempt(attempt)
