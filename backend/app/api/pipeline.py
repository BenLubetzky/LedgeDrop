"""Composed processing-pipeline routes (Stage 4 step 13; Stage 5 step 13;
Stage 6 package 5).

The pipeline chains the stages that already have their own endpoints:

* ``POST /documents/{id}/pipeline``        run extraction, then normalization,
  then validation, then the business decision
* ``POST /documents/{id}/pipeline/retry``  retry a failed extraction, then
  normalize, validate, and decide the new attempt

The per-stage endpoints (``/extractions[...]``,
``/extractions/{eid}/normalizations[...]``,
``/extractions/{eid}/normalizations/{nid}/validations[...]``, and
``.../validations/{vid}/decisions[...]``) are unchanged and remain the way to
drive or inspect a single stage in isolation. This route only saves a caller
the round trip between them.

Status and error semantics come straight from the stages:

* ``404 DOCUMENT_NOT_FOUND`` for an unknown document.
* ``409`` from the extraction stage (``EXTRACTION_IN_PROGRESS``,
  ``DOCUMENT_ALREADY_EXTRACTED``, ``EXTRACTION_NOT_FAILED``,
  ``DOCUMENT_NOT_EXTRACTABLE``).
* A run that *starts* is ``201`` even if a later stage then fails: the
  extraction, normalization, validation, or decision attempt was created and
  its ``status`` / ``failure_code`` say what happened. ``normalization`` is
  ``null`` when the extraction did not complete; ``validation`` is ``null``
  when the normalization is absent or did not complete; ``decision`` is
  ``null`` when the validation is absent or did not complete.

``manual_review_requested`` in the body is forwarded to the decision stage
(spec §2.4). No decision *policy* runs here - it stays in the decision
subsystem; the pipeline only composes the call.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status

from app.api.deps import get_extractor, get_pipeline, get_prepared_result_producer
from app.schemas.pipeline_api import PipelineRunRequest, PipelineRunResult
from app.services.processing.extraction import ResultProducer
from app.services.processing.extraction.provider import ExtractionProvider
from app.services.processing.pipeline import ProcessingPipeline

router = APIRouter(prefix="/documents", tags=["pipeline"])


async def _run(
    pipeline: ProcessingPipeline,
    document_id: uuid.UUID,
    *,
    action: str,
    produce: ResultProducer,
    provider: ExtractionProvider,
    manual_review_requested: bool = False,
) -> PipelineRunResult:
    result = await pipeline.run(
        document_id,
        action=action,
        produce=produce,
        provider_name=provider.name,
        provider_model=getattr(provider, "model", None),
        manual_review_requested=manual_review_requested,
    )
    return PipelineRunResult.from_attempts(
        result.extraction, result.normalization, result.validation, result.decision
    )


@router.post(
    "/{document_id}/pipeline",
    response_model=PipelineRunResult,
    status_code=status.HTTP_201_CREATED,
    summary="Run extraction, normalization, and validation for a document",
)
async def run_pipeline(
    document_id: uuid.UUID,
    pipeline: Annotated[ProcessingPipeline, Depends(get_pipeline)],
    produce: Annotated[ResultProducer, Depends(get_prepared_result_producer)],
    provider: Annotated[ExtractionProvider, Depends(get_extractor)],
    body: PipelineRunRequest | None = None,
) -> PipelineRunResult:
    return await _run(
        pipeline,
        document_id,
        action="start",
        produce=produce,
        provider=provider,
        manual_review_requested=(
            body.manual_review_requested if body is not None else False
        ),
    )


@router.post(
    "/{document_id}/pipeline/retry",
    response_model=PipelineRunResult,
    status_code=status.HTTP_201_CREATED,
    summary="Retry extraction, then normalize and validate the new attempt",
)
async def retry_pipeline(
    document_id: uuid.UUID,
    pipeline: Annotated[ProcessingPipeline, Depends(get_pipeline)],
    produce: Annotated[ResultProducer, Depends(get_prepared_result_producer)],
    provider: Annotated[ExtractionProvider, Depends(get_extractor)],
    body: PipelineRunRequest | None = None,
) -> PipelineRunResult:
    return await _run(
        pipeline,
        document_id,
        action="retry",
        produce=produce,
        provider=provider,
        manual_review_requested=(
            body.manual_review_requested if body is not None else False
        ),
    )
