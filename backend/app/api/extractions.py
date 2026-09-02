"""Extraction API routes (Stage 3, step 7).

All paths hang off an existing document:

* ``POST /documents/{id}/extractions``        start the first extraction
* ``POST /documents/{id}/extractions/retry``  run a new attempt after a failure
* ``GET  /documents/{id}/extractions``         every attempt, newest first
* ``GET  /documents/{id}/extractions/latest``  the most recent attempt
* ``GET  /documents/{id}/extractions/{eid}``   one specific attempt

``404`` for an unknown document or extraction id; ``409`` when the document
cannot legally transition (already processing, already extracted, not failed).
An extraction that *runs* but fails is still a ``201`` - the attempt was
created; its ``status`` is ``FAILED`` and ``failure_code`` says why.

The extractor is injected (:func:`app.api.deps.get_extractor`), selected by
``EXTRACTION_PROVIDER`` - the deterministic offline fake by default, or the
real GPT-5-mini adapter. The started/retried attempt records whichever
provider actually ran it.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, get_extraction_service, get_extractor, get_prepared_result_producer
from app.core.errors import NotFoundError
from app.models.document import Document
from app.schemas.extraction_api import ExtractionStartRequest, InvoiceExtractionResult
from app.services.processing.extraction import ExtractionService, ResultProducer
from app.services.processing.extraction.provider import ExtractionProvider
from app.services.processing.extraction.repository import ExtractionRepository

router = APIRouter(prefix="/documents", tags=["extractions"])


async def _document_or_404(db: AsyncSession, document_id: uuid.UUID) -> Document:
    document = await db.get(Document, document_id)
    if document is None:
        raise NotFoundError("No document exists with that ID.", code="DOCUMENT_NOT_FOUND")
    return document


@router.post(
    "/{document_id}/extractions",
    response_model=InvoiceExtractionResult,
    status_code=status.HTTP_201_CREATED,
    summary="Start the first extraction for a document",
)
async def start_extraction(
    document_id: uuid.UUID,
    service: Annotated[ExtractionService, Depends(get_extraction_service)],
    produce: Annotated[ResultProducer, Depends(get_prepared_result_producer)],
    provider: Annotated[ExtractionProvider, Depends(get_extractor)],
    body: ExtractionStartRequest | None = None,
) -> InvoiceExtractionResult:
    attempt = await service.start(
        document_id,
        produce=produce,
        provider_name=provider.name,
        provider_model=getattr(provider, "model", None),
    )
    return InvoiceExtractionResult.from_attempt(attempt)


@router.post(
    "/{document_id}/extractions/retry",
    response_model=InvoiceExtractionResult,
    status_code=status.HTTP_201_CREATED,
    summary="Run a new extraction attempt after a failed one",
)
async def retry_extraction(
    document_id: uuid.UUID,
    service: Annotated[ExtractionService, Depends(get_extraction_service)],
    produce: Annotated[ResultProducer, Depends(get_prepared_result_producer)],
    provider: Annotated[ExtractionProvider, Depends(get_extractor)],
    body: ExtractionStartRequest | None = None,
) -> InvoiceExtractionResult:
    attempt = await service.retry(
        document_id,
        produce=produce,
        provider_name=provider.name,
        provider_model=getattr(provider, "model", None),
    )
    return InvoiceExtractionResult.from_attempt(attempt)


@router.get(
    "/{document_id}/extractions",
    response_model=list[InvoiceExtractionResult],
    summary="List every extraction attempt for a document, newest first",
)
async def list_extractions(
    document_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[InvoiceExtractionResult]:
    await _document_or_404(db, document_id)
    attempts = await ExtractionRepository(db).list_for_document(document_id)
    return [InvoiceExtractionResult.from_attempt(a) for a in reversed(attempts)]


@router.get(
    "/{document_id}/extractions/latest",
    response_model=InvoiceExtractionResult,
    summary="Get the most recent extraction attempt for a document",
)
async def latest_extraction(
    document_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> InvoiceExtractionResult:
    await _document_or_404(db, document_id)
    attempt = await ExtractionRepository(db).latest_for_document(document_id)
    if attempt is None:
        raise NotFoundError(
            "This document has no extraction attempts yet.",
            code="EXTRACTION_NOT_FOUND",
        )
    return InvoiceExtractionResult.from_attempt(attempt)


@router.get(
    "/{document_id}/extractions/{extraction_id}",
    response_model=InvoiceExtractionResult,
    summary="Get one specific extraction attempt",
)
async def get_extraction(
    document_id: uuid.UUID,
    extraction_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> InvoiceExtractionResult:
    await _document_or_404(db, document_id)
    attempt = await ExtractionRepository(db).get_for_document(document_id, extraction_id)
    if attempt is None:
        raise NotFoundError(
            "No extraction attempt with that ID exists for this document.",
            code="EXTRACTION_NOT_FOUND",
        )
    return InvoiceExtractionResult.from_attempt(attempt)
