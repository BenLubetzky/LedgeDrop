"""Request and public response schemas for the Stage 5 validation API (step 12).

Mirrors :mod:`app.schemas.normalization_api` one stage down: the endpoints hang
off a completed Stage 4 normalization attempt instead of a completed Stage 3
extraction attempt, and the ``data`` payload is the Stage 5 contract
(:class:`~app.schemas.validation.InvoiceValidation` - ``findings`` +
``summary``) instead of a normalized invoice.

Rules that shape the public model (see ``docs/stage-5-validation.md``):

* The response exposes the validation findings, the re-derived summary, the
  source normalization reference, status, and timestamps - and nothing else.
  There is no acceptance/rejection/escalation vocabulary anywhere: Stage 5
  reports facts, not a decision.
* ``failure_code`` / ``failure_message`` are the only technical-failure
  information exposed; they are written to be client-safe (no paths, secrets,
  or stack traces) by the processing layer. A rule violation is not a technical
  failure - it travels inside ``data.findings`` on a ``COMPLETED`` attempt.
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

from app.models.validation import ValidationStatus
from app.schemas.validation import InvoiceValidation
from app.schemas.validation_persistence import invoice_validation_from_rows


class ValidationStartRequest(BaseModel):
    """Body for starting a validation attempt or retrying a failed one.

    There are no parameters - validation is fully deterministic and takes no
    caller options - but unknown keys are rejected so a future option can never
    be silently ignored. An empty body is valid. The same model serves start
    and retry.
    """

    model_config = ConfigDict(extra="forbid")


class InvoiceValidationResult(BaseModel):
    """Client-facing view of a single validation attempt.

    ``data`` is the full internal contract shape: every finding (a stable
    ``field_path`` or ``null``, a closed ``rule``, a ``severity``, client-safe
    ``expected`` / ``actual`` display values, a fixed ``message``, and
    ``context``) plus the re-derived ``summary``. Decimal fields serialise as
    JSON strings, never floating point.

    ``normalization_id`` ties the result back to the Stage 4 attempt it was
    derived from. There is no acceptance/rejection field, no confidence, no raw
    provider payload, and no other internal diagnostics.
    """

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    validation_id: uuid.UUID
    normalization_id: uuid.UUID
    attempt_number: int = Field(ge=1)
    status: ValidationStatus

    started_at: AwareDatetime
    completed_at: AwareDatetime | None
    created_at: AwareDatetime
    updated_at: AwareDatetime

    failure_code: str | None
    failure_message: str | None

    data: InvoiceValidation

    @model_validator(mode="after")
    def _check_status_fields(self) -> "InvoiceValidationResult":
        if self.status is ValidationStatus.PROCESSING:
            valid = (
                self.completed_at is None
                and self.failure_code is None
                and self.failure_message is None
            )
        elif self.status is ValidationStatus.COMPLETED:
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
            raise ValueError("validation status and completion/failure fields disagree")
        return self

    @field_serializer("started_at", "completed_at", "created_at", "updated_at")
    def _serialize_utc(self, value: datetime | None) -> str | None:
        if value is None:
            return None
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    @classmethod
    def from_attempt(cls, attempt: object) -> "InvoiceValidationResult":
        """Build the public result from a persisted ``ValidationAttempt``.

        The position-ordered finding rows are rebuilt into the nested contract
        (and re-validated in the process). Nothing from the source
        normalization other than its id is read.
        """
        return cls.model_validate(
            {
                "validation_id": attempt.validation_id,
                "normalization_id": attempt.normalization_id,
                "attempt_number": attempt.attempt_number,
                "status": attempt.status,
                "started_at": attempt.started_at,
                "completed_at": attempt.completed_at,
                "created_at": attempt.created_at,
                "updated_at": attempt.updated_at,
                "failure_code": attempt.failure_code,
                "failure_message": attempt.failure_message,
                "data": invoice_validation_from_rows(attempt.findings),
            }
        )


__all__ = ["ValidationStartRequest", "InvoiceValidationResult"]
