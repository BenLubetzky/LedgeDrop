"""create reviewer-correction tables

Revision ID: 0008_correction_tables
Revises: 0007_defer_reason_finding_fk
Create Date: 2026-09-08

Stage 8 package 1. Adds the two correction persistence tables
(``invoice_corrections``, ``invoice_correction_fields``), the
``correction_status`` and ``correction_operation`` enum types, and the
``invoice_normalizations.source_correction_id`` provenance column, matching
``app/models/correction.py`` and the ``source_correction_id`` addition to
``app/models/normalization.py``.

Touches no existing row in any Stage 2-7 table - the corrected canonical
values are written as a *new* ``invoice_normalizations`` attempt (tagged with
``source_correction_id``) beside the immutable Stage 4 engine attempt. The
``document_status`` and ``decision_outcome`` enum types already exist (from
``0001`` and ``0005``); the ``invoice_corrections`` columns that reuse them
pass ``create_type=False``. ``downgrade()`` drops only the Stage 8 objects and
the two new enum types, so Stage 2-7 data round-trips unchanged through
downgrade + re-upgrade.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008_correction_tables"
down_revision: Union[str, None] = "0007_defer_reason_finding_fk"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_CORRECTION_STATUS = ("PROCESSING", "COMPLETED", "FAILED")
_CORRECTION_OPERATION = ("SET_FIELD", "ADD_LINE_ITEM", "REMOVE_LINE_ITEM")

# Pre-existing enum types reused by invoice_corrections columns.
_DOCUMENT_STATUS = (
    "UPLOADED",
    "PROCESSING",
    "COMPLETED",
    "NEEDS_REVIEW",
    "FAILED",
    "APPROVED",
    "REJECTED",
)
_DECISION_OUTCOME = ("ACCEPTED", "NEEDS_REVIEW")
_NORMALIZATION_ERROR_CODE = (
    "invalid_date",
    "invalid_currency",
    "unknown_currency",
    "invalid_number",
    "ambiguous_number",
    "text_too_long",
)

# Kept byte-for-byte in sync with app/models/correction.py.
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

_FIELD_PATH_CHECK = (
    "field_path IN ('invoice_number', 'invoice_date', 'due_date', "
    "'vendor_name', 'vendor_tax_id', 'customer_name', 'currency', "
    "'subtotal', 'tax_amount', 'total_amount') OR "
    r"field_path ~ '^line_items\.(0|[1-9][0-9]*)"
    r"(\.(description|quantity|unit_price|line_total))?$'"
)


def _reused_enum(labels: tuple[str, ...], name: str) -> postgresql.ENUM:
    """A reference to a native enum this migration must not create or drop.

    ``postgresql.ENUM(create_type=False)`` is a hard "already exists" - unlike
    a generic ``sa.Enum(create_type=False)``, which still emits ``CREATE TYPE``
    when the type is unknown to the current process's memo (which it is on a
    re-``upgrade`` that starts above ``0001``).
    """
    return postgresql.ENUM(*labels, name=name, create_type=False)


def upgrade() -> None:
    op.create_table(
        "invoice_corrections",
        sa.Column("correction_id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("source_normalization_id", sa.Uuid(), nullable=False),
        sa.Column("source_decision_id", sa.Uuid(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(*_CORRECTION_STATUS, name="correction_status"),
            nullable=False,
        ),
        sa.Column("reviewer_name", sa.String(length=200), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("policy_version", sa.String(length=32), nullable=False),
        sa.Column(
            "origin_document_status",
            _reused_enum(_DOCUMENT_STATUS, "document_status"),
            nullable=False,
        ),
        sa.Column(
            "submitted_payload",
            sa.dialects.postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("resulting_normalization_id", sa.Uuid(), nullable=True),
        sa.Column("resulting_validation_id", sa.Uuid(), nullable=True),
        sa.Column("resulting_decision_id", sa.Uuid(), nullable=True),
        sa.Column(
            "resulting_outcome",
            _reused_enum(_DECISION_OUTCOME, "decision_outcome"),
            nullable=True,
        ),
        sa.Column(
            "resulting_document_status",
            _reused_enum(_DOCUMENT_STATUS, "document_status"),
            nullable=True,
        ),
        sa.Column("failure_code", sa.String(length=64), nullable=True),
        sa.Column("failure_message", sa.Text(), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "attempt_number >= 1",
            name=op.f("ck_invoice_corrections_attempt_number_positive"),
        ),
        sa.CheckConstraint(
            _STATUS_FIELDS_CONSISTENT,
            name=op.f("ck_invoice_corrections_status_fields_consistent"),
        ),
        sa.CheckConstraint(
            "length(btrim(reviewer_name)) > 0",
            name=op.f("ck_invoice_corrections_reviewer_name_not_blank"),
        ),
        sa.CheckConstraint(
            "note IS NULL OR length(btrim(note)) > 0",
            name=op.f("ck_invoice_corrections_note_not_blank"),
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.document_id"],
            name=op.f("fk_invoice_corrections_document_id_documents"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_normalization_id"],
            ["invoice_normalizations.normalization_id"],
            name=op.f(
                "fk_invoice_corrections_source_normalization_id_invoice_normalizations"
            ),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_decision_id"],
            ["invoice_decisions.decision_id"],
            name=op.f("fk_invoice_corrections_source_decision_id_invoice_decisions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["resulting_normalization_id"],
            ["invoice_normalizations.normalization_id"],
            name=op.f(
                "fk_invoice_corrections_resulting_normalization_id_invoice_normalizations"
            ),
        ),
        sa.ForeignKeyConstraint(
            ["resulting_validation_id"],
            ["invoice_validations.validation_id"],
            name=op.f(
                "fk_invoice_corrections_resulting_validation_id_invoice_validations"
            ),
        ),
        sa.ForeignKeyConstraint(
            ["resulting_decision_id"],
            ["invoice_decisions.decision_id"],
            name=op.f("fk_invoice_corrections_resulting_decision_id_invoice_decisions"),
        ),
        sa.PrimaryKeyConstraint("correction_id", name=op.f("pk_invoice_corrections")),
        sa.UniqueConstraint(
            "document_id",
            "attempt_number",
            name="uq_invoice_corrections_document_id_attempt_number",
        ),
    )
    op.create_index(
        "uq_invoice_corrections_one_active_per_document",
        "invoice_corrections",
        ["document_id"],
        unique=True,
        postgresql_where=sa.text("status = 'PROCESSING'"),
    )

    op.create_table(
        "invoice_correction_fields",
        sa.Column("correction_field_id", sa.Uuid(), nullable=False),
        sa.Column("correction_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column(
            "operation",
            sa.Enum(*_CORRECTION_OPERATION, name="correction_operation"),
            nullable=False,
        ),
        sa.Column("field_path", sa.Text(), nullable=False),
        sa.Column("previous_value", sa.Text(), nullable=True),
        sa.Column("raw_value", sa.Text(), nullable=True),
        sa.Column("normalized_value", sa.Text(), nullable=True),
        sa.Column(
            "error_code",
            _reused_enum(_NORMALIZATION_ERROR_CODE, "normalization_error_code"),
            nullable=True,
        ),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "position >= 0",
            name=op.f("ck_invoice_correction_fields_position_non_negative"),
        ),
        sa.CheckConstraint(
            "(error_code IS NULL) = (error_message IS NULL)",
            name=op.f("ck_invoice_correction_fields_error_pair"),
        ),
        sa.CheckConstraint(
            _FIELD_PATH_CHECK,
            name=op.f("ck_invoice_correction_fields_field_path_shape"),
        ),
        sa.ForeignKeyConstraint(
            ["correction_id"],
            ["invoice_corrections.correction_id"],
            name=op.f(
                "fk_invoice_correction_fields_correction_id_invoice_corrections"
            ),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "correction_field_id", name=op.f("pk_invoice_correction_fields")
        ),
        sa.UniqueConstraint(
            "correction_id",
            "position",
            name="uq_invoice_correction_fields_correction_id_position",
        ),
        sa.UniqueConstraint(
            "correction_id",
            "field_path",
            name="uq_invoice_correction_fields_correction_id_field_path",
        ),
    )

    op.add_column(
        "invoice_normalizations",
        sa.Column("source_correction_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        op.f(
            "fk_invoice_normalizations_source_correction"
        ),
        "invoice_normalizations",
        "invoice_corrections",
        ["source_correction_id"],
        ["correction_id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("fk_invoice_normalizations_source_correction"),
        "invoice_normalizations",
        type_="foreignkey",
    )
    op.drop_column("invoice_normalizations", "source_correction_id")

    op.drop_table("invoice_correction_fields")
    op.drop_index(
        "uq_invoice_corrections_one_active_per_document",
        table_name="invoice_corrections",
        postgresql_where=sa.text("status = 'PROCESSING'"),
    )
    op.drop_table("invoice_corrections")

    # drop_table does not drop native enum types; drop only the two Stage 8
    # ones (document_status / decision_outcome / normalization_error_code
    # pre-date Stage 8 and stay).
    sa.Enum(name="correction_operation").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="correction_status").drop(op.get_bind(), checkfirst=True)
