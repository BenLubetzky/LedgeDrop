"""Low-cardinality business metrics for the document pipeline."""

from __future__ import annotations

import time
from collections.abc import Awaitable
from typing import Any, TypeVar

from prometheus_client import Counter, Gauge, Histogram

T = TypeVar("T")

PIPELINE_STAGE_DURATION = Histogram(
    "ledgerdrop_pipeline_stage_duration_seconds",
    "Time spent running a pipeline stage.",
    ("stage",),
)
PIPELINE_OUTCOMES = Counter(
    "ledgerdrop_pipeline_outcomes_total",
    "Terminal pipeline decision outcomes.",
    ("outcome",),
)
PIPELINE_FAILURES = Counter(
    "ledgerdrop_pipeline_failures_total",
    "Technically failed pipeline attempts.",
    ("stage",),
)
REVIEW_QUEUE_DEPTH = Gauge(
    "ledgerdrop_review_queue_depth",
    "Documents currently awaiting human review.",
)


async def observe_stage(stage: str, operation: Awaitable[T]) -> T:
    """Await a stage operation and record its durable terminal result."""
    started = time.perf_counter()
    try:
        result = await operation
    finally:
        PIPELINE_STAGE_DURATION.labels(stage=stage).observe(time.perf_counter() - started)

    status = getattr(result, "status", None)
    if getattr(status, "value", status) == "FAILED":
        PIPELINE_FAILURES.labels(stage=stage).inc()
        PIPELINE_OUTCOMES.labels(outcome="FAILED").inc()
    if stage == "decision":
        outcome: Any = getattr(result, "outcome", None)
        outcome_value = getattr(outcome, "value", outcome)
        if outcome_value in {"ACCEPTED", "NEEDS_REVIEW"}:
            PIPELINE_OUTCOMES.labels(outcome=outcome_value).inc()
    return result


__all__ = [
    "PIPELINE_FAILURES",
    "PIPELINE_OUTCOMES",
    "PIPELINE_STAGE_DURATION",
    "REVIEW_QUEUE_DEPTH",
    "observe_stage",
]
