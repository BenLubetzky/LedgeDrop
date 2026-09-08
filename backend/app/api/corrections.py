"""Persist reviewer edits as a new extraction and re-run deterministic stages."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.core.errors import ConflictError, NotFoundError
from app.models.decision import DecisionOutcome
from app.models.document import Document, DocumentStatus
from app.models.extraction import ExtractionStatus
from app.schemas.correction_api import InvoiceCorrectionRequest, InvoiceCorrectionResult
from app.schemas.pipeline_api import PipelineRunResult
from app.services.processing.decision import DecisionService
from app.services.processing.decision.repository import DecisionRepository
from app.services.processing.extraction.repository import ExtractionRepository
from app.services.processing.normalization import NormalizationService
from app.services.processing.normalization.repository import NormalizationRepository
from app.services.processing.validation import ValidationService
from app.services.processing.validation.repository import ValidationRepository

router = APIRouter(prefix="/documents", tags=["corrections"])


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@router.post(
    "/{document_id}/corrections",
    response_model=InvoiceCorrectionResult,
    status_code=status.HTTP_201_CREATED,
    summary="Correct extracted fields, normalize, validate, and approve if valid",
)
async def submit_correction(
    document_id: uuid.UUID,
    body: InvoiceCorrectionRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> InvoiceCorrectionResult:
    result = await db.execute(
        select(Document).where(Document.document_id == document_id).with_for_update()
    )
    document = result.scalar_one_or_none()
    if document is None:
        raise NotFoundError("No document exists with that ID.", code="DOCUMENT_NOT_FOUND")
    original_status = document.status
    if document.status not in {DocumentStatus.NEEDS_REVIEW, DocumentStatus.COMPLETED}:
        raise ConflictError(
            "This document cannot be corrected in its current state.",
            code="DOCUMENT_NOT_CORRECTABLE",
        )

    extractions = ExtractionRepository(db)
    normalizations = NormalizationRepository(db)
    validations = ValidationRepository(db)
    decisions = DecisionRepository(db)
    current_extraction = await extractions.latest_for_document(document_id)
    current_normalization = (
        await normalizations.latest_for_extraction(current_extraction.extraction_id)
        if current_extraction else None
    )
    current_validation = (
        await validations.latest_for_normalization(current_normalization.normalization_id)
        if current_normalization else None
    )
    current_decision = (
        await decisions.latest_for_validation(current_validation.validation_id)
        if current_validation else None
    )
    decision_matches = (
        body.source_decision_id is not None
        and current_decision is not None
        and current_decision.decision_id == body.source_decision_id
    )
    extraction_matches = (
        body.source_extraction_id is not None
        and current_extraction is not None
        and current_extraction.extraction_id == body.source_extraction_id
    )
    if not decision_matches and not extraction_matches:
        raise ConflictError(
            "This review has been superseded; reload the latest invoice data.",
            code="STALE_DECISION_SOURCE",
        )

    attempt = extractions.add_attempt(
        document_id=document_id,
        attempt_number=await extractions.next_attempt_number(document_id),
        provider_name="human-correction",
        provider_model=None,
    )
    extractions.apply_result(
        attempt,
        body.as_extraction(),
        raw_response={
            "source": "human-correction",
            "source_decision_id": (
                str(body.source_decision_id) if body.source_decision_id else None
            ),
            "source_extraction_id": (
                str(body.source_extraction_id) if body.source_extraction_id else None
            ),
            "reviewer_name": body.reviewer_name,
            "note": body.note,
        },
    )
    await db.flush()
    attempt.status = ExtractionStatus.COMPLETED
    attempt.completed_at = _utcnow()
    await db.commit()

    normalization = await NormalizationService(db).start(attempt.extraction_id)
    validation = await ValidationService(db).start(normalization.normalization_id)
    decision = await DecisionService(db).start(validation.validation_id)
    approved = decision.outcome is DecisionOutcome.ACCEPTED
    if approved:
        document = await db.get(Document, document_id, with_for_update=True)
        assert document is not None
        document.status = (
            DocumentStatus.APPROVED
            if original_status is DocumentStatus.NEEDS_REVIEW
            else DocumentStatus.COMPLETED
        )
        await db.commit()

    refreshed_extraction = await extractions.get(attempt.extraction_id) or attempt
    refreshed_normalization = await normalizations.get(normalization.normalization_id)
    refreshed_validation = await validations.get(validation.validation_id)
    refreshed_decision = await decisions.get(decision.decision_id)
    pipeline = PipelineRunResult.from_attempts(
        refreshed_extraction, refreshed_normalization, refreshed_validation, refreshed_decision
    )
    return InvoiceCorrectionResult(**pipeline.model_dump(), approved=approved)
