"""Data access for decision attempts and their reasons (Stage 6, package 2).

``DecisionRepository`` is the only place that reads or writes the
``invoice_decisions`` / ``invoice_decision_reasons`` tables. It owns no
transaction boundary: it stages objects on the session and runs queries, and
the package 4 service decides when to flush and commit - mirroring
:class:`app.services.processing.validation.repository.ValidationRepository`
exactly.

Writing a decided result goes through
:mod:`app.schemas.decision_persistence`, so the flat column layout is only
ever derived from a validated :class:`InvoiceDecision` contract in one place.
The source ``invoice_validations`` row - and every Stage 2-5 row it
transitively derives from - is never written back to.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.decision import DecisionAttempt, DecisionReasonRow, DecisionStatus
from app.schemas.decision import InvoiceDecision
from app.schemas.decision_persistence import reason_rows


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class DecisionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # --- reads --------------------------------------------------------- --

    @staticmethod
    def _with_children(stmt):
        return stmt.options(selectinload(DecisionAttempt.reasons))

    async def get(self, decision_id: uuid.UUID) -> DecisionAttempt | None:
        """One attempt by id, with its reasons loaded."""
        result = await self._session.execute(
            self._with_children(
                select(DecisionAttempt).where(DecisionAttempt.decision_id == decision_id)
            )
        )
        return result.scalar_one_or_none()

    async def get_for_validation(
        self, validation_id: uuid.UUID, decision_id: uuid.UUID
    ) -> DecisionAttempt | None:
        """One attempt by id, but only if it derives from ``validation_id``."""
        result = await self._session.execute(
            self._with_children(
                select(DecisionAttempt).where(
                    DecisionAttempt.decision_id == decision_id,
                    DecisionAttempt.validation_id == validation_id,
                )
            )
        )
        return result.scalar_one_or_none()

    async def list_for_validation(
        self, validation_id: uuid.UUID
    ) -> Sequence[DecisionAttempt]:
        """Full attempt history for a source validation, oldest attempt first."""
        result = await self._session.execute(
            self._with_children(
                select(DecisionAttempt)
                .where(DecisionAttempt.validation_id == validation_id)
                .order_by(DecisionAttempt.attempt_number)
            )
        )
        return result.scalars().all()

    async def latest_for_validation(
        self, validation_id: uuid.UUID
    ) -> DecisionAttempt | None:
        """The most recent attempt (highest ``attempt_number``) for a validation."""
        result = await self._session.execute(
            self._with_children(
                select(DecisionAttempt)
                .where(DecisionAttempt.validation_id == validation_id)
                .order_by(DecisionAttempt.attempt_number.desc())
                .limit(1)
            )
        )
        return result.scalar_one_or_none()

    async def active_for_validation(
        self, validation_id: uuid.UUID
    ) -> DecisionAttempt | None:
        """The validation's ``PROCESSING`` attempt, if one exists.

        At most one can exist - a partial unique index on
        ``invoice_decisions`` enforces it at the database level.
        """
        result = await self._session.execute(
            select(DecisionAttempt).where(
                DecisionAttempt.validation_id == validation_id,
                DecisionAttempt.status == DecisionStatus.PROCESSING,
            )
        )
        return result.scalar_one_or_none()

    async def next_attempt_number(self, validation_id: uuid.UUID) -> int:
        """``1`` for a validation's first attempt, otherwise ``max + 1``."""
        result = await self._session.execute(
            select(
                func.coalesce(func.max(DecisionAttempt.attempt_number), 0) + 1
            ).where(DecisionAttempt.validation_id == validation_id)
        )
        return int(result.scalar_one())

    # --- writes -------------------------------------------------------- --

    def add_attempt(
        self,
        *,
        validation_id: uuid.UUID,
        attempt_number: int,
        policy_version: str,
    ) -> DecisionAttempt:
        """Create a new ``PROCESSING`` attempt and stage it on the session.

        ``policy_version`` is stamped now, at attempt creation, rather than
        once a result exists - it records which policy revision governed
        this run regardless of how the attempt ends (spec §2.7). The caller
        flushes; the reasons and outcome are filled in later by
        :meth:`apply_result` once a result exists.
        """
        attempt = DecisionAttempt(
            validation_id=validation_id,
            attempt_number=attempt_number,
            status=DecisionStatus.PROCESSING,
            policy_version=policy_version,
            started_at=_utcnow(),
        )
        self._session.add(attempt)
        return attempt

    def apply_result(
        self,
        attempt: DecisionAttempt,
        result: InvoiceDecision,
        source_finding_ids: Sequence[uuid.UUID | None],
    ) -> None:
        """Write a decided result onto ``attempt``.

        Sets ``attempt.outcome`` from ``result`` (already guaranteed
        consistent with ``result.reasons`` by the contract itself) and
        replaces ``attempt.reasons``. Does not change ``attempt.status`` or
        commit - the service does that after this returns, so a
        half-applied result is never marked ``COMPLETED`` and a failing
        write can be rolled back whole.
        """
        attempt.outcome = result.outcome
        attempt.reasons = [
            DecisionReasonRow(**row)
            for row in reason_rows(result, source_finding_ids)
        ]


__all__ = ["DecisionRepository"]
