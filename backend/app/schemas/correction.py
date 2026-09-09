"""Internal reviewer-correction data contract (Stage 8, package 1).

Pure data definition - no AI provider, no external-network call, no database
access. It describes the shape of one recorded human correction of a
document's canonical invoice data plus the ordered before/after diff of the
fields the reviewer changed.

The full boundary and the pinned correction policy live in
``docs/stage-8-corrections.md``. Key boundary facts encoded here by
construction:

* a :class:`CorrectionStatus` of exactly ``PROCESSING | COMPLETED | FAILED`` -
  the technical lifecycle of one correction attempt, mirroring
  :class:`app.schemas.decision.DecisionStatus`;
* a :class:`CorrectionOperation` of exactly ``SET_FIELD | ADD_LINE_ITEM |
  REMOVE_LINE_ITEM``;
* a :class:`CorrectionFieldEntry` that carries the base value
  (``previous_value``), the reviewer's text (``raw_value``), the canonical
  result of normalizing it (``normalized_value``), and - together, or not at
  all - a Stage 4 ``error_code`` / ``error_message`` when the reviewer's value
  could not be normalized. A field entry with an error is *data about the
  correction*, never a technical failure of the attempt.

The identity-free :class:`InvoiceCorrection` can be reused; the persistence
identity (``correction_id``, ``attempt_number``, status, timestamps, the
``resulting_*`` links) lives on the ORM model.
"""

from __future__ import annotations

import re
import uuid
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.document import DocumentStatus
from app.schemas.normalization import (
    NORMALIZED_LINE_ITEM_FIELD_NAMES,
    NORMALIZED_SCALAR_FIELD_NAMES,
    NormalizationErrorCode,
)

__all__ = [
    "CorrectionStatus",
    "CorrectionOperation",
    "CorrectionFieldEntry",
    "InvoiceCorrection",
    "CorrectedInvoiceResult",
    "CORRECTION_POLICY_VERSION",
    "CORRECTABLE_DOCUMENT_STATUSES",
    "REVIEWER_NAME_MAX_LENGTH",
    "NOTE_MAX_LENGTH",
    "CORRECTION_FIELD_ROW_NAMES",
    "require_correction_field_path",
]

# Bumped whenever the Part 2 policy (correctable statuses, the auto-accept
# matrix, the reviewer-attribution rule, the diff semantics) changes, so a
# historical correction stays explicable afterwards.
CORRECTION_POLICY_VERSION = "2026-09-08.1"

REVIEWER_NAME_MAX_LENGTH = 200
NOTE_MAX_LENGTH = 4000

# Part 2.2. A document may be corrected only from one of these statuses.
CORRECTABLE_DOCUMENT_STATUSES: frozenset[DocumentStatus] = frozenset(
    {
        DocumentStatus.NEEDS_REVIEW,
        DocumentStatus.COMPLETED,
        DocumentStatus.APPROVED,
        DocumentStatus.REJECTED,
    }
)

# A correction field path is a Stage 4 scalar name, "line_items.<i>.<leaf>", or
# "line_items.<i>" (an add/remove marker). Built from the normalization
# contract's own name tuples so it cannot drift from the field paths Stage 4
# understands.
_LINE_ITEM_LEAF_ALT = "|".join(NORMALIZED_LINE_ITEM_FIELD_NAMES)
_FIELD_PATH_RE = re.compile(
    r"(?:"
    + "|".join(re.escape(name) for name in NORMALIZED_SCALAR_FIELD_NAMES)
    + r"|line_items\.(?:0|[1-9]\d*)(?:\.(?:"
    + _LINE_ITEM_LEAF_ALT
    + r"))?)"
)


def require_correction_field_path(value: str) -> str:
    """Raise unless ``value`` is a well-formed correction field path."""
    if not _FIELD_PATH_RE.fullmatch(value):
        raise ValueError(f"malformed correction field_path: {value!r}")
    return value


class CorrectionStatus(str, Enum):
    """Lifecycle state of one correction attempt.

    A ``NEEDS_REVIEW`` re-decision is a successful, ``COMPLETED`` correction -
    it is not a ``FAILED`` attempt. ``FAILED`` is a technical fault only.
    """

    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class CorrectionOperation(str, Enum):
    """What a single correction field entry does to the base invoice."""

    SET_FIELD = "SET_FIELD"
    ADD_LINE_ITEM = "ADD_LINE_ITEM"
    REMOVE_LINE_ITEM = "REMOVE_LINE_ITEM"


