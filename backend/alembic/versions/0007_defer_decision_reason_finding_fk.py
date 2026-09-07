"""defer decision reason source-finding foreign key

Revision ID: 0007_defer_reason_finding_fk
Revises: 0006_review_tables
Create Date: 2026-09-07
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op


revision: str = "0007_defer_reason_finding_fk"
down_revision: Union[str, None] = "0006_review_tables"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "invoice_decision_reasons"
_CONSTRAINT = "fk_invoice_decision_reasons_source_finding_id_invoice_v_abc2"


def upgrade() -> None:
    op.drop_constraint(_CONSTRAINT, _TABLE, type_="foreignkey")
    op.create_foreign_key(
        _CONSTRAINT,
        _TABLE,
        "invoice_validation_findings",
        ["source_finding_id"],
        ["validation_finding_id"],
        deferrable=True,
        initially="DEFERRED",
    )


def downgrade() -> None:
    op.drop_constraint(_CONSTRAINT, _TABLE, type_="foreignkey")
    op.create_foreign_key(
        _CONSTRAINT,
        _TABLE,
        "invoice_validation_findings",
        ["source_finding_id"],
        ["validation_finding_id"],
    )
