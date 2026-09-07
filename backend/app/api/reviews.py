"""Human-review API routes (Stage 7, package 2).

Two flat routes for the reviewer workflow:

* ``GET  /reviews/queue``        documents awaiting a human decision, FIFO
* ``GET  /reviews/{review_id}``  one recorded resolution, by id

and two scoped to the decision being resolved, nested like every other stage so
each broken link in the chain gets its own ``404``:

* ``POST .../validations/{vid}/decisions/{did}/review``   submit APPROVE / REJECT
* ``GET  .../validations/{vid}/decisions/{did}/review``   the resolution, if any

``404`` for an unknown document / extraction / normalization / validation /
decision id (``DOCUMENT_NOT_FOUND`` ... ``DECISION_NOT_FOUND``) or review id
(``REVIEW_NOT_FOUND``). ``409`` when the decision cannot be reviewed
(``DECISION_NOT_REVIEWABLE`` - not a ``COMPLETED`` ``NEEDS_REVIEW`` decision, or
a Stage 6 ``ACCEPTED`` result, which is never overridable here),
(``DECISION_ALREADY_REVIEWED``), or (``STALE_DECISION_SOURCE``). A successful
submit is ``201`` and moves the owning document ``NEEDS_REVIEW -> APPROVED |
REJECTED``; it is terminal, with no retry or re-open route (a technical submit
failure persists nothing, so the caller simply submits again).

There is no injected provider: reviewing is a database-only operation. Stage 2-6
endpoints are unchanged.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, get_review_service
from app.core.errors import NotFoundError
from app.models.decision import DecisionAttempt
from app.models.document import Document
from app.schemas.review import InvoiceReview
from app.schemas.review_api import (
    InvoiceReviewResult,
    ReviewQueueEntry,
    ReviewSubmitRequest,
)
from app.services.processing.decision.repository import DecisionRepository
from app.services.processing.extraction.repository import ExtractionRepository
from app.services.processing.normalization.repository import NormalizationRepository
from app.services.processing.review import ReviewService
from app.services.processing.review.repository import ReviewRepository
from app.services.processing.validation.repository import ValidationRepository

router = APIRouter(tags=["reviews"])

_SCOPED = (
    "/documents/{document_id}/extractions/{extraction_id}"
    "/normalizations/{normalization_id}/validations/{validation_id}"
    "/decisions/{decision_id}/review"
)


async def _decision_or_404(
    db: AsyncSession,
    document_id: uuid.UUID,
    extraction_id: uuid.UUID,
    normalization_id: uuid.UUID,
    validation_id: uuid.UUID,
    decision_id: uuid.UUID,
) -> DecisionAttempt:
    """Walk document -> extraction -> normalization -> validation -> decision,
    raising a link-specific ``404`` for the first hop that does not resolve."""
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
    decision = await DecisionRepository(db).get_for_validation(
        validation_id, decision_id
    )
    if decision is None:
        raise NotFoundError(
            "No decision attempt with that ID exists for this validation.",
            code="DECISION_NOT_FOUND",
        )
    return decision


@router.get(
    "/reviews/queue",
    response_model=list[ReviewQueueEntry],
    summary="List documents awaiting human review, oldest first",
)
async def review_queue(
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[ReviewQueueEntry]:
    rows = await ReviewRepository(db).queue(limit=limit, offset=offset)
    return [ReviewQueueEntry.from_row(*row) for row in rows]


@router.get(
    "/reviews/{review_id}",
    response_model=InvoiceReviewResult,
    summary="Get one recorded review by id",
)
async def get_review(
    review_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> InvoiceReviewResult:
    found = await ReviewRepository(db).get_with_document(review_id)
    if found is None:
        raise NotFoundError(
            "No review exists with that ID.", code="REVIEW_NOT_FOUND"
        )
    record, document_id = found
    return InvoiceReviewResult.from_record(record, document_id=document_id)


@router.post(
    _SCOPED,
    response_model=InvoiceReviewResult,
    status_code=status.HTTP_201_CREATED,
    summary="Approve or reject a decision that needs review",
)
async def submit_review(
    document_id: uuid.UUID,
    extraction_id: uuid.UUID,
    normalization_id: uuid.UUID,
    validation_id: uuid.UUID,
    decision_id: uuid.UUID,
    body: ReviewSubmitRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    service: Annotated[ReviewService, Depends(get_review_service)],
) -> InvoiceReviewResult:
    await _decision_or_404(
        db, document_id, extraction_id, normalization_id, validation_id, decision_id
    )
    review = InvoiceReview.model_validate(body.model_dump())
    record = await service.submit(decision_id, review)
    return InvoiceReviewResult.from_record(record, document_id=document_id)


@router.get(
    _SCOPED,
    response_model=InvoiceReviewResult,
    summary="Get the review recorded for a decision, if any",
)
async def get_decision_review(
    document_id: uuid.UUID,
    extraction_id: uuid.UUID,
    normalization_id: uuid.UUID,
    validation_id: uuid.UUID,
    decision_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> InvoiceReviewResult:
    await _decision_or_404(
        db, document_id, extraction_id, normalization_id, validation_id, decision_id
    )
    record = await ReviewRepository(db).get_by_decision(decision_id)
    if record is None:
        raise NotFoundError(
            "This decision has not been reviewed.", code="REVIEW_NOT_FOUND"
        )
    return InvoiceReviewResult.from_record(record, document_id=document_id)
