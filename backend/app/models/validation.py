"""Validation persistence models (Stage 5, step 6).

Two tables:

* ``invoice_validations`` - one row per **validation attempt** over a source
  normalization attempt. A normalization attempt may have several validation
  attempts (the initial run plus explicit retries of a *technical* failure), so
  the natural key is ``(normalization_id, attempt_number)``. The partial unique
  index ``uq_invoice_validations_one_active_per_normalization`` allows at most
  one ``PROCESSING`` attempt per source normalization. The row carries lifecycle
  and technical-failure columns only - no invoice data (that stays on the
  normalization attempt) and no verdict (Stage 5 asserts facts, spec Part 1).
* ``invoice_validation_findings`` - the structured findings for one attempt,
  ordered by ``position`` (the engine emits them in rule-catalogue order). Each
  finding mirrors :class:`app.schemas.validation.ValidationFinding`: a closed
  ``rule`` code, a ``severity`` of ``error | warning | info``, an optional Stage
  4 ``field_path`` (``NULL`` for an invoice-level finding), client-safe
  ``expected`` / ``actual`` display strings, a fixed ``message``, and a JSONB
  ``context`` object. A finding is *data about the invoice*, not a technical
  failure - an attempt that produces findings still ends ``COMPLETED``.

These rows never modify the Stage 2-4 records they derive from. The summary
counts in :class:`app.schemas.validation.ValidationSummary` are re-derived from
the finding rows on read, never stored, so they cannot drift.

The ORM classes are ``ValidationAttempt`` and ``ValidationFindingRow`` to avoid
confusion with the Pydantic contract types ``InvoiceValidation`` /
``ValidationFinding``; the tables are ``invoice_validations`` and
``invoice_validation_findings``.

The full boundary, pinned policies, and the closed rule catalogue live in
``docs/stage-5-validation.md``. The column types here enforce only structural
parts (the status/failure invariants, the ``field_path`` shape, the closed
enums); rule evaluation is the step 8-9 engine.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    CheckConstraint,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

from app.database.base import Base
from app.schemas.normalization import (
    NORMALIZED_LINE_ITEM_FIELD_NAMES,
    NORMALIZED_SCALAR_FIELD_NAMES,
)
from app.schemas.validation import FindingSeverity, ValidationRule, ValidationStatus

if TYPE_CHECKING:
    from app.models.decision import DecisionAttempt
    from app.models.normalization import NormalizationAttempt

__all__ = [
    "ValidationStatus",
    "FindingSeverity",
    "ValidationRule",
    "ValidationAttempt",
    "ValidationFindingRow",
]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _json_compatible(value: Any) -> Any:
    """Convert contract-valid context values to lossless JSONB values."""
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("validation context Decimal values must be finite")
        return str(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, float):
        raise ValueError("validation context must not contain binary float values")
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, list):
        return [_json_compatible(item) for item in value]
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise ValueError("validation context object keys must be strings")
        return {key: _json_compatible(item) for key, item in value.items()}
    raise TypeError(f"validation context value is not JSON-compatible: {type(value)!r}")


class _ValidationContextType(TypeDecorator[dict[str, Any]]):
    """JSONB that serializes Decimal and UUID values like the Pydantic contract."""

    impl = JSONB
    cache_ok = True

    def process_bind_param(self, value: dict[str, Any] | None, dialect: Any) -> Any:
        if value is None:
            return None
        return _json_compatible(value)


# The finding ``field_path`` reuses the Stage 4 vocabulary exactly, or is NULL
# for an invoice-level finding. Built from the normalization contract's name
# tuples so the CHECK cannot drift from the field paths Stage 4 can emit.
_SCALAR_IN_LIST = ", ".join(f"'{name}'" for name in NORMALIZED_SCALAR_FIELD_NAMES)
_LINE_ITEM_FIELD_ALT = "|".join(NORMALIZED_LINE_ITEM_FIELD_NAMES)
_FIELD_PATH_CHECK = (
    f"field_path IS NULL OR field_path IN ({_SCALAR_IN_LIST}) OR "
    rf"field_path ~ '^line_items\.(0|[1-9][0-9]*)\.({_LINE_ITEM_FIELD_ALT})$'"
)

# Mirrors ``invoice_normalizations`` (Stage 4): PROCESSING carries no completion
# or failure detail; COMPLETED has a completion time and no failure detail;
# FAILED has both a completion time and a client-safe failure code + message.
_STATUS_FIELDS_CONSISTENT = (
    "(status = 'PROCESSING' AND completed_at IS NULL "
    "AND failure_code IS NULL AND failure_message IS NULL) OR "
    "(status = 'COMPLETED' AND completed_at IS NOT NULL "
    "AND failure_code IS NULL AND failure_message IS NULL) OR "
    "(status = 'FAILED' AND completed_at IS NOT NULL "
    "AND failure_code IS NOT NULL AND failure_message IS NOT NULL)"
)


class ValidationAttempt(Base):
    __tablename__ = "invoice_validations"

    __table_args__ = (
        UniqueConstraint(
            "normalization_id",
            "attempt_number",
            name="uq_invoice_validations_normalization_id_attempt_number",
        ),
        # At most one active (PROCESSING) attempt per source normalization.
        Index(
            "uq_invoice_validations_one_active_per_normalization",
            "normalization_id",
            unique=True,
            postgresql_where=text("status = 'PROCESSING'"),
        ),
        CheckConstraint("attempt_number >= 1", name="attempt_number_positive"),
        CheckConstraint(_STATUS_FIELDS_CONSISTENT, name="status_fields_consistent"),
    )

    validation_id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    # Source Stage 4 attempt. CASCADE so deleting a document (which cascades
    # through its extractions and normalizations) also clears the derived
    # validations; validation code itself never writes back to this row.
    normalization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("invoice_normalizations.normalization_id", ondelete="CASCADE"),
        nullable=False,
    )

    # 1-based; retry N produces attempt_number N. Unique per source normalization.
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)

    status: Mapped[ValidationStatus] = mapped_column(
        Enum(
            ValidationStatus,
            name="validation_status",
            native_enum=True,
            validate_strings=True,
        ),
        nullable=False,
    )

    # --- timing ------------------------------------------------------------ ---
    started_at: Mapped[datetime] = mapped_column(nullable=False, default=_utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(nullable=True)

    # --- technical failure (client-safe; set only when status is FAILED) -----
    failure_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    failure_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        nullable=False, default=_utcnow, onupdate=_utcnow
    )

    # --- relationships --------------------------------------------------- -----
    source_normalization: Mapped[NormalizationAttempt] = relationship(
        back_populates="validations"
    )
    findings: Mapped[list[ValidationFindingRow]] = relationship(
        back_populates="validation",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="ValidationFindingRow.position",
    )
    # Stage 6 decision attempts over this validation attempt, oldest first.
    # Added in Stage 6; does not affect any Stage 5 behaviour.
    decisions: Mapped[list["DecisionAttempt"]] = relationship(
        back_populates="source_validation",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="DecisionAttempt.attempt_number",
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<ValidationAttempt {self.validation_id} "
            f"normalization={self.normalization_id} #{self.attempt_number} "
            f"{self.status}>"
        )


class ValidationFindingRow(Base):
    __tablename__ = "invoice_validation_findings"

    __table_args__ = (
        UniqueConstraint(
            "validation_id",
            "position",
            name="uq_invoice_validation_findings_validation_id_position",
        ),
        CheckConstraint("position >= 0", name="position_non_negative"),
        CheckConstraint(_FIELD_PATH_CHECK, name="field_path_shape"),
    )

    validation_finding_id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, default=uuid.uuid4
    )
    validation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("invoice_validations.validation_id", ondelete="CASCADE"),
        nullable=False,
    )

    # 0-based order within the attempt; the engine emits findings in
    # rule-catalogue order so the stored list is reproducible.
    position: Mapped[int] = mapped_column(Integer, nullable=False)

    rule: Mapped[ValidationRule] = mapped_column(
        Enum(
            ValidationRule,
            name="validation_rule",
            native_enum=True,
            validate_strings=True,
            values_callable=lambda enum_type: [member.value for member in enum_type],
        ),
        nullable=False,
    )
    severity: Mapped[FindingSeverity] = mapped_column(
        Enum(
            FindingSeverity,
            name="finding_severity",
            native_enum=True,
            validate_strings=True,
            values_callable=lambda enum_type: [member.value for member in enum_type],
        ),
        nullable=False,
    )

    # A Stage 4 field path (scalar name or "line_items.<index>.<field>") or NULL
    # for an invoice-level finding. Shape enforced by a CHECK above.
    field_path: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Client-safe display values a reviewer needs to see, already stringified
    # (a Decimal is stored as its canonical string; the contract's
    # ``str | Decimal`` union serialises identically either way). NULL when the
    # rule has no meaningful pair.
    expected: Mapped[str | None] = mapped_column(Text, nullable=True)
    actual: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Fixed, generic, client-safe sentence. Never a path, secret, or stack trace.
    message: Mapped[str] = mapped_column(Text, nullable=False)

    # Rule-specific structured facts (the threshold used, a matched sibling
    # document id, a signed delta, ...). Contract-valid Decimal and UUID values
    # are losslessly stored as JSON strings by the custom JSONB type.
    context: Mapped[dict[str, Any]] = mapped_column(
        _ValidationContextType(), nullable=False, default=dict
    )

    created_at: Mapped[datetime] = mapped_column(nullable=False, default=_utcnow)

    validation: Mapped[ValidationAttempt] = relationship(back_populates="findings")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<ValidationFindingRow {self.validation_finding_id} "
            f"validation={self.validation_id} pos={self.position} "
            f"{self.rule} {self.severity}>"
        )
