"""Reviewer-correction persistence models (Stage 8, package 1).

Two tables plus one column on ``invoice_normalizations``:

* ``invoice_corrections`` - one row per **correction attempt** for a document.
  A document may have several (repeated edits, plus ``retry`` of a technical
  failure), so the natural key is ``(document_id, attempt_number)``. The
  partial unique index ``uq_invoice_corrections_one_active_per_document``
  allows at most one ``PROCESSING`` attempt per document. The row references
  its **source chain** (the base normalization attempt and the ``COMPLETED``
  decision attempt the reviewer acted on) and records the **new** attempt
  chain it produced (``resulting_normalization_id`` /
  ``resulting_validation_id`` / ``resulting_decision_id`` /
  ``resulting_outcome`` / ``resulting_document_status``).
* ``invoice_correction_fields`` - the ordered before/after diff for one
  attempt: one row per field the reviewer actually changed, mirroring
  :class:`app.schemas.correction.CorrectionFieldEntry`.
* ``invoice_normalizations.source_correction_id`` - ``NULL`` on every Stage 4
  engine attempt; set on the projection attempt a correction writes
  (``docs/stage-8-corrections.md`` Part 3.5). The provenance marker that
  distinguishes machine normalization from a reviewer correction.

These rows never modify any Stage 2-7 record or the stored PDF. The corrected
canonical values live in a *new* ``invoice_normalizations`` attempt beside the
immutable engine attempt, never on top of it. The one existing-row write Stage
8 performs is ``documents.status`` (Part 2.4).

The ORM classes are ``CorrectionAttempt`` and ``CorrectionFieldRow`` to stay
distinct from the Pydantic contract types ``InvoiceCorrection`` /
``CorrectionFieldEntry``.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
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

from app.database.base import Base
from app.models.decision import DecisionOutcome
from app.models.document import DocumentStatus
from app.schemas.correction import CorrectionOperation, CorrectionStatus
from app.schemas.normalization import (
    NORMALIZED_LINE_ITEM_FIELD_NAMES,
    NORMALIZED_SCALAR_FIELD_NAMES,
    NormalizationErrorCode,
)

if TYPE_CHECKING:
    from app.models.document import Document

__all__ = [
    "CorrectionStatus",
    "CorrectionOperation",
    "CorrectionAttempt",
    "CorrectionFieldRow",
]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# field_path shape: a Stage 4 scalar name, "line_items.<i>.<leaf>", or
# "line_items.<i>" (an add/remove marker). Built from the normalization
# contract's name tuples so the CHECK cannot drift.
_SCALAR_IN_LIST = ", ".join(f"'{name}'" for name in NORMALIZED_SCALAR_FIELD_NAMES)
_LINE_ITEM_LEAF_ALT = "|".join(NORMALIZED_LINE_ITEM_FIELD_NAMES)
_FIELD_PATH_CHECK = (
    f"field_path IN ({_SCALAR_IN_LIST}) OR "
    rf"field_path ~ '^line_items\.(0|[1-9][0-9]*)(\.({_LINE_ITEM_LEAF_ALT}))?$'"
)

# PROCESSING has no completion, failure, or *business* outcome yet, but may
# carry the partial resulting_normalization_id / resulting_validation_id of
# sub-stage attempts it has already committed; COMPLETED has a completion time
# and every resulting-chain column set, and no failure detail; FAILED has a
# completion time and a client-safe failure code + message, no business
# outcome, and may keep the partial resulting_normalization_id /
# resulting_validation_id of attempts the failed run did create.
_STATUS_FIELDS_CONSISTENT = (
    "(status = 'PROCESSING' AND completed_at IS NULL "
    "AND failure_code IS NULL AND failure_message IS NULL "
    "AND resulting_decision_id IS NULL AND resulting_outcome IS NULL "
    "AND resulting_document_status IS NULL) OR "
    "(status = 'COMPLETED' AND completed_at IS NOT NULL "
    "AND failure_code IS NULL AND failure_message IS NULL "
    "AND resulting_normalization_id IS NOT NULL "
    "AND resulting_validation_id IS NOT NULL "
    "AND resulting_decision_id IS NOT NULL AND resulting_outcome IS NOT NULL "
    "AND resulting_document_status IS NOT NULL) OR "
    "(status = 'FAILED' AND completed_at IS NOT NULL "
    "AND failure_code IS NOT NULL AND failure_message IS NOT NULL "
    "AND resulting_decision_id IS NULL AND resulting_outcome IS NULL "
    "AND resulting_document_status IS NULL)"
)

_REVIEWER_NAME_NOT_BLANK = "length(btrim(reviewer_name)) > 0"
_NOTE_NOT_BLANK = "note IS NULL OR length(btrim(note)) > 0"


class CorrectionAttempt(Base):
    __tablename__ = "invoice_corrections"

    __table_args__ = (
        UniqueConstraint(
            "document_id",
            "attempt_number",
            name="uq_invoice_corrections_document_id_attempt_number",
        ),
        Index(
            "uq_invoice_corrections_one_active_per_document",
            "document_id",
            unique=True,
            postgresql_where=text("status = 'PROCESSING'"),
        ),
        CheckConstraint("attempt_number >= 1", name="attempt_number_positive"),
        CheckConstraint(_STATUS_FIELDS_CONSISTENT, name="status_fields_consistent"),
        CheckConstraint(_REVIEWER_NAME_NOT_BLANK, name="reviewer_name_not_blank"),
        CheckConstraint(_NOTE_NOT_BLANK, name="note_not_blank"),
    )

    correction_id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)

    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.document_id", ondelete="CASCADE"), nullable=False
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)

    # --- source chain (the reviewer acted on these) ------------------------
    source_normalization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("invoice_normalizations.normalization_id", ondelete="CASCADE"),
        nullable=False,
    )
    source_decision_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("invoice_decisions.decision_id", ondelete="CASCADE"),
        nullable=False,
    )

    status: Mapped[CorrectionStatus] = mapped_column(
        Enum(
            CorrectionStatus,
            name="correction_status",
            native_enum=True,
            validate_strings=True,
        ),
        nullable=False,
    )

    # --- attribution (unverified label; Part 2.3) -------------------------
    reviewer_name: Mapped[str] = mapped_column(String(200), nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    policy_version: Mapped[str] = mapped_column(String(32), nullable=False)

    # The document's status when the correction was submitted - the input to
    # the Part 2.4 auto-accept matrix and part of the audit record.
    origin_document_status: Mapped[DocumentStatus] = mapped_column(
        Enum(
            DocumentStatus,
            name="document_status",
            native_enum=True,
            validate_strings=True,
            create_type=False,
        ),
        nullable=False,
    )

    # The raw submitted invoice. Internal only - never in a public response.
    # Kept so retry is exact and the diff stays auditable.
    submitted_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    # --- resulting chain (this correction produced these) ----------------
    resulting_normalization_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("invoice_normalizations.normalization_id"), nullable=True
    )
    resulting_validation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("invoice_validations.validation_id"), nullable=True
    )
    resulting_decision_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("invoice_decisions.decision_id"), nullable=True
    )
    resulting_outcome: Mapped[DecisionOutcome | None] = mapped_column(
        Enum(
            DecisionOutcome,
            name="decision_outcome",
            native_enum=True,
            validate_strings=True,
            create_type=False,
        ),
        nullable=True,
    )
    resulting_document_status: Mapped[DocumentStatus | None] = mapped_column(
        Enum(
            DocumentStatus,
            name="document_status",
            native_enum=True,
            validate_strings=True,
            create_type=False,
        ),
        nullable=True,
    )

    # --- technical failure (client-safe; set only when status is FAILED) --
    failure_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    failure_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    submitted_at: Mapped[datetime] = mapped_column(nullable=False, default=_utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        nullable=False, default=_utcnow, onupdate=_utcnow
    )

    # --- relationships --------------------------------------------------- --
    document: Mapped["Document"] = relationship(back_populates="corrections")
    fields: Mapped[list["CorrectionFieldRow"]] = relationship(
        back_populates="correction",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="CorrectionFieldRow.position",
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<CorrectionAttempt {self.correction_id} doc={self.document_id} "
            f"#{self.attempt_number} {self.status}>"
        )


class CorrectionFieldRow(Base):
    __tablename__ = "invoice_correction_fields"

    __table_args__ = (
        UniqueConstraint(
            "correction_id",
            "position",
            name="uq_invoice_correction_fields_correction_id_position",
        ),
        UniqueConstraint(
            "correction_id",
            "field_path",
            name="uq_invoice_correction_fields_correction_id_field_path",
        ),
        CheckConstraint("position >= 0", name="position_non_negative"),
        CheckConstraint(
            "(error_code IS NULL) = (error_message IS NULL)", name="error_pair"
        ),
        CheckConstraint(_FIELD_PATH_CHECK, name="field_path_shape"),
    )

    correction_field_id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, default=uuid.uuid4
    )
    correction_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("invoice_corrections.correction_id", ondelete="CASCADE"),
        nullable=False,
    )

    position: Mapped[int] = mapped_column(Integer, nullable=False)

    operation: Mapped[CorrectionOperation] = mapped_column(
        Enum(
            CorrectionOperation,
            name="correction_operation",
            native_enum=True,
            validate_strings=True,
        ),
        nullable=False,
    )

    field_path: Mapped[str] = mapped_column(Text, nullable=False)
    previous_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    normalized_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[NormalizationErrorCode | None] = mapped_column(
        Enum(
            NormalizationErrorCode,
            name="normalization_error_code",
            native_enum=True,
            validate_strings=True,
            values_callable=lambda enum_type: [member.value for member in enum_type],
            create_type=False,
        ),
        nullable=True,
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(nullable=False, default=_utcnow)

    correction: Mapped[CorrectionAttempt] = relationship(back_populates="fields")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<CorrectionFieldRow {self.correction_field_id} "
            f"correction={self.correction_id} pos={self.position} "
            f"{self.operation} {self.field_path}>"
        )
