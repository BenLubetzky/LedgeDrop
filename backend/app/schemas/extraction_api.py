"""Request and public response schemas for the Stage 3 extraction API
(step 4).

The endpoints that use these models are added in step 7; the schemas are fixed
here so the API contract can be reviewed and documented before implementation.

Two rules shape the public model:

* The raw provider payload (``raw_response``) is internal audit data and is
  never part of a response.
* ``failure_code`` / ``failure_message`` are the only failure information
  exposed; they are written to be client-safe (no paths, secrets, or stack
  traces) by the processing layer.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, field_serializer

from app.models.extraction import ExtractionStatus
from app.schemas.extraction import InvoiceExtraction
from app.schemas.extraction_persistence import invoice_extraction_from_attempt


class ExtractionStartRequest(BaseModel):
    """Body for starting an extraction or retrying a failed one.

    There are no parameters yet - the provider is chosen by server
    configuration, not by the caller - but unknown keys are rejected so a future
    option can never be silently ignored. An empty body is valid.
    """

    model_config = ConfigDict(extra="forbid")


class InvoiceExtractionResult(BaseModel):
    """Client-facing view of a single extraction attempt.

    ``data`` carries every extracted field as a ``{value, confidence}`` pair,
    plus ``line_items`` - the full internal contract shape. Confidence bounds,
    decimal serialization (JSON strings, no floating-point artifacts), and
    currency shape are all inherited from :class:`InvoiceExtraction`.

    The raw provider response and any other internal diagnostics are
    deliberately absent.
    """

    model_config = ConfigDict(from_attributes=True)

    extraction_id: uuid.UUID
    document_id: uuid.UUID
    attempt_number: int
    status: ExtractionStatus

    provider_name: str 
    provider_model: str | None

    started_at: datetime
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime

    failure_code: str | None
    failure_message: str | None

    data: InvoiceExtraction

    @field_serializer("started_at", "completed_at", "created_at", "updated_at")
    def _serialize_utc(self, value: datetime | None) -> str | None:
        if value is None:
            return None
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    @classmethod
    def from_attempt(cls, attempt: object) -> "InvoiceExtractionResult":
        """Build the public result from a persisted ``ExtractionAttempt``.

        The flat extracted columns are rebuilt into the nested contract (and
        re-validated in the process); the raw provider payload on the attempt is
        never read.
        """
        return cls.model_validate(
            {
                "extraction_id": attempt.extraction_id,
                "document_id": attempt.document_id,
                "attempt_number": attempt.attempt_number,
                "status": attempt.status,
                "provider_name": attempt.provider_name,
                "provider_model": attempt.provider_model,
                "started_at": attempt.started_at,
                "completed_at": attempt.completed_at,
                "created_at": attempt.created_at,
                "updated_at": attempt.updated_at,
                "failure_code": attempt.failure_code,
                "failure_message": attempt.failure_message,
                "data": invoice_extraction_from_attempt(attempt),
            }
        )


__all__ = ["ExtractionStartRequest", "InvoiceExtractionResult"]
