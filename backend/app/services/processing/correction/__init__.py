"""Stage 8 reviewer-correction subsystem.

``docs/stage-8-corrections.md`` is the spec. The subsystem takes a submitted
corrected invoice, merges it onto the document's current normalized result
(``projection``), persists the diff (``repository``), re-runs validation and
the decision over the merged projection, and resolves the document status
(``service``) - all deterministic, no AI, no external-network call.
"""

from __future__ import annotations

from app.services.processing.correction.service import CorrectionService

__all__ = ["CorrectionService"]
