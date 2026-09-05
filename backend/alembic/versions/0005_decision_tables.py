"""create decision tables

Revision ID: 0005_decision_tables
Revises: 0004_validation_tables
Create Date: 2026-09-05

Stage 6 package 2. Adds the two decision persistence tables
(``invoice_decisions``, ``invoice_decision_reasons``) and the
``decision_status``, ``decision_outcome`` and ``decision_reason_code`` enum
types, matching ``app/models/decision.py``.

Touches nothing in ``documents``, ``invoice_extractions``,
``invoice_line_items``, ``invoice_normalizations``,
``invoice_normalized_line_items``, ``invoice_normalization_errors``,
``invoice_validations`` or ``invoice_validation_findings`` - every Stage 2-5
row is left exactly as it is. Attempt history is preserved by the natural key
``(validation_id, attempt_number)``; the partial unique index
``uq_invoice_decisions_one_active_per_validation`` allows at most one
``PROCESSING`` attempt per source validation. ``downgrade()`` drops only the
Stage 6 objects and the three new enum types, so Stage 2-5 data round-trips
unchanged through downgrade + re-upgrade.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005_decision_tables"
down_revision: Union[str, None] = "0004_validation_tables"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Enum labels, matching app/schemas/decision.py.
_DECISION_STATUS = ("PROCESSING", "COMPLETED", "FAILED")
_DECISION_OUTCOME = ("ACCEPTED", "NEEDS_REVIEW")
_DECISION_REASON_CODE = (
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
    "manual_review_requested",
)

# Kept byte-for-byte in sync with ``_FIELD_PATH_CHECK`` in
# app/models/decision.py (and identical to Stage 5's own, since both are
# built from the same Stage 4 field-name tuples).
_FIELD_PATH_CHECK = (
    "field_path IS NULL OR field_path IN ('invoice_number', 'invoice_date', "
    "'due_date', 'vendor_name', 'vendor_tax_id', 'customer_name', 'currency', "
    "'subtotal', 'tax_amount', 'total_amount') OR "
    r"field_path ~ '^line_items\.(0|[1-9][0-9]*)\."
    r"(description|quantity|unit_price|line_total)$'"
)

_STATUS_FIELDS_CONSISTENT = (
    "(status = 'PROCESSING' AND completed_at IS NULL "
    "AND failure_code IS NULL AND failure_message IS NULL "
    "AND outcome IS NULL) OR "
    "(status = 'COMPLETED' AND completed_at IS NOT NULL "
    "AND failure_code IS NULL AND failure_message IS NULL "
    "AND outcome IS NOT NULL) OR "
    "(status = 'FAILED' AND completed_at IS NOT NULL "
    "AND failure_code IS NOT NULL AND failure_message IS NOT NULL "
    "AND outcome IS NULL)"
)

_SOURCE_RULE_MATCHES_CODE = (
    "(code = 'manual_review_requested' AND source_rule IS NULL "
    "AND source_finding_id IS NULL) OR "
    "(code <> 'manual_review_requested' AND source_rule IS NOT NULL "
    "AND source_rule = code::text AND source_finding_id IS NOT NULL)"
)


def upgrade() -> None:
    # The three enum types are created automatically by create_table because a
    # column uses each; they are dropped explicitly in downgrade(). Unlike
    # source_finding_id (a real FK to invoice_validation_findings),
    # source_rule is plain text, not a second native enum reusing Stage 5's
    # validation_rule type - see app/models/decision.py's module docstring.
    op.create_table(
        "invoice_decisions",
        sa.Column("decision_id", sa.Uuid(), nullable=False),
        sa.Column("validation_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(*_DECISION_STATUS, name="decision_status"),
            nullable=False,
        ),
        sa.Column(
            "outcome",
            sa.Enum(*_DECISION_OUTCOME, name="decision_outcome"),
            nullable=True,
        ),
        sa.Column("policy_version", sa.String(length=32), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_code", sa.String(length=64), nullable=True),
        sa.Column("failure_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "attempt_number >= 1",
            name=op.f("ck_invoice_decisions_attempt_number_positive"),
        ),
        sa.CheckConstraint(
            _STATUS_FIELDS_CONSISTENT,
            name=op.f("ck_invoice_decisions_status_fields_consistent"),
        ),
        sa.ForeignKeyConstraint(
            ["validation_id"],
            ["invoice_validations.validation_id"],
            name=op.f("fk_invoice_decisions_validation_id_invoice_validations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("decision_id", name=op.f("pk_invoice_decisions")),
        sa.UniqueConstraint(
            "validation_id",
            "attempt_number",
            name="uq_invoice_decisions_validation_id_attempt_number",
        ),
    )
    op.create_index(
        "uq_invoice_decisions_one_active_per_validation",
        "invoice_decisions",
        ["validation_id"],
        unique=True,
        postgresql_where=sa.text("status = 'PROCESSING'"),
    )

    op.create_table(
        "invoice_decision_reasons",
        sa.Column("decision_reason_id", sa.Uuid(), nullable=False),
        sa.Column("decision_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column(
            "code",
            sa.Enum(*_DECISION_REASON_CODE, name="decision_reason_code"),
            nullable=False,
        ),
        sa.Column("triggers_review", sa.Boolean(), nullable=False),
        sa.Column("source_rule", sa.Text(), nullable=True),
        sa.Column("source_finding_id", sa.Uuid(), nullable=True),
        sa.Column("field_path", sa.Text(), nullable=True),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "position >= 0",
            name=op.f("ck_invoice_decision_reasons_position_non_negative"),
        ),
        sa.CheckConstraint(
            _FIELD_PATH_CHECK,
            name=op.f("ck_invoice_decision_reasons_field_path_shape"),
        ),
        sa.CheckConstraint(
            _SOURCE_RULE_MATCHES_CODE,
            name=op.f("ck_invoice_decision_reasons_source_rule_matches_code"),
        ),
        sa.ForeignKeyConstraint(
            ["decision_id"],
            ["invoice_decisions.decision_id"],
            name=op.f("fk_invoice_decision_reasons_decision_id_invoice_decisions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_finding_id"],
            ["invoice_validation_findings.validation_finding_id"],
            name=op.f(
                "fk_invoice_decision_reasons_source_finding_id_invoice_validation_findings"
            ),
        ),
        sa.PrimaryKeyConstraint(
            "decision_reason_id", name=op.f("pk_invoice_decision_reasons")
        ),
        sa.UniqueConstraint(
            "decision_id",
            "position",
            name="uq_invoice_decision_reasons_decision_id_position",
        ),
    )


def downgrade() -> None:
    op.drop_table("invoice_decision_reasons")
    op.drop_index(
        "uq_invoice_decisions_one_active_per_validation",
        table_name="invoice_decisions",
        postgresql_where=sa.text("status = 'PROCESSING'"),
    )
    op.drop_table("invoice_decisions")
    # drop_table does not drop the native enum types; do it explicitly so a
    # re-upgrade does not fail with "type ... already exists".
    sa.Enum(name="decision_reason_code").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="decision_outcome").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="decision_status").drop(op.get_bind(), checkfirst=True)
