"""Stage 6 decision subsystem.

Package 2 added :mod:`.repository`, the sole reader/writer of the
``invoice_decisions`` / ``invoice_decision_reasons`` tables. Package 3 added
the pure, deterministic evaluator, :mod:`.engine`. Package 4 (this package's
:mod:`.lifecycle` and :mod:`.service`) adds the orchestration that drives a
real decision attempt end to end: locking the source validation and its
owning document, the §6.1/§6.4 lifecycle and stale-source guards, committing
``PROCESSING``, calling :func:`.engine.decide`, persisting via
:class:`.DecisionRepository`, and writing the §6.2 document-status outcome.
The public API and pipeline integration are package 5.
"""

from app.services.processing.decision.engine import (
    decide,
    manual_review_reason,
    reason_for_finding,
)
from app.services.processing.decision.repository import DecisionRepository
from app.services.processing.decision.service import DecisionService

__all__ = [
    "DecisionRepository",
    "DecisionService",
    "decide",
    "manual_review_reason",
    "reason_for_finding",
]
