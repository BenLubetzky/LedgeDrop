"""Validation API routes (Stage 5, step 12).

Every path hangs off a completed Stage 4 normalization attempt:

* ``POST /documents/{id}/extractions/{eid}/normalizations/{nid}/validations``
  start the first validation
* ``POST .../validations/retry``   run a new attempt after a technical failure
* ``GET  .../validations``          every attempt, newest first
* ``GET  .../validations/latest``   the most recent attempt
* ``GET  .../validations/{vid}``    one specific attempt

``404`` for an unknown document (``DOCUMENT_NOT_FOUND``), extraction
(``EXTRACTION_NOT_FOUND``), normalization (``NORMALIZATION_NOT_FOUND``), or
validation id (``VALIDATION_NOT_FOUND``); ``409`` when the normalization cannot
legally transition (``NORMALIZATION_NOT_COMPLETED``, ``VALIDATION_IN_PROGRESS``,
``NORMALIZATION_ALREADY_VALIDATED``, ``VALIDATION_FAILED``,
``VALIDATION_NOT_FAILED``). A validation that *runs* but hits a technical
failure is still a ``201`` - the attempt was created; its ``status`` is
``FAILED`` and ``failure_code`` / ``failure_message`` (both client-safe) say
why. A rule violation is not a failure - it travels inside ``data.findings`` on
a ``COMPLETED`` attempt.

There is no injected provider: validation is deterministic and offline. Stage
2-4 endpoints are unchanged.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, get_validation_service
from app.core.errors import NotFoundError
from app.models.document import Document
from app.models.normalization import NormalizationAttempt
from app.schemas.validation_api import InvoiceValidationResult, ValidationStartRequest
from app.services.processing.extraction.repository import ExtractionRepository
from app.services.processing.normalization.repository import NormalizationRepository
from app.services.processing.validation import ValidationRepository, ValidationService

router = APIRouter(prefix="/documents", tags=["validations"])

_BASE = (
    "/{document_id}/extractions/{extraction_id}"
    "/normalizations/{normalization_id}/validations"
)


async def _normalization_or_404(
    db: AsyncSession,
    document_id: uuid.UUID,
    extraction_id: uuid.UUID,
    normalization_id: uuid.UUID,
) -> NormalizationAttempt:
    """Resolve the source normalization, distinguishing which link in the
    document -> extraction -> normalization chain is unknown."""
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
    return normalization


@router.post(
    _BASE,
    response_model=InvoiceValidationResult,
    status_code=status.HTTP_201_CREATED,
    summary="Start validation for a completed normalization",
)
async def start_validation(
    document_id: uuid.UUID,
    extraction_id: uuid.UUID,
    normalization_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    service: Annotated[ValidationService, Depends(get_validation_service)],
    body: ValidationStartRequest | None = None,
) -> InvoiceValidationResult:
    await _normalization_or_404(db, document_id, extraction_id, normalization_id)
    attempt = await service.start(normalization_id)
    return InvoiceValidationResult.from_attempt(attempt)


@router.post(
    _BASE + "/retry",
    response_model=InvoiceValidationResult,
    status_code=status.HTTP_201_CREATED,
    summary="Run a new validation attempt after a technical failure",
)
async def retry_validation(
    document_id: uuid.UUID,
    extraction_id: uuid.UUID,
    normalization_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    service: Annotated[ValidationService, Depends(get_validation_service)],
    body: ValidationStartRequest | None = None,
) -> InvoiceValidationResult:
    await _normalization_or_404(db, document_id, extraction_id, normalization_id)
    attempt = await service.retry(normalization_id)
    return InvoiceValidationResult.from_attempt(attempt)


@router.get(
    _BASE,
    response_model=list[InvoiceValidationResult],
    summary="List every validation attempt for a normalization, newest first",
)
async def list_validations(
    document_id: uuid.UUID,
    extraction_id: uuid.UUID,
    normalization_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[InvoiceValidationResult]:
    await _normalization_or_404(db, document_id, extraction_id, normalization_id)
    attempts = await ValidationRepository(db).list_for_normalization(normalization_id)
    return [InvoiceValidationResult.from_attempt(a) for a in reversed(attempts)]


@router.get(
    _BASE + "/latest",
    response_model=InvoiceValidationResult,
    summary="Get the most recent validation attempt for a normalization",
)
async def latest_validation(
    document_id: uuid.UUID,
    extraction_id: uuid.UUID,
    normalization_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> InvoiceValidationResult:
    await _normalization_or_404(db, document_id, extraction_id, normalization_id)
    attempt = await ValidationRepository(db).latest_for_normalization(normalization_id)
    if attempt is None:
        raise NotFoundError(
            "This normalization has no validation attempts yet.",
            code="VALIDATION_NOT_FOUND",
        )
    return InvoiceValidationResult.from_attempt(attempt)


@router.get(
    _BASE + "/{validation_id}",
    response_model=InvoiceValidationResult,
    summary="Get one specific validation attempt",
)
async def get_validation(
    document_id: uuid.UUID,
    extraction_id: uuid.UUID,
    normalization_id: uuid.UUID,
    validation_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> InvoiceValidationResult:
    await _normalization_or_404(db, document_id, extraction_id, normalization_id)
    attempt = await ValidationRepository(db).get_for_normalization(
        normalization_id, validation_id
    )
    if attempt is None:
        raise NotFoundError(
            "No validation attempt with that ID exists for this normalization.",
            code="VALIDATION_NOT_FOUND",
        )
    return InvoiceValidationResult.from_attempt(attempt)
