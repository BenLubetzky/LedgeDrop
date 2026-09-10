"""Safely convert stale PROCESSING attempts to retryable FAILED attempts.

Dry-run is the default. Pass ``--apply`` only after checking that no worker can
still own the selected attempts::

    uv run python -m scripts.recover_stuck_attempts --older-than-minutes 30
    uv run python -m scripts.recover_stuck_attempts --older-than-minutes 30 --apply

Rows are locked with ``FOR UPDATE SKIP LOCKED``. Completed history and result
data are never changed; correction attempts retain any partial chain IDs.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.session import SessionLocal
from app.models.correction import CorrectionAttempt, CorrectionStatus
from app.models.decision import DecisionAttempt, DecisionStatus
from app.models.document import Document, DocumentStatus
from app.models.extraction import ExtractionAttempt, ExtractionStatus
from app.models.normalization import NormalizationAttempt, NormalizationStatus
from app.models.validation import ValidationAttempt, ValidationStatus

_FAILURE_CODE = "WORKER_INTERRUPTED"
_FAILURE_MESSAGE = "Processing was interrupted. Retry this stage to try again."


@dataclass(frozen=True)
class Stage:
    name: str
    model: type[Any]
    id_field: str
    time_field: str
    processing: Any
    failed: Any


STAGES = (
    Stage(
        "extraction", ExtractionAttempt, "extraction_id", "started_at",
        ExtractionStatus.PROCESSING, ExtractionStatus.FAILED,
    ),
    Stage(
        "normalization", NormalizationAttempt, "normalization_id", "started_at",
        NormalizationStatus.PROCESSING, NormalizationStatus.FAILED,
    ),
    Stage(
        "validation", ValidationAttempt, "validation_id", "started_at",
        ValidationStatus.PROCESSING, ValidationStatus.FAILED,
    ),
    Stage(
        "decision", DecisionAttempt, "decision_id", "started_at",
        DecisionStatus.PROCESSING, DecisionStatus.FAILED,
    ),
    Stage(
        "correction", CorrectionAttempt, "correction_id", "submitted_at",
        CorrectionStatus.PROCESSING, CorrectionStatus.FAILED,
    ),
)


async def recover(
    session: AsyncSession, *, cutoff: datetime, selected_stage: str | None, apply: bool
) -> list[str]:
    recovered: list[str] = []
    completed_at = datetime.now(timezone.utc)
    for stage in STAGES:
        if selected_stage and stage.name != selected_stage:
            continue
        timestamp = getattr(stage.model, stage.time_field)
        rows = (
            await session.scalars(
                select(stage.model)
                .where(stage.model.status == stage.processing, timestamp <= cutoff)
                .with_for_update(skip_locked=True)
            )
        ).all()
        for row in rows:
            recovered.append(f"{stage.name}:{getattr(row, stage.id_field)}")
            if not apply:
                continue
            row.status = stage.failed
            row.completed_at = completed_at
            row.failure_code = _FAILURE_CODE
            row.failure_message = _FAILURE_MESSAGE
            if stage.name == "extraction":
                document = await session.get(
                    Document, row.document_id, with_for_update=True
                )
                if document is not None and document.status is DocumentStatus.PROCESSING:
                    document.status = DocumentStatus.FAILED
    if apply:
        await session.commit()
    else:
        await session.rollback()
    return recovered


async def _run(args: argparse.Namespace) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=args.older_than_minutes)
    async with SessionLocal() as session:
        rows = await recover(
            session, cutoff=cutoff, selected_stage=args.stage, apply=args.apply
        )
    mode = "RECOVERED" if args.apply else "WOULD RECOVER"
    for row in rows:
        print(f"{mode}: {row}")
    print(f"{mode}: {len(rows)} stale attempt(s)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--older-than-minutes", type=int, required=True)
    parser.add_argument("--stage", choices=[stage.name for stage in STAGES])
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    if args.older_than_minutes < 1:
        parser.error("--older-than-minutes must be at least 1")
    return asyncio.run(_run(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
