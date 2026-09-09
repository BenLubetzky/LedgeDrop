"""Reviewer-correction API routes (Stage 8).

Every path hangs off the document - a correction is about the whole invoice,
not one upstream attempt:

* ``POST /documents/{id}/corrections``                    submit a correction
* ``POST /documents/{id}/corrections/{cid}/retry``        re-run a failed one
* ``GET  /documents/{id}/corrections``                    every attempt, newest first
* ``GET  /documents/{id}/corrections/latest``             the most recent attempt
* ``GET  /documents/{id}/corrections/{cid}``              one specific attempt

``404`` for an unknown document (``DOCUMENT_NOT_FOUND``) or correction id
(``CORRECTION_NOT_FOUND``); ``409`` when the document cannot be corrected
(``DOCUMENT_NOT_CORRECTABLE``, ``DECISION_NOT_COMPLETED``,
``STALE_CORRECTION_SOURCE``, ``CORRECTION_IN_PROGRESS``, ``CORRECTION_NOT_FAILED``);
``422`` for a bad body or a no-op / inapplicable correction
(``CORRECTION_EMPTY`` / ``CORRECTION_INVALID``). A correction that *runs* but
hits a technical failure is still ``201`` - the attempt row exists; its
``status`` is ``FAILED`` with a client-safe ``failure_code`` /
``failure_message``. A ``NEEDS_REVIEW`` re-decision is a normal ``201``
``COMPLETED`` attempt whose ``resulting_outcome`` is ``NEEDS_REVIEW``.

There is no injected provider: correcting is a database-only operation.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_correction_service, get_db
from app.core.errors import NotFoundError
from app.models.correction import CorrectionAttempt
from app.models.document import Document
from app.schemas.correction_api import (
    InvoiceCorrectionRequest,
    InvoiceCorrectionResult,
)
from app.schemas.pipeline_api import PipelineRunResult
from app.services.processing.correction import CorrectionService
from app.services.processing.correction.repository import CorrectionRepository
from app.services.processing.correction.service import CorrectionInput
from app.services.processing.decision.repository import DecisionRepository
from app.services.processing.extraction.repository import ExtractionRepository
from app.services.processing.normalization.repository import NormalizationRepository
from app.services.processing.validation.repository import ValidationRepository

router = APIRouter(prefix="/documents", tags=["corrections"])

_BASE = "/{document_id}/corrections"


async def _document_or_404(db: AsyncSession, document_id: uuid.UUID) -> None:
    if await db.get(Document, document_id) is None:
        raise NotFoundError(
            "No document exists with that ID.", code="DOCUMENT_NOT_FOUND"
        )


async def _pipeline_for(
    db: AsyncSession, attempt: CorrectionAttempt
) -> PipelineRunResult | None:
    """Build the re-run per-stage view from a correction's resulting chain ids."""
    if attempt.resulting_normalization_id is None:
        return None
    normalization = await NormalizationRepository(db).get(
        attempt.resulting_normalization_id
    )
    if normalization is None:
        return None
    extraction = await ExtractionRepository(db).get(normalization.extraction_id)
    if extraction is None:
        return None
    validation = (
        await ValidationRepository(db).get(attempt.resulting_validation_id)
        if attempt.resulting_validation_id is not None
        else None
    )
    decision = (
        await DecisionRepository(db).get(attempt.resulting_decision_id)
        if attempt.resulting_decision_id is not None
        else None
    )
    return PipelineRunResult.from_attempts(extraction, normalization, validation, decision)


async def _result(
    db: AsyncSession, attempt: CorrectionAttempt
) -> InvoiceCorrectionResult:
    return InvoiceCorrectionResult.from_attempt(
        attempt, pipeline=await _pipeline_for(db, attempt)
    )


@router.post(
    _BASE,
    response_model=InvoiceCorrectionResult,
    status_code=status.HTTP_201_CREATED,
    summary="Submit a reviewer correction and re-run validation + the decision",
)
async def submit_correction(
    document_id: uuid.UUID,
    body: InvoiceCorrectionRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    service: Annotated[CorrectionService, Depends(get_correction_service)],
) -> InvoiceCorrectionResult:
    await _document_or_404(db, document_id)
    attempt = await service.submit(
        document_id,
        CorrectionInput(
            submitted=body.to_submitted(),
            reviewer_name=body.reviewer_name,
            note=body.note,
            source_decision_id=body.source_decision_id,
            source_normalization_id=body.source_normalization_id,
            manual_review_requested=body.manual_review_requested,
        ),
    )
    return await _result(db, attempt)


@router.post(
    _BASE + "/{correction_id}/retry",
    response_model=InvoiceCorrectionResult,
    status_code=status.HTTP_201_CREATED,
    summary="Re-run a correction that failed technically, as a new attempt",
)
async def retry_correction(
    document_id: uuid.UUID,
    correction_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    service: Annotated[CorrectionService, Depends(get_correction_service)],
) -> InvoiceCorrectionResult:
    await _document_or_404(db, document_id)
    attempt = await service.retry(document_id, correction_id)
    return await _result(db, attempt)


@router.get(
    _BASE,
    response_model=list[InvoiceCorrectionResult],
    summary="List every correction attempt for a document, newest first",
)
async def list_corrections(
    document_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[InvoiceCorrectionResult]:
    await _document_or_404(db, document_id)
    attempts = await CorrectionRepository(db).list_for_document(document_id)
    return [await _result(db, attempt) for attempt in reversed(attempts)]


@router.get(
    _BASE + "/latest",
    response_model=InvoiceCorrectionResult,
    summary="Get the most recent correction attempt for a document",
)
async def latest_correction(
    document_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> InvoiceCorrectionResult:
    await _document_or_404(db, document_id)
    attempt = await CorrectionRepository(db).latest_for_document(document_id)
    if attempt is None:
        raise NotFoundError(
            "This document has no corrections yet.", code="CORRECTION_NOT_FOUND"
        )
    return await _result(db, attempt)


@router.get(
    _BASE + "/{correction_id}",
    response_model=InvoiceCorrectionResult,
    summary="Get one specific correction attempt",
)
async def get_correction(
    document_id: uuid.UUID,
    correction_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> InvoiceCorrectionResult:
    await _document_or_404(db, document_id)
    attempt = await CorrectionRepository(db).get_for_document(document_id, correction_id)
    if attempt is None:
        raise NotFoundError(
            "No correction with that ID exists for this document.",
            code="CORRECTION_NOT_FOUND",
        )
    return await _result(db, attempt)
