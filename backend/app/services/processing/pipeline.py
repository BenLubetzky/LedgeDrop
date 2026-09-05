"""Processing-pipeline composition (Stage 4 step 13; Stage 5 step 13).

Chains the processing stages into one path::

    upload -> extraction -> normalization -> validation

Each stage stays a fully independent subsystem - its own service, lifecycle,
transaction boundary, repository, and API endpoints. This module only
*composes* them so a caller can run the chain in a single request instead of
polling between stages; it adds no processing rules of its own.

Rules:

* Normalization runs only when the extraction ended ``COMPLETED``. A failed
  extraction stops the chain (``normalization`` and ``validation`` are
  ``None``); retry it.
* A normalization *technical* failure does not undo the completed extraction -
  it is recorded as a ``FAILED`` normalization attempt and can be retried on
  the normalization endpoint. A field-level normalization error is not a
  failure at all; the normalization attempt is ``COMPLETED`` and the error is
  carried in its ``errors``.
* Validation runs only when the normalization ended ``COMPLETED``. A failed or
  skipped normalization stops the chain (``validation`` is ``None``); no Stage
  5 rule logic lives here - a rule violation is a finding on a ``COMPLETED``
  validation attempt, not a pipeline decision.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError
from app.models.extraction import ExtractionAttempt, ExtractionStatus
from app.models.normalization import NormalizationAttempt, NormalizationStatus
from app.models.validation import ValidationAttempt
from app.services.processing.extraction import ExtractionService, ResultProducer
from app.services.processing.extraction.repository import ExtractionRepository
from app.services.processing.normalization import NormalizationService
from app.services.processing.normalization.repository import NormalizationRepository
from app.services.processing.validation import ValidationRepository, ValidationService

logger = logging.getLogger("app.pipeline")

Action = Literal["start", "retry"]


@dataclass(frozen=True, slots=True)
class PipelineResult:
    """The outcome of one composed pipeline run.

    ``normalization`` is ``None`` only when the extraction did not complete, so
    there was nothing to normalize. ``validation`` is ``None`` only when
    ``normalization`` is absent or did not complete, so there was nothing to
    validate.
    """

    extraction: ExtractionAttempt
    normalization: NormalizationAttempt | None
    validation: ValidationAttempt | None


class ProcessingPipeline:
    """Runs extraction, then normalization, then validation against one session.

    The individual services are injectable for testing but default to plain
    instances bound to the same session, exactly as a caller would build them
    to run a stage on its own.
    """

    def __init__(
        self,
        session: AsyncSession,
        *,
        extraction: ExtractionService | None = None,
        normalization: NormalizationService | None = None,
        validation: ValidationService | None = None,
    ) -> None:
        self._session = session
        self._extraction = extraction or ExtractionService(session)
        self._normalization = normalization or NormalizationService(session)
        self._validation = validation or ValidationService(session)

    async def run(
        self,
        document_id: uuid.UUID,
        *,
        action: Action = "start",
        produce: ResultProducer,
        provider_name: str,
        provider_model: str | None = None,
    ) -> PipelineResult:
        """Run extraction (``start`` or ``retry``), then continue to
        normalization, then validation.

        Any ``NotFoundError`` / ``ConflictError`` from the extraction stage
        propagates unchanged - the pipeline does not mask a stage's own
        lifecycle errors.
        """
        if action not in ("start", "retry"):
            raise ValueError(f"unknown pipeline action: {action!r}")

        run_stage = (
            self._extraction.retry if action == "retry" else self._extraction.start
        )
        extraction = await run_stage(
            document_id,
            produce=produce,
            provider_name=provider_name,
            provider_model=provider_model,
        )
        extraction_id = extraction.extraction_id

        normalization = await self._continue_to_normalization(extraction)
        normalization_id = (
            None if normalization is None else normalization.normalization_id
        )

        validation = await self._continue_to_validation(normalization)
        validation_id = None if validation is None else validation.validation_id

        # Each later stage owns its own transaction and, on a technical
        # failure, rolls the shared session back - which expires objects
        # loaded earlier in it. Re-load every attempt so the caller always
        # gets live, eager-loaded rows regardless of which path was taken.
        extraction = (
            await ExtractionRepository(self._session).get(extraction_id) or extraction
        )
        if normalization_id is not None:
            normalization = (
                await NormalizationRepository(self._session).get(normalization_id)
                or normalization
            )
        if validation_id is not None:
            validation = (
                await ValidationRepository(self._session).get(validation_id)
                or validation
            )
        return PipelineResult(
            extraction=extraction, normalization=normalization, validation=validation
        )

    async def _continue_to_normalization(
        self, extraction: ExtractionAttempt
    ) -> NormalizationAttempt | None:
        """Normalize a just-completed extraction; skip a failed one."""
        if extraction.status is not ExtractionStatus.COMPLETED:
            return None

        try:
            return await self._normalization.start(extraction.extraction_id)
        except ConflictError as exc:
            # A normalization attempt already exists for this extraction (for
            # example one started directly on the normalization endpoint). The
            # pipeline does not force a second one - it hands back the newest so
            # the caller still gets a coherent result.
            if exc.code not in {
                "NORMALIZATION_IN_PROGRESS",
                "EXTRACTION_ALREADY_NORMALIZED",
                "NORMALIZATION_FAILED",
            }:
                raise
            logger.info(
                "pipeline: normalization already present for extraction %s; "
                "returning the latest attempt",
                extraction.extraction_id,
            )
            return await NormalizationRepository(self._session).latest_for_extraction(
                extraction.extraction_id
            )

    async def _continue_to_validation(
        self, normalization: NormalizationAttempt | None
    ) -> ValidationAttempt | None:
        """Validate a just-completed normalization; skip a missing or failed one."""
        if (
            normalization is None
            or normalization.status is not NormalizationStatus.COMPLETED
        ):
            return None

        try:
            return await self._validation.start(normalization.normalization_id)
        except ConflictError as exc:
            # A validation attempt already exists for this normalization (for
            # example one started directly on the validation endpoint). The
            # pipeline does not force a second one - it hands back the newest so
            # the caller still gets a coherent result.
            if exc.code not in {
                "VALIDATION_IN_PROGRESS",
                "NORMALIZATION_ALREADY_VALIDATED",
                "VALIDATION_FAILED",
            }:
                raise
            logger.info(
                "pipeline: validation already present for normalization %s; "
                "returning the latest attempt",
                normalization.normalization_id,
            )
            return await ValidationRepository(self._session).latest_for_normalization(
                normalization.normalization_id
            )


__all__ = ["Action", "PipelineResult", "ProcessingPipeline"]
