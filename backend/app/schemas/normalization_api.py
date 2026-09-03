"""Request and public response schemas for the Stage 4 normalization API
(step 5).

The endpoints that use these models are added in step 12; the schemas are
fixed here so the API contract can be reviewed and documented before
implementation.

Rules that shape the public model (see ``docs/stage-4-normalization.md``):

* The response exposes normalized values, structured field errors, the source
  extraction reference, status, and timestamps - and nothing else. There is no
  confidence (it stays on the Stage 3 extraction record) and no internal
  diagnostics.
* ``failure_code`` / ``failure_message`` are the only technical-failure
  information exposed; they are written to be client-safe (no paths, secrets,
  or stack traces) by the processing layer. A field-level normalization error
  is *not* a technical failure - it travels inside ``data.errors``.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    model_validator,
)

from app.models.normalization import NormalizationStatus
from app.schemas.normalization import NormalizedInvoice
from app.schemas.normalization_persistence import normalized_invoice_from_attempt


class NormalizationStartRequest(BaseModel):
    """Body for starting a normalization attempt or retrying a failed one.

    There are no parameters - normalization is fully deterministic and takes no
    caller options - but unknown keys are rejected so a future option can never
    be silently ignored. An empty body is valid. The same model serves start
    and retry.
    """

    model_config = ConfigDict(extra="forbid")


class InvoiceNormalizationResult(BaseModel):
    """Client-facing view of a single normalization attempt.

    ``data`` is the full internal contract shape: every canonical scalar field,
    ``line_items``, and ``errors`` (each a stable ``field_path`` with a
    client-safe ``code`` / ``message``). The ``YYYY-MM-DD`` date shape, the
    3-letter currency shape, and decimal serialization (JSON strings, no
    floating-point artifacts) are inherited from :class:`NormalizedInvoice`.

    ``extraction_id`` ties the result back to the Stage 3 attempt it was
    derived from. Confidence, the raw provider payload, and any other internal
    diagnostics are deliberately absent.
    """

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    normalization_id: uuid.UUID
    extraction_id: uuid.UUID
    attempt_number: int = Field(ge=1)
    status: NormalizationStatus

    started_at: AwareDatetime
    completed_at: AwareDatetime | None
    created_at: AwareDatetime
    updated_at: AwareDatetime

    failure_code: str | None
    failure_message: str | None

    data: NormalizedInvoice

    @model_validator(mode="after")
    def _check_status_fields(self) -> "InvoiceNormalizationResult":
        if self.status is NormalizationStatus.PROCESSING:
            valid = (
                self.completed_at is None
                and self.failure_code is None
                and self.failure_message is None
            )
        elif self.status is NormalizationStatus.COMPLETED:
            valid = (
                self.completed_at is not None
                and self.failure_code is None
                and self.failure_message is None
            )
        else:
            valid = (
                self.completed_at is not None
                and self.failure_code is not None
                and self.failure_message is not None
            )
        if not valid:
            raise ValueError("normalization status and completion/failure fields disagree")
        return self

    @field_serializer("started_at", "completed_at", "created_at", "updated_at")
    def _serialize_utc(self, value: datetime | None) -> str | None:
        if value is None:
            return None
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    @classmethod
    def from_attempt(cls, attempt: object) -> "InvoiceNormalizationResult":
        """Build the public result from a persisted ``NormalizationAttempt``.

        The flat normalized columns, line items and field errors are rebuilt
        into the nested contract (and re-validated in the process). Nothing
        from the source extraction other than its id is read.
        """
        return cls.model_validate(
            {
                "normalization_id": attempt.normalization_id,
                "extraction_id": attempt.extraction_id,
                "attempt_number": attempt.attempt_number,
                "status": attempt.status,
                "started_at": attempt.started_at,
                "completed_at": attempt.completed_at,
                "created_at": attempt.created_at,
                "updated_at": attempt.updated_at,
                "failure_code": attempt.failure_code,
                "failure_message": attempt.failure_message,
                "data": normalized_invoice_from_attempt(attempt),
            }
        )


__all__ = ["NormalizationStartRequest", "InvoiceNormalizationResult"]
