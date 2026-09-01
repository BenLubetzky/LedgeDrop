"""The ``documents`` table.

This is the only persistent model in Stage 2. It records metadata about an
uploaded PDF; the PDF bytes themselves live on the filesystem (see
``app.services.storage``), never in PostgreSQL.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import BigInteger, Enum, Integer, String, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class DocumentStatus(str, enum.Enum):
    """Lifecycle states for a document.

    Only :attr:`UPLOADED` is reachable in Stage 2. The later states are defined
    now so the column type is stable, but nothing transitions a document into
    them until a real processor exists.
    """

    UPLOADED = "UPLOADED"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    FAILED = "FAILED"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Document(Base):
    __tablename__ = "documents"

    document_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        primary_key=True,
        default=uuid.uuid4,
    )

    # The name the user's browser reported. Retained as metadata only - it is
    # never used to build a filesystem path.
    original_filename: Mapped[str] = mapped_column(String(512), nullable=False)

    # Backend-relative location of the stored original PDF. Not exposed through
    # any public API response.
    file_location: Mapped[str] = mapped_column(String(1024), nullable=False)

    # Hex-encoded SHA-256 of the uploaded bytes (64 characters).
    file_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    file_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    page_count: Mapped[int] = mapped_column(Integer, nullable=False)

    status: Mapped[DocumentStatus] = mapped_column(
        Enum(
            DocumentStatus,
            name="document_status",
            # Native PostgreSQL ENUM. The status set only ever grows (see the
            # future states above), and new values are added with an explicit
            # ``ALTER TYPE ... ADD VALUE`` migration.
            native_enum=True,
            validate_strings=True,
        ),
        nullable=False,
        default=DocumentStatus.UPLOADED,
        server_default=text("'UPLOADED'::document_status"),
    )

    uploaded_at: Mapped[datetime] = mapped_column(nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        nullable=False, default=_utcnow, onupdate=_utcnow
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<Document {self.document_id} {self.status} "
            f"{self.original_filename!r} {self.file_size_bytes}B>"
        )
