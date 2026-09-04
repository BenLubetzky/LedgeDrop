"""Normalization persistence models (Stage 4, step 3).

Three tables:

* ``invoice_normalizations`` - one row per **normalization attempt** for a
  source extraction attempt. A source extraction may have many normalization
  attempts (the initial run plus explicit retries of a *technical* failure), so
  the natural key is ``(extraction_id, attempt_number)``. Each row holds the
  flat *canonical* invoice fields - one column per field, and **no confidence**
  (confidence stays on the Stage 3 record).
* ``invoice_normalized_line_items`` - the normalized line items for one attempt,
  ordered by ``position``; the order mirrors the source extraction's line-item
  order 1:1.
* ``invoice_normalization_errors`` - field-level normalization errors for one
  attempt: a stable ``field_path``, the offending ``raw_value``, a safe
  ``code``, and a safe ``message``. A field-level error is *data about the
  invoice*, not a technical failure - an attempt that only has field errors
  still ends ``COMPLETED``.

These rows never modify the Stage 3 extraction they derive from. The ORM class
is ``NormalizationAttempt`` to avoid confusion with the Pydantic contract
``app.schemas.normalization.NormalizedInvoice``; the table is
``invoice_normalizations``.

The full policy (text length caps, the closed error-code set, the day-first
date default, the accepted ISO 4217 list) is in
``docs/stage-4-normalization.md``. The column types here enforce only the
structural parts - length caps, the ``YYYY-MM-DD`` shape, upper-case currency;
value-level rules live in the step 6-10 normalizers.
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
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base
from app.schemas.normalization import NormalizationErrorCode

if TYPE_CHECKING:
    from app.models.extraction import ExtractionAttempt
    from app.models.validation import ValidationAttempt


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# Reusable column types. A single SQLAlchemy type object is safe to share.
_AMOUNT = Numeric()  # unbounded NUMERIC - the scale from extraction is preserved
_QUANTITY = Numeric()

# Normalized text length caps (Unicode characters), per
# docs/stage-4-normalization.md. The step 10 normalizer errors (never
# truncates) before a value this long can reach these columns; the bounded
# types are a matching backstop.
_INVOICE_NUMBER_MAX = 100
_TAX_ID_MAX = 60
_PARTY_NAME_MAX = 256
_DESCRIPTION_MAX = 512


class NormalizationStatus(str, enum.Enum):
    """Lifecycle of a single normalization attempt.

    ``PROCESSING`` is the only "active" state; a partial unique index on
    ``invoice_normalizations`` allows at most one PROCESSING attempt per source
    extraction. ``COMPLETED`` means normalization finished - the attempt may
    still carry field-level errors. ``FAILED`` is a technical failure only.
    """

    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class NormalizationAttempt(Base):
    __tablename__ = "invoice_normalizations"

    __table_args__ = (
        UniqueConstraint(
            "extraction_id",
            "attempt_number",
            name="uq_invoice_normalizations_extraction_id_attempt_number",
        ),
        # At most one active (PROCESSING) attempt per source extraction.
        Index(
            "uq_invoice_normalizations_one_active_per_extraction",
            "extraction_id",
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
            "AND failure_code IS NOT NULL AND failure_message IS NOT NULL)",
            name="status_fields_consistent",
        ),
        CheckConstraint(
            "invoice_date IS NULL OR invoice_date ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'",
            name="invoice_date_iso_shape",
        ),
        CheckConstraint(
            "due_date IS NULL OR due_date ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'",
            name="due_date_iso_shape",
        ),
        CheckConstraint(
            "currency IS NULL OR currency ~ '^[A-Z]{3}$'",
            name="currency_alpha3_upper",
        ),
    )

    normalization_id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, default=uuid.uuid4
    )
    # Source Stage 3 attempt. CASCADE so deleting a document (which cascades to
    # its extractions) also clears the derived normalizations; normalization
    # code itself never writes back to this row.
    extraction_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("invoice_extractions.extraction_id", ondelete="CASCADE"),
        nullable=False,
    )

    # 1-based; retry N produces attempt_number N. Unique per source extraction.
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)

    status: Mapped[NormalizationStatus] = mapped_column(
        Enum(
            NormalizationStatus,
            name="normalization_status",
            native_enum=True,
            validate_strings=True,
        ),
        nullable=False,
    )

    # --- timing ----------------------------------------------------------- ---
    started_at: Mapped[datetime] = mapped_column(nullable=False, default=_utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(nullable=True)

    # --- technical failure (client-safe; set only when status is FAILED) -----
    failure_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    failure_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        nullable=False, default=_utcnow, onupdate=_utcnow
    )

    # --- normalized scalar fields (canonical value or NULL; no confidence) ---
    invoice_number: Mapped[str | None] = mapped_column(
        String(_INVOICE_NUMBER_MAX), nullable=True
    )
    # Canonical YYYY-MM-DD string; the shape is enforced by a CHECK above and
    # the calendar validity by the step 7 normalizer.
    invoice_date: Mapped[str | None] = mapped_column(String(10), nullable=True)
    due_date: Mapped[str | None] = mapped_column(String(10), nullable=True)
    vendor_name: Mapped[str | None] = mapped_column(String(_PARTY_NAME_MAX), nullable=True)
    vendor_tax_id: Mapped[str | None] = mapped_column(String(_TAX_ID_MAX), nullable=True)
    customer_name: Mapped[str | None] = mapped_column(String(_PARTY_NAME_MAX), nullable=True)
    # 3-letter alphabetic ISO 4217 code, upper-cased (CHECK above). List
    # membership is enforced by the step 8 normalizer.
    currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    subtotal: Mapped[Decimal | None] = mapped_column(_AMOUNT, nullable=True)
    tax_amount: Mapped[Decimal | None] = mapped_column(_AMOUNT, nullable=True)
    total_amount: Mapped[Decimal | None] = mapped_column(_AMOUNT, nullable=True)

    # --- relationships -------------------------------------------------- -----
    source_extraction: Mapped[ExtractionAttempt] = relationship(
        back_populates="normalizations"
    )
    line_items: Mapped[list[NormalizationLineItem]] = relationship(
        back_populates="normalization",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="NormalizationLineItem.position",
    )
    errors: Mapped[list[NormalizationFieldError]] = relationship(
        back_populates="normalization",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="NormalizationFieldError.field_path",
    )
    # Stage 5 validation attempts over this normalization attempt, oldest first.
    # Added in Stage 5; does not affect any Stage 4 behaviour.
    validations: Mapped[list[ValidationAttempt]] = relationship(
        back_populates="source_normalization",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="ValidationAttempt.attempt_number",
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<NormalizationAttempt {self.normalization_id} "
            f"extraction={self.extraction_id} #{self.attempt_number} {self.status}>"
        )


class NormalizationLineItem(Base):
    __tablename__ = "invoice_normalized_line_items"

    __table_args__ = (
        UniqueConstraint(
            "normalization_id",
            "position",
            name="uq_invoice_normalized_line_items_normalization_id_position",
        ),
        CheckConstraint("position >= 0", name="position_non_negative"),
    )

    normalized_line_item_id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, default=uuid.uuid4
    )
    normalization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("invoice_normalizations.normalization_id", ondelete="CASCADE"),
        nullable=False,
    )

    # 0-based order within the attempt; matches the source line item's position.
    position: Mapped[int] = mapped_column(Integer, nullable=False)

    description: Mapped[str | None] = mapped_column(String(_DESCRIPTION_MAX), nullable=True)
    quantity: Mapped[Decimal | None] = mapped_column(_QUANTITY, nullable=True)
    unit_price: Mapped[Decimal | None] = mapped_column(_AMOUNT, nullable=True)
    line_total: Mapped[Decimal | None] = mapped_column(_AMOUNT, nullable=True)

    created_at: Mapped[datetime] = mapped_column(nullable=False, default=_utcnow)

    normalization: Mapped[NormalizationAttempt] = relationship(back_populates="line_items")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<NormalizationLineItem {self.normalized_line_item_id} "
            f"normalization={self.normalization_id} pos={self.position}>"
        )


class NormalizationFieldError(Base):
    __tablename__ = "invoice_normalization_errors"

    __table_args__ = (
        # One normalizer runs per field, so at most one error per field path.
        UniqueConstraint(
            "normalization_id",
            "field_path",
            name="uq_invoice_normalization_errors_normalization_id_field_path",
        ),
        CheckConstraint(
            "field_path IN ('invoice_number', 'invoice_date', 'due_date', "
            "'vendor_name', 'vendor_tax_id', 'customer_name', 'currency', "
            "'subtotal', 'tax_amount', 'total_amount') OR "
            r"field_path ~ '^line_items\.(0|[1-9][0-9]*)\."
            r"(description|quantity|unit_price|line_total)$'",
            name="field_path_shape",
        ),
    )

    normalization_error_id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, default=uuid.uuid4
    )
    normalization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("invoice_normalizations.normalization_id", ondelete="CASCADE"),
        nullable=False,
    )

    # Stable path to the field this error is about: a scalar name
    # ("total_amount") or "line_items.<index>.<field>".
    field_path: Mapped[str] = mapped_column(String(64), nullable=False)
    # The offending source value, stringified. NULL only when the source value
    # was itself NULL.
    raw_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    code: Mapped[NormalizationErrorCode] = mapped_column(
        Enum(
            NormalizationErrorCode,
            name="normalization_error_code",
            native_enum=True,
            validate_strings=True,
            values_callable=lambda enum_type: [member.value for member in enum_type],
        ),
        nullable=False,
    )
    # Client-safe: no internal paths, secrets, or stack traces.
    message: Mapped[str] = mapped_column(Text, nullable=False)

    created_at: Mapped[datetime] = mapped_column(nullable=False, default=_utcnow)

    normalization: Mapped[NormalizationAttempt] = relationship(back_populates="errors")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<NormalizationFieldError {self.normalization_error_id} "
            f"normalization={self.normalization_id} {self.field_path} {self.code}>"
        )
