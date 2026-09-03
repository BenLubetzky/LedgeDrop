"""Normalization API routes (Stage 4, step 12).

Every path hangs off a completed Stage 3 extraction attempt:

* ``POST /documents/{id}/extractions/{eid}/normalizations``        start the first normalization
* ``POST /documents/{id}/extractions/{eid}/normalizations/retry``  run a new attempt after a technical failure
* ``GET  /documents/{id}/extractions/{eid}/normalizations``         every attempt, newest first
* ``GET  /documents/{id}/extractions/{eid}/normalizations/latest``  the most recent attempt
* ``GET  /documents/{id}/extractions/{eid}/normalizations/{nid}``   one specific attempt

``404`` for an unknown document (``DOCUMENT_NOT_FOUND``), extraction
(``EXTRACTION_NOT_FOUND``), or normalization id (``NORMALIZATION_NOT_FOUND``);
``409`` when the extraction cannot legally transition
(``EXTRACTION_NOT_COMPLETED``, ``NORMALIZATION_IN_PROGRESS``,
``EXTRACTION_ALREADY_NORMALIZED``, ``NORMALIZATION_FAILED``,
``NORMALIZATION_NOT_FAILED``). A normalization that *runs* but hits a technical
failure is still a ``201`` - the attempt was created; its ``status`` is
``FAILED`` and ``failure_code`` / ``failure_message`` (both client-safe) say
why. A field-level normalization error is not a failure - it travels inside
``data.errors``.

There is no injected provider: normalization is deterministic and offline.
Stage 2 and Stage 3 endpoints are unchanged.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, get_normalization_service
from app.core.errors import NotFoundError
from app.models.document import Document
from app.models.extraction import ExtractionAttempt
from app.schemas.normalization_api import (
    InvoiceNormalizationResult,
    NormalizationStartRequest,
)
from app.services.processing.extraction.repository import ExtractionRepository
from app.services.processing.normalization import NormalizationService
from app.services.processing.normalization.repository import NormalizationRepository

router = APIRouter(prefix="/documents", tags=["normalizations"])

_BASE = "/{document_id}/extractions/{extraction_id}/normalizations"


async def _extraction_or_404(
    db: AsyncSession, document_id: uuid.UUID, extraction_id: uuid.UUID
) -> ExtractionAttempt:
    """Resolve the source extraction, distinguishing an unknown document from an
    unknown (or wrong-document) extraction."""
    if await db.get(Document, document_id) is None:
        raise NotFoundError(
            "No document exists with that ID.", code="DOCUMENT_NOT_FOUND"
        )
    attempt = await ExtractionRepository(db).get_for_document(document_id, extraction_id)
    if attempt is None:
        raise NotFoundError(
            "No extraction attempt with that ID exists for this document.",
            code="EXTRACTION_NOT_FOUND",
        )
    return attempt


@router.post(
    _BASE,
    response_model=InvoiceNormalizationResult,
    status_code=status.HTTP_201_CREATED,
    summary="Start normalization for a completed extraction",
)
async def start_normalization(
    document_id: uuid.UUID,
    extraction_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    service: Annotated[NormalizationService, Depends(get_normalization_service)],
    body: NormalizationStartRequest | None = None,
) -> InvoiceNormalizationResult:
    await _extraction_or_404(db, document_id, extraction_id)
    attempt = await service.start(extraction_id)
    return InvoiceNormalizationResult.from_attempt(attempt)


@router.post(
    _BASE + "/retry",
    response_model=InvoiceNormalizationResult,
    status_code=status.HTTP_201_CREATED,
    summary="Run a new normalization attempt after a technical failure",
)
async def retry_normalization(
    document_id: uuid.UUID,
    extraction_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    service: Annotated[NormalizationService, Depends(get_normalization_service)],
    body: NormalizationStartRequest | None = None,
) -> InvoiceNormalizationResult:
    await _extraction_or_404(db, document_id, extraction_id)
    attempt = await service.retry(extraction_id)
    return InvoiceNormalizationResult.from_attempt(attempt)


@router.get(
    _BASE,
    response_model=list[InvoiceNormalizationResult],
    summary="List every normalization attempt for an extraction, newest first",
)
async def list_normalizations(
    document_id: uuid.UUID,
    extraction_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[InvoiceNormalizationResult]:
    await _extraction_or_404(db, document_id, extraction_id)
    attempts = await NormalizationRepository(db).list_for_extraction(extraction_id)
    return [InvoiceNormalizationResult.from_attempt(a) for a in reversed(attempts)]


@router.get(
    _BASE + "/latest",
    response_model=InvoiceNormalizationResult,
    summary="Get the most recent normalization attempt for an extraction",
)
async def latest_normalization(
    document_id: uuid.UUID,
    extraction_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> InvoiceNormalizationResult:
    await _extraction_or_404(db, document_id, extraction_id)
    attempt = await NormalizationRepository(db).latest_for_extraction(extraction_id)
    if attempt is None:
        raise NotFoundError(
            "This extraction has no normalization attempts yet.",
            code="NORMALIZATION_NOT_FOUND",
        )
    return InvoiceNormalizationResult.from_attempt(attempt)


@router.get(
    _BASE + "/{normalization_id}",
    response_model=InvoiceNormalizationResult,
    summary="Get one specific normalization attempt",
)
async def get_normalization(
    document_id: uuid.UUID,
    extraction_id: uuid.UUID,
    normalization_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> InvoiceNormalizationResult:
    await _extraction_or_404(db, document_id, extraction_id)
    attempt = await NormalizationRepository(db).get_for_extraction(
        extraction_id, normalization_id
    )
    if attempt is None:
        raise NotFoundError(
            "No normalization attempt with that ID exists for this extraction.",
            code="NORMALIZATION_NOT_FOUND",
        )
    return InvoiceNormalizationResult.from_attempt(attempt)
