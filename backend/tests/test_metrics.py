"""Stage 9 business-metric behavior."""

from __future__ import annotations

from enum import Enum
from types import SimpleNamespace

import pytest

from app.core.metrics import PIPELINE_FAILURES, PIPELINE_OUTCOMES, observe_stage


class _Value(str, Enum):
    FAILED = "FAILED"
    COMPLETED = "COMPLETED"
    ACCEPTED = "ACCEPTED"


async def _result(*, failed: bool = False, accepted: bool = False):
    return SimpleNamespace(
        status=_Value.FAILED if failed else _Value.COMPLETED,
        outcome=_Value.ACCEPTED if accepted else None,
    )


@pytest.mark.asyncio
async def test_observe_stage_counts_technical_failure():
    metric = PIPELINE_FAILURES.labels(stage="test-stage")
    outcome = PIPELINE_OUTCOMES.labels(outcome="FAILED")
    before = metric._value.get()
    outcome_before = outcome._value.get()

    await observe_stage("test-stage", _result(failed=True))

    assert metric._value.get() == before + 1
    assert outcome._value.get() == outcome_before + 1


@pytest.mark.asyncio
async def test_observe_decision_counts_terminal_outcome():
    metric = PIPELINE_OUTCOMES.labels(outcome="ACCEPTED")
    before = metric._value.get()

    await observe_stage("decision", _result(accepted=True))

    assert metric._value.get() == before + 1
