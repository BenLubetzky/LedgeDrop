"""Request and public response schemas for the composed processing pipeline
(Stage 4 step 13; Stage 5 step 13; Stage 6 package 5).

The pipeline endpoint runs extraction, normalization, validation, and the
business decision in one request. Its response is just the four per-stage
public results side by side - it adds no fields of its own, so each stage's
own contract stays the single source of truth for its shape.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.decision_api import InvoiceDecisionResult
from app.schemas.extraction_api import InvoiceExtractionResult
from app.schemas.normalization_api import InvoiceNormalizationResult
from app.schemas.validation_api import InvoiceValidationResult


class PipelineRunRequest(BaseModel):
    """Body for running (or retrying) the pipeline.

    ``manual_review_requested`` (default ``false``) is forwarded to the
    decision stage (spec §2.4): it adds one ``manual_review_requested`` reason
    to the decision attempt and is ignored if the chain stops before a
    decision runs. It is strict - ``0``, ``1``, ``"false"`` and ``null`` are
    rejected, matching the decision endpoint. Unknown keys are rejected so a
    future option can never be silently ignored. An empty body is valid.
    """

    model_config = ConfigDict(extra="forbid")

    manual_review_requested: bool = Field(default=False, strict=True)


class PipelineRunResult(BaseModel):
    """All four stage results from one composed run.

    ``normalization`` is ``null`` when the extraction did not complete (there
    was nothing to normalize); retry the pipeline. When present it may itself be
    ``FAILED`` (a normalization technical failure that left the completed
    extraction intact) or ``COMPLETED`` with field-level errors inside
    ``normalization.data.errors``.

    ``validation`` is ``null`` when ``normalization`` is absent or did not
    complete (there was nothing to validate). When present it may itself be
    ``FAILED`` (a validation technical failure that left the completed
    normalization intact) or ``COMPLETED`` with findings inside
    ``validation.data.findings`` - a finding is not a failure.

    ``decision`` is ``null`` when ``validation`` is absent or did not complete
    (there was nothing to decide - spec §2.5). When present it may itself be
    ``FAILED`` (a decision technical failure that left the completed validation
    intact) or ``COMPLETED``; a ``COMPLETED`` decision whose ``outcome`` is
    ``NEEDS_REVIEW`` is not a failure - it is the point at which the document
    moved to ``NEEDS_REVIEW``.
    """

    model_config = ConfigDict(extra="forbid")

    extraction: InvoiceExtractionResult
    normalization: InvoiceNormalizationResult | None
    validation: InvoiceValidationResult | None
    decision: InvoiceDecisionResult | None

    @classmethod
    def from_attempts(
        cls,
        extraction: object,
        normalization: object | None,
        validation: object | None,
        decision: object | None,
    ) -> "PipelineRunResult":
        return cls(
            extraction=InvoiceExtractionResult.from_attempt(extraction),
            normalization=(
                None
                if normalization is None
                else InvoiceNormalizationResult.from_attempt(normalization)
            ),
            validation=(
                None
                if validation is None
                else InvoiceValidationResult.from_attempt(validation)
            ),
            decision=(
                None
                if decision is None
                else InvoiceDecisionResult.from_attempt(decision)
            ),
        )


__all__ = ["PipelineRunRequest", "PipelineRunResult"]
