"""Safety tests for the Stage 9 interrupted-worker recovery command."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.document import Document, DocumentStatus
from app.models.extraction import ExtractionAttempt, ExtractionStatus
from scripts.recover_stuck_attempts import recover


async def _stuck_extraction(session: AsyncSession) -> tuple[Document, ExtractionAttempt]:
    document = Document(
        original_filename="invoice.pdf",
        file_location=f"{uuid.uuid4()}/original.pdf",
        file_hash="a" * 64,
        file_size_bytes=100,
        page_count=1,
        status=DocumentStatus.PROCESSING,
    )
    attempt = ExtractionAttempt(
        document=document,
        attempt_number=1,
        status=ExtractionStatus.PROCESSING,
        provider_name="test",
        started_at=datetime.now(timezone.utc) - timedelta(hours=2),
    )
    session.add_all([document, attempt])
    await session.commit()
    return document, attempt


@pytest.mark.asyncio
async def test_recovery_is_dry_run_by_default_behavior(db_session: AsyncSession):
    _, attempt = await _stuck_extraction(db_session)
    attempt_id = attempt.extraction_id

    found = await recover(
        db_session,
        cutoff=datetime.now(timezone.utc) - timedelta(minutes=30),
        selected_stage="extraction",
        apply=False,
    )

    assert found == [f"extraction:{attempt_id}"]
    status = await db_session.scalar(
        select(ExtractionAttempt.status).where(
            ExtractionAttempt.extraction_id == attempt_id
        )
    )
    assert status is ExtractionStatus.PROCESSING


@pytest.mark.asyncio
async def test_apply_makes_stuck_extraction_retryable(db_session: AsyncSession):
    document, attempt = await _stuck_extraction(db_session)
    document_id = document.document_id
    attempt_id = attempt.extraction_id

    await recover(
        db_session,
        cutoff=datetime.now(timezone.utc) - timedelta(minutes=30),
        selected_stage="extraction",
        apply=True,
    )

    recovered = (
        await db_session.execute(
            select(
                ExtractionAttempt.status,
                ExtractionAttempt.failure_code,
                ExtractionAttempt.completed_at,
            ).where(ExtractionAttempt.extraction_id == attempt_id)
        )
    ).one()
    document_status = await db_session.scalar(
        select(Document.status).where(Document.document_id == document_id)
    )
    assert recovered.status is ExtractionStatus.FAILED
    assert recovered.failure_code == "WORKER_INTERRUPTED"
    assert recovered.completed_at is not None
    assert document_status is DocumentStatus.FAILED
