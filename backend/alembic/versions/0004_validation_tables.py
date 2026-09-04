"""create validation tables

Revision ID: 0004_validation_tables
Revises: 0003_normalization_tables
Create Date: 2026-09-04

Stage 5 step 7. Adds the two validation persistence tables
(``invoice_validations``, ``invoice_validation_findings``) and the
``validation_status``, ``validation_rule`` and ``finding_severity`` enum types,
matching ``app/models/validation.py``.

Touches nothing in ``documents``, ``invoice_extractions``, ``invoice_line_items``,
``invoice_normalizations``, ``invoice_normalized_line_items`` or
``invoice_normalization_errors`` - every Stage 2, 3 and 4 row is left exactly as
it is. Attempt history is preserved by the natural key
``(normalization_id, attempt_number)``; the partial unique index
``uq_invoice_validations_one_active_per_normalization`` allows at most one
``PROCESSING`` attempt per source normalization. ``downgrade()`` drops only the
Stage 5 objects and the three new enum types, so Stage 2-4 data round-trips
unchanged through downgrade + re-upgrade.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_validation_tables"
down_revision: Union[str, None] = "0003_normalization_tables"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Enum labels. ``validation_status`` stores the Python member *names*;
# ``validation_rule`` and ``finding_severity`` store the member *values* (lower
# case) - all three matching app/models/validation.py and
# app/schemas/validation.py.
_VALIDATION_STATUS = ("PROCESSING", "COMPLETED", "FAILED")
_VALIDATION_RULE = (
    "missing_required_field",
    "normalization_error",
    "due_date_before_invoice_date",
    "due_date_far_after_invoice_date",
    "invoice_date_in_future",
    "invoice_date_implausibly_old",
    "totals_do_not_reconcile",
    "line_item_amount_mismatch",
    "line_items_do_not_sum",
    "line_item_sum_not_checked",
    "low_confidence_critical_field",
    "critical_field_confidence_unavailable",
    "probable_duplicate_invoice",
    "high_value_invoice",
    "no_line_items",
)
_FINDING_SEVERITY = ("error", "warning", "info")

# Kept byte-for-byte in sync with ``_FIELD_PATH_CHECK`` in
# app/models/validation.py (which builds it from the Stage 4 field-name tuples).
_FIELD_PATH_CHECK = (
    "field_path IS NULL OR field_path IN ('invoice_number', 'invoice_date', "
    "'due_date', 'vendor_name', 'vendor_tax_id', 'customer_name', 'currency', "
    "'subtotal', 'tax_amount', 'total_amount') OR "
    r"field_path ~ '^line_items\.(0|[1-9][0-9]*)\."
    r"(description|quantity|unit_price|line_total)$'"
)

_STATUS_FIELDS_CONSISTENT = (
    "(status = 'PROCESSING' AND completed_at IS NULL "
    "AND failure_code IS NULL AND failure_message IS NULL) OR "
    "(status = 'COMPLETED' AND completed_at IS NOT NULL "
    "AND failure_code IS NULL AND failure_message IS NULL) OR "
    "(status = 'FAILED' AND completed_at IS NOT NULL "
    "AND failure_code IS NOT NULL AND failure_message IS NOT NULL)"
)


def upgrade() -> None:
    # The three enum types are created automatically by create_table because a
    # column uses each; they are dropped explicitly in downgrade().
    op.create_table(
        "invoice_validations",
        sa.Column("validation_id", sa.Uuid(), nullable=False),
        sa.Column("normalization_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(*_VALIDATION_STATUS, name="validation_status"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_code", sa.String(length=64), nullable=True),
        sa.Column("failure_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "attempt_number >= 1",
            name=op.f("ck_invoice_validations_attempt_number_positive"),
        ),
        sa.CheckConstraint(
            _STATUS_FIELDS_CONSISTENT,
            name=op.f("ck_invoice_validations_status_fields_consistent"),
        ),
        sa.ForeignKeyConstraint(
            ["normalization_id"],
            ["invoice_normalizations.normalization_id"],
            name=op.f(
                "fk_invoice_validations_normalization_id_invoice_normalizations"
            ),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "validation_id", name=op.f("pk_invoice_validations")
        ),
        sa.UniqueConstraint(
            "normalization_id",
            "attempt_number",
            name="uq_invoice_validations_normalization_id_attempt_number",
        ),
    )
    op.create_index(
        "uq_invoice_validations_one_active_per_normalization",
        "invoice_validations",
        ["normalization_id"],
        unique=True,
        postgresql_where=sa.text("status = 'PROCESSING'"),
    )

    op.create_table(
        "invoice_validation_findings",
        sa.Column("validation_finding_id", sa.Uuid(), nullable=False),
        sa.Column("validation_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column(
            "rule",
            sa.Enum(*_VALIDATION_RULE, name="validation_rule"),
            nullable=False,
        ),
        sa.Column(
            "severity",
            sa.Enum(*_FINDING_SEVERITY, name="finding_severity"),
            nullable=False,
        ),
        sa.Column("field_path", sa.Text(), nullable=True),
        sa.Column("expected", sa.Text(), nullable=True),
        sa.Column("actual", sa.Text(), nullable=True),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column(
            "context", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "position >= 0",
            name=op.f("ck_invoice_validation_findings_position_non_negative"),
        ),
        sa.CheckConstraint(
            _FIELD_PATH_CHECK,
            name=op.f("ck_invoice_validation_findings_field_path_shape"),
        ),
        sa.ForeignKeyConstraint(
            ["validation_id"],
            ["invoice_validations.validation_id"],
            name=op.f(
                "fk_invoice_validation_findings_validation_id_invoice_validations"
            ),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "validation_finding_id",
            name=op.f("pk_invoice_validation_findings"),
        ),
        sa.UniqueConstraint(
            "validation_id",
            "position",
            name="uq_invoice_validation_findings_validation_id_position",
        ),
    )


def downgrade() -> None:
    op.drop_table("invoice_validation_findings")
    op.drop_index(
        "uq_invoice_validations_one_active_per_normalization",
        table_name="invoice_validations",
        postgresql_where=sa.text("status = 'PROCESSING'"),
    )
    op.drop_table("invoice_validations")
    # drop_table does not drop the native enum types; do it explicitly so a
    # re-upgrade does not fail with "type ... already exists".
    sa.Enum(name="finding_severity").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="validation_rule").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="validation_status").drop(op.get_bind(), checkfirst=True)
