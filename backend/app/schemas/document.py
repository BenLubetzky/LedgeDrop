"""Public API representations of a document.

These intentionally omit internal fields - ``file_location`` and ``file_hash``
are never exposed through the API.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, field_serializer

from app.models.document import DocumentStatus


class DocumentRead(BaseModel):
    """Safe, client-facing document metadata."""

    model_config = ConfigDict(from_attributes=True)

    document_id: uuid.UUID
    original_filename: str
    file_size_bytes: int
    page_count: int
    status: DocumentStatus
    uploaded_at: datetime
    updated_at: datetime

    @field_serializer("uploaded_at", "updated_at")
    def _serialize_utc(self, value: datetime) -> str:
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
