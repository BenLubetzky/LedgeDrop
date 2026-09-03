"""Extraction persistence models (Stage 3, step 2).

Two tables:

* ``invoice_extractions`` - one row per **extraction attempt** for a document.
  A document may have many attempts (initial run plus explicit retries), so the
  natural key is ``(document_id, attempt_number)``. Each row holds the flat
  extracted invoice fields, one ``*_value`` / ``*_confidence`` pair per field.
* ``invoice_line_items`` - the line items belonging to one attempt, ordered by
  ``position``.

These records are **unnormalized, unvalidated extraction output** - not
canonical invoices. Every ``*_value`` and every ``*_confidence`` column is
nullable; date and currency text is stored exactly as extracted. Interpretation
happens in the later normalization stage.

The ORM class is ``ExtractionAttempt`` to avoid confusion with the Pydantic
contract ``app.schemas.extraction.InvoiceExtraction``; the table is still
``invoice_extractions``.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base

if TYPE_CHECKING:
    from app.models.document import Document
    from app.models.normalization import NormalizationAttempt


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# Reusable column types. A single SQLAlchemy type object is safe to share.
_AMOUNT = Numeric()  # unbounded NUMERIC - no precision loss on odd extracted values
_QUANTITY = Numeric()
_CONFIDENCE = Numeric(6, 5)  # e.g. 0.97321; bounded to [0, 1] by CHECK constraints


class ExtractionStatus(str, enum.Enum):
    """Lifecycle of a single extraction attempt.

    ``PROCESSING`` is the only "active" state; the partial unique index on
    ``invoice_extractions`` allows at most one PROCESSING attempt per document.
    """

    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


# Scalar invoice fields, in contract order. Each becomes a ``<name>_value`` and a
# ``<name>_confidence`` column on invoice_extractions.
_SCALAR_FIELDS: tuple[str, ...] = (
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
)

_LINE_ITEM_FIELDS: tuple[str, ...] = (
    "description",
    "quantity",
    "unit_price",
    "line_total",
)


def _confidence_range_checks(fields: tuple[str, ...]) -> list[CheckConstraint]:
    """One ``0 <= x <= 1`` CHECK per confidence column (NULL allowed)."""
    return [
        CheckConstraint(
            f"{name}_confidence IS NULL OR "
            f"({name}_confidence >= 0 AND {name}_confidence <= 1)",
            name=f"{name}_confidence_range",  # naming convention prepends ck_<table>_
        )
        for name in fields
    ]


class ExtractionAttempt(Base):
    __tablename__ = "invoice_extractions"

    __table_args__ = (
        UniqueConstraint(
            "document_id",
            "attempt_number",
            name="uq_invoice_extractions_document_id_attempt_number",
        ),
        # At most one active (PROCESSING) attempt per document.
        Index(
            "uq_invoice_extractions_one_active_per_document",
            "document_id",
            unique=True,
            postgresql_where=text("status = 'PROCESSING'"),
        ),
        CheckConstraint("attempt_number >= 1", name="attempt_number_positive"),
        CheckConstraint(
            "(status = 'PROCESSING' AND completed_at IS NULL "
            "AND failure_code IS NULL AND failure_message IS NULL) OR "
            "(status = 'COMPLETED' AND completed_at IS NOT NULL "
            "AND failure_code IS NULL AND failure_message IS NULL) OR "
            "(status = 'FAILED' AND completed_at IS NOT NULL "
            "AND failure_code IS NOT NULL)",
            name="status_fields_consistent",
        ),
        *_confidence_range_checks(_SCALAR_FIELDS),
    )

    extraction_id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, default=uuid.uuid4
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.document_id", ondelete="CASCADE"),
        nullable=False,
    )

    # 1-based; retry N produces attempt_number N. Unique per document.
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)

    status: Mapped[ExtractionStatus] = mapped_column(
        Enum(
            ExtractionStatus,
            name="extraction_status",
            native_enum=True,
            validate_strings=True,
        ),
        nullable=False,
    )

    # --- provider metadata ---------------------------------------------------
    provider_name: Mapped[str] = mapped_column(String(128), nullable=False)
    provider_model: Mapped[str | None] = mapped_column(String(256), nullable=True)
    # Raw provider payload for debugging/audit. Internal only - never returned by
    # a public endpoint.
    raw_response: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # --- timing ------------------------------------------------------------ --
    started_at: Mapped[datetime] = mapped_column(nullable=False, default=_utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(nullable=True)

    # --- failure (client-safe; set only when status is FAILED) --------------
    failure_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    failure_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        nullable=False, default=_utcnow, onupdate=_utcnow
    )

    # --- extracted scalar fields (value + own confidence, all nullable) -----
    invoice_number_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    invoice_number_confidence: Mapped[Decimal | None] = mapped_column(_CONFIDENCE, nullable=True)

    # Raw date text exactly as extracted; not parsed or validated here.
    invoice_date_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    invoice_date_confidence: Mapped[Decimal | None] = mapped_column(_CONFIDENCE, nullable=True)

    due_date_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    due_date_confidence: Mapped[Decimal | None] = mapped_column(_CONFIDENCE, nullable=True)

    vendor_name_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    vendor_name_confidence: Mapped[Decimal | None] = mapped_column(_CONFIDENCE, nullable=True)

    vendor_tax_id_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    vendor_tax_id_confidence: Mapped[Decimal | None] = mapped_column(_CONFIDENCE, nullable=True)

    customer_name_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    customer_name_confidence: Mapped[Decimal | None] = mapped_column(_CONFIDENCE, nullable=True)

    # 3-letter alphabetic code as extracted (upper-cased by the contract).
    currency_value: Mapped[str | None] = mapped_column(String(3), nullable=True)
    currency_confidence: Mapped[Decimal | None] = mapped_column(_CONFIDENCE, nullable=True)

    subtotal_value: Mapped[Decimal | None] = mapped_column(_AMOUNT, nullable=True)
    subtotal_confidence: Mapped[Decimal | None] = mapped_column(_CONFIDENCE, nullable=True)

    tax_amount_value: Mapped[Decimal | None] = mapped_column(_AMOUNT, nullable=True)
    tax_amount_confidence: Mapped[Decimal | None] = mapped_column(_CONFIDENCE, nullable=True)

    total_amount_value: Mapped[Decimal | None] = mapped_column(_AMOUNT, nullable=True)
    total_amount_confidence: Mapped[Decimal | None] = mapped_column(_CONFIDENCE, nullable=True)

    # --- relationships ---------------------------------------------------- --
    document: Mapped[Document] = relationship(back_populates="extractions")
    line_items: Mapped[list[ExtractionLineItem]] = relationship(
        back_populates="extraction",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="ExtractionLineItem.position",
    )
    # Stage 4 normalization attempts derived from this extraction, oldest first.
    # Added in Stage 4; does not affect any Stage 3 behaviour.
    normalizations: Mapped[list[NormalizationAttempt]] = relationship(
        back_populates="source_extraction",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="NormalizationAttempt.attempt_number",
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<ExtractionAttempt {self.extraction_id} doc={self.document_id} "
            f"#{self.attempt_number} {self.status}>"
        )


class ExtractionLineItem(Base):
    __tablename__ = "invoice_line_items"

    __table_args__ = (
        UniqueConstraint(
            "extraction_id",
            "position",
            name="uq_invoice_line_items_extraction_id_position",
        ),
        CheckConstraint("position >= 0", name="position_non_negative"),
        *_confidence_range_checks(_LINE_ITEM_FIELDS),
    )

    line_item_id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    extraction_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("invoice_extractions.extraction_id", ondelete="CASCADE"),
        nullable=False,
    )

    # 0-based order of the line item within its extraction attempt.
    position: Mapped[int] = mapped_column(Integer, nullable=False)

    description_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    description_confidence: Mapped[Decimal | None] = mapped_column(_CONFIDENCE, nullable=True)

    quantity_value: Mapped[Decimal | None] = mapped_column(_QUANTITY, nullable=True)
    quantity_confidence: Mapped[Decimal | None] = mapped_column(_CONFIDENCE, nullable=True)

    unit_price_value: Mapped[Decimal | None] = mapped_column(_AMOUNT, nullable=True)
    unit_price_confidence: Mapped[Decimal | None] = mapped_column(_CONFIDENCE, nullable=True)

    line_total_value: Mapped[Decimal | None] = mapped_column(_AMOUNT, nullable=True)
    line_total_confidence: Mapped[Decimal | None] = mapped_column(_CONFIDENCE, nullable=True)

    created_at: Mapped[datetime] = mapped_column(nullable=False, default=_utcnow)

    extraction: Mapped[ExtractionAttempt] = relationship(back_populates="line_items")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<ExtractionLineItem {self.line_item_id} "
            f"extraction={self.extraction_id} pos={self.position}>"
        )
