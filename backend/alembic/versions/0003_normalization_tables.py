"""create normalization tables

Revision ID: 0003_normalization_tables
Revises: 0002_invoice_extraction_tables
Create Date: 2026-09-03

Stage 4 step 4. Adds the three normalization persistence tables
(``invoice_normalizations``, ``invoice_normalized_line_items``,
``invoice_normalization_errors``) and the ``normalization_status`` and
``normalization_error_code`` enum types.

Touches nothing in ``documents``, ``invoice_extractions``, or
``invoice_line_items`` - the Stage 2 and Stage 3 rows are left exactly as they
are. Attempt history is preserved by the natural key
``(extraction_id, attempt_number)``; the partial unique index
``uq_invoice_normalizations_one_active_per_extraction`` allows at most one
``PROCESSING`` attempt per source extraction.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003_normalization_tables"
down_revision: Union[str, None] = "0002_invoice_extraction_tables"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Enum labels. ``normalization_status`` stores the Python member *names*;
# ``normalization_error_code`` stores the member *values* (lower case) - both
# matching app/models/normalization.py.
_NORMALIZATION_STATUS = ("PROCESSING", "COMPLETED", "FAILED")
_NORMALIZATION_ERROR_CODE = (
    "invalid_date",
    "invalid_currency",
    "unknown_currency",
    "invalid_number",
    "ambiguous_number",
    "text_too_long",
)


def upgrade() -> None:
    # The two enum types are created automatically by create_table because a
    # column uses each; they are dropped explicitly in downgrade().
    op.create_table(
        "invoice_normalizations",
        sa.Column("normalization_id", sa.Uuid(), nullable=False),
        sa.Column("extraction_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(*_NORMALIZATION_STATUS, name="normalization_status"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_code", sa.String(length=64), nullable=True),
        sa.Column("failure_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("invoice_number", sa.String(length=100), nullable=True),
        sa.Column("invoice_date", sa.String(length=10), nullable=True),
        sa.Column("due_date", sa.String(length=10), nullable=True),
        sa.Column("vendor_name", sa.String(length=256), nullable=True),
        sa.Column("vendor_tax_id", sa.String(length=60), nullable=True),
        sa.Column("customer_name", sa.String(length=256), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=True),
        sa.Column("subtotal", sa.Numeric(), nullable=True),
        sa.Column("tax_amount", sa.Numeric(), nullable=True),
        sa.Column("total_amount", sa.Numeric(), nullable=True),
        sa.CheckConstraint(
            "attempt_number >= 1",
            name=op.f("ck_invoice_normalizations_attempt_number_positive"),
        ),
        sa.CheckConstraint(
            "(status = 'PROCESSING' AND completed_at IS NULL "
            "AND failure_code IS NULL AND failure_message IS NULL) OR "
            "(status = 'COMPLETED' AND completed_at IS NOT NULL "
            "AND failure_code IS NULL AND failure_message IS NULL) OR "
            "(status = 'FAILED' AND completed_at IS NOT NULL "
            "AND failure_code IS NOT NULL AND failure_message IS NOT NULL)",
            name=op.f("ck_invoice_normalizations_status_fields_consistent"),
        ),
        sa.CheckConstraint(
            "invoice_date IS NULL OR invoice_date ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'",
            name=op.f("ck_invoice_normalizations_invoice_date_iso_shape"),
        ),
        sa.CheckConstraint(
            "due_date IS NULL OR due_date ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'",
            name=op.f("ck_invoice_normalizations_due_date_iso_shape"),
        ),
        sa.CheckConstraint(
            "currency IS NULL OR currency ~ '^[A-Z]{3}$'",
            name=op.f("ck_invoice_normalizations_currency_alpha3_upper"),
        ),
        sa.ForeignKeyConstraint(
            ["extraction_id"],
            ["invoice_extractions.extraction_id"],
            name=op.f("fk_invoice_normalizations_extraction_id_invoice_extractions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "normalization_id", name=op.f("pk_invoice_normalizations")
        ),
        sa.UniqueConstraint(
            "extraction_id",
            "attempt_number",
            name="uq_invoice_normalizations_extraction_id_attempt_number",
        ),
    )
    op.create_index(
        "uq_invoice_normalizations_one_active_per_extraction",
        "invoice_normalizations",
        ["extraction_id"],
        unique=True,
        postgresql_where=sa.text("status = 'PROCESSING'"),
    )

    op.create_table(
        "invoice_normalized_line_items",
        sa.Column("normalized_line_item_id", sa.Uuid(), nullable=False),
        sa.Column("normalization_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("description", sa.String(length=512), nullable=True),
        sa.Column("quantity", sa.Numeric(), nullable=True),
        sa.Column("unit_price", sa.Numeric(), nullable=True),
        sa.Column("line_total", sa.Numeric(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "position >= 0",
            name=op.f("ck_invoice_normalized_line_items_position_non_negative"),
        ),
        sa.ForeignKeyConstraint(
            ["normalization_id"],
            ["invoice_normalizations.normalization_id"],
            name=op.f(
                "fk_invoice_normalized_line_items_normalization_id_invoice_normalizations"
            ),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "normalized_line_item_id",
            name=op.f("pk_invoice_normalized_line_items"),
        ),
        sa.UniqueConstraint(
            "normalization_id",
            "position",
            name="uq_invoice_normalized_line_items_normalization_id_position",
        ),
    )

    op.create_table(
        "invoice_normalization_errors",
        sa.Column("normalization_error_id", sa.Uuid(), nullable=False),
        sa.Column("normalization_id", sa.Uuid(), nullable=False),
        sa.Column("field_path", sa.String(length=64), nullable=False),
        sa.Column("raw_value", sa.Text(), nullable=True),
        sa.Column(
            "code",
            sa.Enum(*_NORMALIZATION_ERROR_CODE, name="normalization_error_code"),
            nullable=False,
        ),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "field_path IN ('invoice_number', 'invoice_date', 'due_date', "
            "'vendor_name', 'vendor_tax_id', 'customer_name', 'currency', "
            "'subtotal', 'tax_amount', 'total_amount') OR "
            r"field_path ~ '^line_items\.(0|[1-9][0-9]*)\."
            r"(description|quantity|unit_price|line_total)$'",
            name=op.f("ck_invoice_normalization_errors_field_path_shape"),
        ),
        sa.ForeignKeyConstraint(
            ["normalization_id"],
            ["invoice_normalizations.normalization_id"],
            name=op.f(
                "fk_invoice_normalization_errors_normalization_id_invoice_normalizations"
            ),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "normalization_error_id",
            name=op.f("pk_invoice_normalization_errors"),
        ),
        sa.UniqueConstraint(
            "normalization_id",
            "field_path",
            name="uq_invoice_normalization_errors_normalization_id_field_path",
        ),
    )


def downgrade() -> None:
    op.drop_table("invoice_normalization_errors")
    op.drop_table("invoice_normalized_line_items")
    op.drop_index(
        "uq_invoice_normalizations_one_active_per_extraction",
        table_name="invoice_normalizations",
        postgresql_where=sa.text("status = 'PROCESSING'"),
    )
    op.drop_table("invoice_normalizations")
    # drop_table does not drop the native enum types; do it explicitly so a
    # re-upgrade does not fail with "type ... already exists".
    sa.Enum(name="normalization_error_code").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="normalization_status").drop(op.get_bind(), checkfirst=True)