class CorrectionFieldEntry(BaseModel):
    """One changed field in a correction - the before/after diff unit.

    ``normalized_value`` is the canonical result of running the reviewer's
    ``raw_value`` through the same Stage 4 normalizer the engine uses for that
    field. It is ``null`` for a clean absence *or* when ``error_code`` is set.
    ``error_code`` and ``error_message`` are set together or not at all.
    """

    model_config = ConfigDict(extra="forbid")

    operation: CorrectionOperation
    field_path: str = Field(min_length=1)
    previous_value: str | None
    raw_value: str | None
    normalized_value: str | None
    error_code: NormalizationErrorCode | None
    error_message: str | None

    @field_validator("field_path")
    @classmethod
    def _check_field_path(cls, value: str) -> str:
        return require_correction_field_path(value)

    @model_validator(mode="after")
    def _check_shape(self) -> "CorrectionFieldEntry":
        if (self.error_code is None) != (self.error_message is None):
            raise ValueError("error_code and error_message must be set together")
        if self.error_code is not None and self.normalized_value is not None:
            raise ValueError(
                "a field entry with an error must have a null normalized_value"
            )
        if self.operation is CorrectionOperation.SET_FIELD:
            if "line_items." in self.field_path and self.field_path.count(".") != 2:
                raise ValueError("SET_FIELD on a line item needs a leaf field_path")
        else:
            # ADD_LINE_ITEM / REMOVE_LINE_ITEM target a whole line.
            if not re.fullmatch(r"line_items\.(?:0|[1-9]\d*)", self.field_path):
                raise ValueError(
                    "line-item add/remove field_path must be 'line_items.<index>'"
                )
            if self.operation is CorrectionOperation.REMOVE_LINE_ITEM:
                if self.raw_value is not None or self.normalized_value is not None:
                    raise ValueError("REMOVE_LINE_ITEM carries no raw/normalized value")
        return self


class InvoiceCorrection(BaseModel):
    """One recorded human correction: attribution plus the ordered diff.

    Identity-free. ``entries`` is exactly the set of fields that changed
    relative to the base normalization attempt, in a stable order (scalars in
    contract order, then line-item edits by index, then adds, then removes).
    An empty ``entries`` list means the reviewer submitted the invoice
    unchanged - allowed, but the service rejects it as ``422`` (nothing to
    correct) at the API boundary.
    """

    model_config = ConfigDict(extra="forbid")

    reviewer_name: str
    note: str | None
    entries: list[CorrectionFieldEntry]

    @field_validator("reviewer_name")
    @classmethod
    def _trim_reviewer(cls, value: str) -> str:
        trimmed = value.strip()
        if not 1 <= len(trimmed) <= REVIEWER_NAME_MAX_LENGTH:
            raise ValueError(
                f"reviewer_name must be 1-{REVIEWER_NAME_MAX_LENGTH} characters"
            )
        return trimmed

    @field_validator("note")
    @classmethod
    def _trim_note(cls, value: str | None) -> str | None:
        if value is None:
            return None
        trimmed = value.strip()
        if not 1 <= len(trimmed) <= NOTE_MAX_LENGTH:
            raise ValueError(f"note must be 1-{NOTE_MAX_LENGTH} characters when present")
        return trimmed

    @model_validator(mode="after")
    def _check_unique_paths(self) -> "InvoiceCorrection":
        seen = [entry.field_path for entry in self.entries]
        if len(seen) != len(set(seen)):
            raise ValueError("each field_path may appear at most once in a correction")
        return self


class CorrectedInvoiceResult(BaseModel):
    """An :class:`InvoiceCorrection` bound to the source chain it corrected."""

    model_config = ConfigDict(extra="forbid")

    source_normalization_id: uuid.UUID
    source_decision_id: uuid.UUID
    correction: InvoiceCorrection


# Columns of one persisted correction field row, in ORM order.
CORRECTION_FIELD_ROW_NAMES: tuple[str, ...] = (
    "operation",
    "field_path",
    "previous_value",
    "raw_value",
    "normalized_value",
    "error_code",
    "error_message",
)
