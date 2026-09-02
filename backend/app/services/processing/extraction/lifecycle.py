"""Stage 3 processing lifecycle - the valid status transitions.

Document lifecycle::

    UPLOADED -> PROCESSING -> COMPLETED
                          \\-> FAILED
    FAILED   -> PROCESSING -> COMPLETED | FAILED     (explicit retry)

* ``PROCESSING`` is set only when an extraction attempt actually begins.
* ``COMPLETED`` means *extraction* finished - not normalization or validation.
* ``NEEDS_REVIEW`` belongs to the later decision/escalation stage and is never
  assigned here, not even for low confidence.

Extraction-attempt lifecycle::

    PROCESSING -> COMPLETED
    PROCESSING -> FAILED
    (COMPLETED and FAILED are terminal; a retry creates a *new* attempt)

The guard functions raise :class:`ConflictError` for an illegal *caller-driven*
transition (something a client asked for) and :class:`ValueError` for an illegal
*internal* one (a bug in the orchestration).
"""

from __future__ import annotations

from typing import Literal

from app.core.errors import ConflictError
from app.models.document import DocumentStatus
from app.models.extraction import ExtractionStatus

Action = Literal["start", "retry"]

# Document statuses each action may legally begin from.
START_FROM: frozenset[DocumentStatus] = frozenset({DocumentStatus.UPLOADED})
RETRY_FROM: frozenset[DocumentStatus] = frozenset({DocumentStatus.FAILED})

# Terminal extraction status reachable from PROCESSING.
_ATTEMPT_TRANSITIONS: dict[ExtractionStatus, frozenset[ExtractionStatus]] = {
    ExtractionStatus.PROCESSING: frozenset(
        {ExtractionStatus.COMPLETED, ExtractionStatus.FAILED}
    ),
    ExtractionStatus.COMPLETED: frozenset(),
    ExtractionStatus.FAILED: frozenset(),
}


def ensure_document_can_extract(current: DocumentStatus, *, action: Action) -> None:
    """Raise :class:`ConflictError` unless ``action`` may begin from ``current``.

    The error code lets the API translate the reason without re-deriving it:

    * ``EXTRACTION_IN_PROGRESS`` - the document is already ``PROCESSING``.
    * ``DOCUMENT_ALREADY_EXTRACTED`` - a ``start`` on a ``COMPLETED`` document.
    * ``EXTRACTION_NOT_FAILED`` - a ``retry`` on a document that has not failed.
    * ``DOCUMENT_NOT_EXTRACTABLE`` - any other illegal starting status.
    """
    allowed = START_FROM if action == "start" else RETRY_FROM
    if current in allowed:
        return

    if current is DocumentStatus.PROCESSING:
        raise ConflictError(
            "An extraction is already in progress for this document.",
            code="EXTRACTION_IN_PROGRESS",
        )
    if action == "retry":
        raise ConflictError(
            "Only a document whose extraction failed can be retried.",
            code="EXTRACTION_NOT_FAILED",
        )
    if current is DocumentStatus.COMPLETED:
        raise ConflictError(
            "This document has already been extracted.",
            code="DOCUMENT_ALREADY_EXTRACTED",
        )
    if current is DocumentStatus.FAILED:
        raise ConflictError(
            "This document's extraction has failed; retry it instead of starting a new one.",
            code="EXTRACTION_NOT_FAILED",
        )
    raise ConflictError(
        "This document cannot be extracted from its current state.",
        code="DOCUMENT_NOT_EXTRACTABLE",
    )


def ensure_attempt_transition(
    current: ExtractionStatus, new: ExtractionStatus
) -> None:
    """Guard an internal attempt-status change; raise ``ValueError`` if invalid."""
    if new not in _ATTEMPT_TRANSITIONS.get(current, frozenset()):
        raise ValueError(
            f"illegal extraction-attempt transition: {current.value} -> {new.value}"
        )


__all__ = [
    "Action",
    "START_FROM",
    "RETRY_FROM",
    "ensure_document_can_extract",
    "ensure_attempt_transition",
]
