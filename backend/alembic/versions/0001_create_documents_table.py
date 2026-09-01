"""create documents table

Revision ID: 0001_create_documents_table
Revises:
Create Date: 2026-09-01

Initial schema for Stage 2 (the upload foundation): a single ``documents`` table
holding metadata about uploaded PDFs. The PDF bytes are stored on the filesystem,
never here.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0001_create_documents_table"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_STATUS_VALUES = ("UPLOADED", "PROCESSING", "COMPLETED", "NEEDS_REVIEW", "FAILED")


def upgrade() -> None:
    # The ``document_status`` enum type is created automatically by create_table
    # because the status column uses it; it is dropped explicitly in downgrade().
    op.create_table(
        "documents",
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("original_filename", sa.String(length=512), nullable=False),
        sa.Column("file_location", sa.String(length=1024), nullable=False),
        sa.Column("file_hash", sa.String(length=64), nullable=False),
        sa.Column("file_size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("page_count", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(*_STATUS_VALUES, name="document_status"),
            server_default=sa.text("'UPLOADED'::document_status"),
            nullable=False,
        ),
        sa.Column("uploaded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("document_id", name="pk_documents"),
    )
    op.create_index("ix_documents_file_hash", "documents", ["file_hash"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_documents_file_hash", table_name="documents")
    op.drop_table("documents")
    sa.Enum(name="document_status").drop(op.get_bind(), checkfirst=True)
