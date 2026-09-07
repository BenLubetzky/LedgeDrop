"""create review table and extend document_status

Revision ID: 0006_review_tables
Revises: 0005_decision_tables
Create Date: 2026-09-06

Stage 7 package 1. Two changes:

1. Adds the ``APPROVED`` and ``REJECTED`` labels to the native
   ``document_status`` enum. ``ALTER TYPE ... ADD VALUE`` cannot be used in the
   same transaction that then references the new value, so this uses the
   rename-swap pattern instead (rename old type -> create the seven-label type
   -> drop the column default -> ``ALTER COLUMN ... TYPE ... USING
   status::text::document_status`` -> restore the default -> drop the old
   type). It runs inside Alembic's transaction and round-trips.
2. Adds the ``invoice_reviews`` table and the ``review_action`` enum, matching
   ``app/models/review.py``. One row per terminal human resolution of a Stage 6
   ``NEEDS_REVIEW`` decision; a ``UNIQUE`` constraint on ``decision_id``
   enforces at most one review per decision.

Touches nothing else in ``documents`` (only the ``status`` column *type*, not a
value) or in any Stage 3-6 table - every Stage 2-6 row is left exactly as it
is. ``downgrade()`` drops ``invoice_reviews`` and the ``review_action`` type,
then swaps ``document_status`` back to its original five labels; that swap
succeeds as long as no row is ``APPROVED``/``REJECTED`` (by which point
``invoice_reviews`` is gone, so nothing produces those values), and fails
loudly otherwise - the correct behaviour for a downgrade that would lose audit
state.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006_review_tables"
down_revision: Union[str, None] = "0005_decision_tables"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Enum labels, matching app/models/document.py and app/schemas/review.py.
_OLD_DOCUMENT_STATUS = ("UPLOADED", "PROCESSING", "COMPLETED", "NEEDS_REVIEW", "FAILED")
_NEW_DOCUMENT_STATUS = _OLD_DOCUMENT_STATUS + ("APPROVED", "REJECTED")
_REVIEW_ACTION = ("APPROVE", "REJECT")

# Kept byte-for-byte in sync with ``_NOTE_SHAPE`` in app/models/review.py.
_NOTE_SHAPE = (
    "(action = 'REJECT' AND note IS NOT NULL AND length(btrim(note)) > 0) OR "
    "(action = 'APPROVE' AND (note IS NULL OR length(btrim(note)) > 0))"
)


def _swap_document_status(new_values: tuple[str, ...]) -> None:
    """Rebuild the ``document_status`` enum type with ``new_values``.

    Safe inside a transaction (unlike ``ALTER TYPE ... ADD VALUE``) and
    reversible: call it again with the other label set. The ``USING`` cast
    fails if a live row holds a label absent from ``new_values`` - intended,
    so a downgrade that would drop a real ``APPROVED``/``REJECTED`` row is
    refused rather than silently corrupting it.
    """
    op.execute("ALTER TYPE document_status RENAME TO document_status_old")
    sa.Enum(*new_values, name="document_status").create(op.get_bind(), checkfirst=False)
    op.execute("ALTER TABLE documents ALTER COLUMN status DROP DEFAULT")
    op.execute(
        "ALTER TABLE documents ALTER COLUMN status TYPE document_status "
        "USING status::text::document_status"
    )
    op.execute(
        "ALTER TABLE documents ALTER COLUMN status "
        "SET DEFAULT 'UPLOADED'::document_status"
    )
    op.execute("DROP TYPE document_status_old")


def upgrade() -> None:
    _swap_document_status(_NEW_DOCUMENT_STATUS)

    # The review_action enum type is created automatically by create_table
    # because the action column uses it; it is dropped explicitly in
    # downgrade().
    op.create_table(
        "invoice_reviews",
        sa.Column("review_id", sa.Uuid(), nullable=False),
        sa.Column("decision_id", sa.Uuid(), nullable=False),
        sa.Column(
            "action",
            sa.Enum(*_REVIEW_ACTION, name="review_action"),
            nullable=False,
        ),
        sa.Column("reviewer_name", sa.String(length=200), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("policy_version", sa.String(length=32), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "length(btrim(reviewer_name)) > 0",
            name=op.f("ck_invoice_reviews_reviewer_name_not_blank"),
        ),
        sa.CheckConstraint(
            _NOTE_SHAPE,
            name=op.f("ck_invoice_reviews_note_shape"),
        ),
        sa.ForeignKeyConstraint(
            ["decision_id"],
            ["invoice_decisions.decision_id"],
            name=op.f("fk_invoice_reviews_decision_id_invoice_decisions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("review_id", name=op.f("pk_invoice_reviews")),
        sa.UniqueConstraint("decision_id", name="uq_invoice_reviews_decision_id"),
    )


def downgrade() -> None:
    op.drop_table("invoice_reviews")
    # drop_table does not drop the native enum type; do it explicitly so a
    # re-upgrade does not fail with "type ... already exists".
    sa.Enum(name="review_action").drop(op.get_bind(), checkfirst=True)
    _swap_document_status(_OLD_DOCUMENT_STATUS)
