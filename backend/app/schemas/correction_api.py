"""Client contract for reviewer corrections and deterministic reprocessing."""

from __future__ import annotations

import uuid
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.extraction import ExtractedField, ExtractedLineItem, InvoiceExtraction
from app.schemas.pipeline_api import PipelineRunResult


class CorrectionLineItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str | None = None
    quantity: Decimal | None = None
    unit_price: Decimal | None = None
    line_total: Decimal | None = None

    @field_validator("description", mode="before")
    @classmethod
    def blank_description_is_missing(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator("quantity", "unit_price", "line_total", mode="before")
    @classmethod
    def blank_number_is_missing(cls, value: object) -> object:
        return None if value == "" else value


class InvoiceCorrectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_decision_id: uuid.UUID
    reviewer_name: str = Field(min_length=1, max_length=200)
    note: str | None = Field(default=None, max_length=4000)
    invoice_number: str | None = None
    invoice_date: str | None = None
    due_date: str | None = None
    vendor_name: str | None = None
    vendor_tax_id: str | None = None
    customer_name: str | None = None
    currency: str | None = None
    subtotal: Decimal | None = None
    tax_amount: Decimal | None = None
    total_amount: Decimal | None = None
    line_items: list[CorrectionLineItem] = Field(default_factory=list)

    @field_validator("reviewer_name")
    @classmethod
    def reviewer_is_trimmed(cls, value: str) -> str:
        return value.strip()

    @field_validator(
        "invoice_number", "invoice_date", "due_date", "vendor_name",
        "vendor_tax_id", "customer_name", "currency", "note", mode="before"
    )
    @classmethod
    def blank_text_is_missing(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator("subtotal", "tax_amount", "total_amount", mode="before")
    @classmethod
    def blank_number_is_missing(cls, value: object) -> object:
        return None if value == "" else value

    def as_extraction(self) -> InvoiceExtraction:
        def field(value: object) -> ExtractedField:
            return ExtractedField(value=value, confidence=None)

        return InvoiceExtraction(
            invoice_number=field(self.invoice_number),
            invoice_date=field(self.invoice_date),
            due_date=field(self.due_date),
            vendor_name=field(self.vendor_name),
            vendor_tax_id=field(self.vendor_tax_id),
            customer_name=field(self.customer_name),
            currency=field(self.currency.upper() if self.currency else None),
            subtotal=field(self.subtotal),
            tax_amount=field(self.tax_amount),
            total_amount=field(self.total_amount),
            line_items=[
                ExtractedLineItem(
                    description=field(item.description),
                    quantity=field(item.quantity),
                    unit_price=field(item.unit_price),
                    line_total=field(item.line_total),
                )
                for item in self.line_items
            ],
        )


class InvoiceCorrectionResult(PipelineRunResult):
    approved: bool


__all__ = ["InvoiceCorrectionRequest", "InvoiceCorrectionResult"]
