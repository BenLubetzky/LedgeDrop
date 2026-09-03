"""Data access for normalization attempts, their line items, and field errors
(Stage 4, step 11).

``NormalizationRepository`` is the only place that reads or writes the
``invoice_normalizations`` / ``invoice_normalized_line_items`` /
``invoice_normalization_errors`` tables. It owns no transaction boundary: it
stages objects on the session and runs queries, and the
:class:`~app.services.processing.normalization.service.NormalizationService`
decides when to flush and commit.

Writing a normalized result goes through
:mod:`app.schemas.normalization_persistence`, so the flat column layout is only
ever derived from the validated :class:`NormalizedInvoice` contract in one
place. The source ``invoice_extractions`` row is never written back to.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.normalization import (
    NormalizationAttempt,
    NormalizationFieldError,
    NormalizationLineItem,
    NormalizationStatus,
)
from app.schemas.normalization import NormalizedInvoice
from app.schemas.normalization_persistence import (
    error_rows,
    line_item_rows,
    scalar_columns,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class NormalizationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # --- reads ------------------------------------------------------------

    @staticmethod
    def _with_children(stmt):
        return stmt.options(
            selectinload(NormalizationAttempt.line_items),
            selectinload(NormalizationAttempt.errors),
        )

    async def get(
        self, normalization_id: uuid.UUID
    ) -> NormalizationAttempt | None:
        """One attempt by id, with its line items and field errors loaded."""
        result = await self._session.execute(
            self._with_children(
                select(NormalizationAttempt).where(
                    NormalizationAttempt.normalization_id == normalization_id
                )
            )
        )
        return result.scalar_one_or_none()

    async def get_for_extraction(
        self, extraction_id: uuid.UUID, normalization_id: uuid.UUID
    ) -> NormalizationAttempt | None:
        """One attempt by id, but only if it derives from ``extraction_id``."""
        result = await self._session.execute(
            self._with_children(
                select(NormalizationAttempt).where(
                    NormalizationAttempt.normalization_id == normalization_id,
                    NormalizationAttempt.extraction_id == extraction_id,
                )
            )
        )
        return result.scalar_one_or_none()

    async def list_for_extraction(
        self, extraction_id: uuid.UUID
    ) -> Sequence[NormalizationAttempt]:
        """Full attempt history for a source extraction, oldest attempt first."""
        result = await self._session.execute(
            self._with_children(
                select(NormalizationAttempt)
                .where(NormalizationAttempt.extraction_id == extraction_id)
                .order_by(NormalizationAttempt.attempt_number)
            )
        )
        return result.scalars().all()

    async def latest_for_extraction(
        self, extraction_id: uuid.UUID
    ) -> NormalizationAttempt | None:
        """The most recent attempt (highest ``attempt_number``) for an extraction."""
        result = await self._session.execute(
            self._with_children(
                select(NormalizationAttempt)
                .where(NormalizationAttempt.extraction_id == extraction_id)
                .order_by(NormalizationAttempt.attempt_number.desc())
                .limit(1)
            )
        )
        return result.scalar_one_or_none()

    async def active_for_extraction(
        self, extraction_id: uuid.UUID
    ) -> NormalizationAttempt | None:
        """The extraction's ``PROCESSING`` attempt, if one exists.

        At most one can exist - a partial unique index on
        ``invoice_normalizations`` enforces it at the database level.
        """
        result = await self._session.execute(
            select(NormalizationAttempt).where(
                NormalizationAttempt.extraction_id == extraction_id,
                NormalizationAttempt.status == NormalizationStatus.PROCESSING,
            )
        )
        return result.scalar_one_or_none()

    async def next_attempt_number(self, extraction_id: uuid.UUID) -> int:
        """``1`` for an extraction's first attempt, otherwise ``max + 1``."""
        result = await self._session.execute(
            select(
                func.coalesce(func.max(NormalizationAttempt.attempt_number), 0) + 1
            ).where(NormalizationAttempt.extraction_id == extraction_id)
        )
        return int(result.scalar_one())

    # --- writes -------------------------------------------------------- --

    def add_attempt(
        self, *, extraction_id: uuid.UUID, attempt_number: int
    ) -> NormalizationAttempt:
        """Create a new ``PROCESSING`` attempt and stage it on the session.

        The caller flushes; the normalized fields, line items, and field errors
        are filled in later by :meth:`apply_result` once a result exists.
        """
        attempt = NormalizationAttempt(
            extraction_id=extraction_id,
            attempt_number=attempt_number,
            status=NormalizationStatus.PROCESSING,
            started_at=_utcnow(),
        )
        self._session.add(attempt)
        return attempt

    def apply_result(
        self, attempt: NormalizationAttempt, normalized: NormalizedInvoice
    ) -> None:
        """Write a validated normalized contract onto ``attempt``.

        Does not change ``attempt.status`` or commit - the service does that
        after this returns, so a half-applied result is never marked
        ``COMPLETED`` and a failing write can be rolled back whole.
        """
        for column, value in scalar_columns(normalized).items():
            setattr(attempt, column, value)
        attempt.line_items = [
            NormalizationLineItem(**row) for row in line_item_rows(normalized)
        ]
        attempt.errors = [
            NormalizationFieldError(**row) for row in error_rows(normalized)
        ]


__all__ = ["NormalizationRepository"]
