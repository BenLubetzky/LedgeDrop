"""Data access for extraction attempts and their line items (Stage 3, step 5).

``ExtractionRepository`` is the only place that reads or writes the
``invoice_extractions`` / ``invoice_line_items`` tables. It owns no transaction
boundary: it adds objects to the session and runs queries, and the
:class:`~app.services.processing.extraction.service.ExtractionService` decides
when to flush and commit.

Writing structured fields goes through
:mod:`app.schemas.extraction_persistence`, so the flat column layout is only
ever derived from the validated :class:`InvoiceExtraction` contract in one
place.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.extraction import ExtractionAttempt, ExtractionLineItem, ExtractionStatus
from app.schemas.extraction import InvoiceExtraction
from app.schemas.extraction_persistence import line_item_columns, scalar_columns


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ExtractionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # --- reads ------------------------------------------------------------

    async def get(self, extraction_id: uuid.UUID) -> ExtractionAttempt | None:
        """One attempt by id, with its line items eagerly loaded."""
        result = await self._session.execute(
            select(ExtractionAttempt)
            .where(ExtractionAttempt.extraction_id == extraction_id)
            .options(selectinload(ExtractionAttempt.line_items))
        )
        return result.scalar_one_or_none()

    async def get_for_document(
        self, document_id: uuid.UUID, extraction_id: uuid.UUID
    ) -> ExtractionAttempt | None:
        """One attempt by id, but only if it belongs to ``document_id``."""
        result = await self._session.execute(
            select(ExtractionAttempt)
            .where(
                ExtractionAttempt.extraction_id == extraction_id,
                ExtractionAttempt.document_id == document_id,
            )
            .options(selectinload(ExtractionAttempt.line_items))
        )
        return result.scalar_one_or_none()

    async def list_for_document(
        self, document_id: uuid.UUID
    ) -> Sequence[ExtractionAttempt]:
        """Full attempt history for a document, oldest attempt first."""
        result = await self._session.execute(
            select(ExtractionAttempt)
            .where(ExtractionAttempt.document_id == document_id)
            .order_by(ExtractionAttempt.attempt_number)
            .options(selectinload(ExtractionAttempt.line_items))
        )
        return result.scalars().all()

    async def latest_for_document(
        self, document_id: uuid.UUID
    ) -> ExtractionAttempt | None:
        """The most recent attempt (highest ``attempt_number``) for a document."""
        result = await self._session.execute(
            select(ExtractionAttempt)
            .where(ExtractionAttempt.document_id == document_id)
            .order_by(ExtractionAttempt.attempt_number.desc())
            .limit(1)
            .options(selectinload(ExtractionAttempt.line_items))
        )
        return result.scalar_one_or_none()

    async def active_for_document(
        self, document_id: uuid.UUID
    ) -> ExtractionAttempt | None:
        """The document's ``PROCESSING`` attempt, if one exists.

        At most one can exist - a partial unique index on
        ``invoice_extractions`` enforces it at the database level.
        """
        result = await self._session.execute(
            select(ExtractionAttempt).where(
                ExtractionAttempt.document_id == document_id,
                ExtractionAttempt.status == ExtractionStatus.PROCESSING,
            )
        )
        return result.scalar_one_or_none()

    async def next_attempt_number(self, document_id: uuid.UUID) -> int:
        """``1`` for a document's first attempt, otherwise ``max + 1``."""
        result = await self._session.execute(
            select(func.coalesce(func.max(ExtractionAttempt.attempt_number), 0) + 1).where(
                ExtractionAttempt.document_id == document_id
            )
        )
        return int(result.scalar_one())

    # --- writes --------------------------------------------------------- --

    def add_attempt(
        self,
        *,
        document_id: uuid.UUID,
        attempt_number: int,
        provider_name: str,
        provider_model: str | None = None,
    ) -> ExtractionAttempt:
        """Create a new ``PROCESSING`` attempt and stage it on the session.

        The caller flushes; the extracted fields are filled in later by
        :meth:`apply_result` once a result exists.
        """
        attempt = ExtractionAttempt(
            document_id=document_id,
            attempt_number=attempt_number,
            status=ExtractionStatus.PROCESSING,
            provider_name=provider_name,
            provider_model=provider_model,
            started_at=_utcnow(),
        )
        self._session.add(attempt)
        return attempt

    def apply_result(
        self,
        attempt: ExtractionAttempt,
        extraction: InvoiceExtraction,
        *,
        raw_response: dict[str, Any] | None = None,
    ) -> None:
        """Write a validated contract's fields and line items onto ``attempt``.

        Does not change ``attempt.status`` or commit - the service does that
        after this returns, so a malformed result never reaches here and a
        half-applied result is never marked ``COMPLETED``.
        """
        for column, value in scalar_columns(extraction).items():
            setattr(attempt, column, value)
        attempt.line_items = [
            ExtractionLineItem(**row) for row in line_item_columns(extraction)
        ]
        if raw_response is not None:
            attempt.raw_response = raw_response


__all__ = ["ExtractionRepository"]
