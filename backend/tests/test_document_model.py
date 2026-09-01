from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Document, DocumentStatus


async def test_insert_and_read_back_document(db_session: AsyncSession) -> None:
    doc = Document(
        original_filename="invoice-2048.pdf",
        file_location=f"{uuid.uuid4()}/original.pdf",
        file_hash="a" * 64,
        file_size_bytes=248_102,
        page_count=2,
    )
    db_session.add(doc)
    await db_session.commit()

    fetched = (await db_session.execute(select(Document))).scalar_one()
    assert isinstance(fetched.document_id, uuid.UUID)
    assert fetched.status is DocumentStatus.UPLOADED
    assert fetched.file_size_bytes == 248_102
    assert fetched.uploaded_at.tzinfo is not None
    assert fetched.updated_at.tzinfo is not None


async def test_updated_at_changes_on_update(db_session: AsyncSession) -> None:
    doc = Document(
        original_filename="a.pdf",
        file_location=f"{uuid.uuid4()}/original.pdf",
        file_hash="b" * 64,
        file_size_bytes=10,
        page_count=1,
        uploaded_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
        updated_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
    )
    db_session.add(doc)
    await db_session.commit()

    doc.page_count = 3
    await db_session.commit()
    await db_session.refresh(doc)

    assert doc.page_count == 3
    assert doc.updated_at > datetime(2020, 1, 2, tzinfo=timezone.utc)
