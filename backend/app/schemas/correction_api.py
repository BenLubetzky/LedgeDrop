"""Request and public response schemas for the Stage 8 correction API.

The endpoints hang off the document (a correction is about the whole invoice,
not one upstream attempt). Rules that shape the public model
(``docs/stage-8-corrections.md`` Part 6):

* The request *is* the ``SubmittedInvoice`` the merge projection consumes: the
  ten canonical scalars and the full desired line-item list, as free text,
  plus reviewer attribution, the source-decision anchor, and the add-only
  ``manual_review_requested`` flag forwarded to the re-run decision.
* The response exposes the correction attempt's status, the before/after diff
  (``entries``), where the document ended (``resulting_document_status`` /
  ``resulting_outcome``), and the re-run per-stage results (``pipeline``) - and
  nothing internal. ``submitted_payload``, storage paths, hashes, and raw
  provider payloads are never exposed.
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
    field_validator,
)

from app.models.decision import DecisionOutcome
from app.models.document import DocumentStatus
from app.schemas.correction import (
    NOTE_MAX_LENGTH,
    REVIEWER_NAME_MAX_LENGTH,
    CorrectionOperation,
    CorrectionStatus,
)
from app.schemas.normalization import (
    NORMALIZED_LINE_ITEM_FIELD_NAMES,
    NORMALIZED_SCALAR_FIELD_NAMES,
    NormalizationErrorCode,
)
from app.schemas.pipeline_api import PipelineRunResult
from app.services.processing.correction.projection import SubmittedInvoice

_SCALAR_NAMES = tuple(NORMALIZED_SCALAR_FIELD_NAMES)


def _blank_to_none(value: object) -> object:
    return None if isinstance(value, str) and not value.strip() else value


class CorrectionLineItemInput(BaseModel):
    """One submitted line item, all leaves as free text."""

    model_config = ConfigDict(extra="forbid")

    description: str | None = None
    quantity: str | None = None
    unit_price: str | None = None
    line_total: str | None = None

    @field_validator("*", mode="before")
    @classmethod
    def _blank_is_none(cls, value: object) -> object:
        return _blank_to_none(value)


class InvoiceCorrectionRequest(BaseModel):
    """Body for ``POST /documents/{document_id}/corrections``."""

    model_config = ConfigDict(extra="forbid")

    source_decision_id: uuid.UUID
    source_normalization_id: uuid.UUID | None = None
    reviewer_name: str = Field(min_length=1, max_length=REVIEWER_NAME_MAX_LENGTH)
    note: str | None = Field(default=None, max_length=NOTE_MAX_LENGTH)
    manual_review_requested: bool = Field(default=False, strict=True)

    invoice_number: str | None = None
    invoice_date: str | None = None
    due_date: str | None = None
    vendor_name: str | None = None
    vendor_tax_id: str | None = None
    customer_name: str | None = None
    currency: str | None = None
    subtotal: str | None = None
    tax_amount: str | None = None
    total_amount: str | None = None
    line_items: list[CorrectionLineItemInput] = Field(default_factory=list)

    @field_validator("reviewer_name")
    @classmethod
    def _trim_reviewer(cls, value: str) -> str:
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("reviewer_name must not be blank")
        return trimmed

    @field_validator(
        "note",
        "invoice_number",
        "invoice_date",
        "due_date",
        "vendor_name",
        "vendor_tax_id",
        "customer_name",
        "currency",
        "subtotal",
        "tax_amount",
        "total_amount",
        mode="before",
    )
    @classmethod
    def _blank_is_none(cls, value: object) -> object:
        return _blank_to_none(value)

    @field_validator("note")
    @classmethod
    def _note_non_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("note must not be blank when present")
        return value.strip() if value is not None else None

    def to_submitted(self) -> SubmittedInvoice:
        return SubmittedInvoice(
            scalars={name: getattr(self, name) for name in _SCALAR_NAMES},
            line_items=[
                {leaf: getattr(item, leaf) for leaf in NORMALIZED_LINE_ITEM_FIELD_NAMES}
                for item in self.line_items
            ],
        )


class CorrectionFieldEntryView(BaseModel):
    """One before/after diff row in a correction result."""

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    operation: CorrectionOperation
    field_path: str
    previous_value: str | None
    raw_value: str | None
    normalized_value: str | None
    error_code: NormalizationErrorCode | None
    error_message: str | None


class InvoiceCorrectionResult(BaseModel):
    """Client-facing view of one correction attempt."""

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    correction_id: uuid.UUID
    document_id: uuid.UUID
    attempt_number: int = Field(ge=1)
    status: CorrectionStatus

    reviewer_name: str
    note: str | None
    policy_version: str
    origin_document_status: DocumentStatus
    resulting_document_status: DocumentStatus | None
    resulting_outcome: DecisionOutcome | None

    submitted_at: AwareDatetime
    completed_at: AwareDatetime | None

    failure_code: str | None
    failure_message: str | None

    entries: list[CorrectionFieldEntryView]
    pipeline: PipelineRunResult | None

    @field_serializer("submitted_at", "completed_at")
    def _serialize_utc(self, value: datetime | None) -> str | None:
        if value is None:
            return None
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    @classmethod
    def from_attempt(
        cls, attempt: object, *, pipeline: PipelineRunResult | None
    ) -> "InvoiceCorrectionResult":
        return cls.model_validate(
            {
                "correction_id": attempt.correction_id,
                "document_id": attempt.document_id,
                "attempt_number": attempt.attempt_number,
                "status": attempt.status,
                "reviewer_name": attempt.reviewer_name,
                "note": attempt.note,
                "policy_version": attempt.policy_version,
                "origin_document_status": attempt.origin_document_status,
                "resulting_document_status": attempt.resulting_document_status,
                "resulting_outcome": attempt.resulting_outcome,
                "submitted_at": attempt.submitted_at,
                "completed_at": attempt.completed_at,
                "failure_code": attempt.failure_code,
                "failure_message": attempt.failure_message,
                "entries": [
                    CorrectionFieldEntryView.model_validate(row)
                    for row in attempt.fields
                ],
                "pipeline": pipeline,
            }
        )


__all__ = [
    "CorrectionLineItemInput",
    "InvoiceCorrectionRequest",
    "CorrectionFieldEntryView",
    "InvoiceCorrectionResult",
]
