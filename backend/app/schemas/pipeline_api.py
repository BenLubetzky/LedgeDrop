"""Request and public response schemas for the composed processing pipeline
(Stage 4, step 13).

The pipeline endpoint runs extraction and then normalization in one request.
Its response is just the two per-stage public results side by side - it adds no
fields of its own, so the extraction and normalization contracts stay the
single source of truth for their own shapes.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from app.schemas.extraction_api import InvoiceExtractionResult
from app.schemas.normalization_api import InvoiceNormalizationResult


class PipelineRunRequest(BaseModel):
    """Body for running (or retrying) the pipeline.

    There are no parameters - each stage is configured server-side - but
    unknown keys are rejected so a future option can never be silently ignored.
    An empty body is valid.
    """

    model_config = ConfigDict(extra="forbid")


class PipelineRunResult(BaseModel):
    """Both stage results from one composed run.

    ``normalization`` is ``null`` when the extraction did not complete (there
    was nothing to normalize); retry the pipeline. When present it may itself be
    ``FAILED`` (a normalization technical failure that left the completed
    extraction intact) or ``COMPLETED`` with field-level errors inside
    ``normalization.data.errors``.
    """

    model_config = ConfigDict(extra="forbid")

    extraction: InvoiceExtractionResult
    normalization: InvoiceNormalizationResult | None

    @classmethod
    def from_attempts(
        cls, extraction: object, normalization: object | None
    ) -> "PipelineRunResult":
        return cls(
            extraction=InvoiceExtractionResult.from_attempt(extraction),
            normalization=(
                None
                if normalization is None
                else InvoiceNormalizationResult.from_attempt(normalization)
            ),
        )


__all__ = ["PipelineRunRequest", "PipelineRunResult"]
