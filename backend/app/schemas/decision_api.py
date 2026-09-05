"""Request and public response schemas for the Stage 6 decision API (package 5).

Mirrors :mod:`app.schemas.validation_api` one stage down: the endpoints hang
off a completed Stage 5 validation attempt instead of a completed Stage 4
normalization attempt, and the ``data`` payload is the Stage 6 contract
(:class:`~app.schemas.decision.InvoiceDecision` - ``outcome`` + ordered
``reasons``) instead of validation findings.

Rules that shape the public model (see ``docs/stage-6-decision.md``):

* The response exposes the business ``outcome`` (``ACCEPTED`` / ``NEEDS_REVIEW``),
  the ordered ``reasons`` that explain it, the source validation reference, the
  ``policy_version`` that produced the outcome (spec §2.7), the attempt status,
  and timestamps - and nothing else. There is no approval/rejection vocabulary
  and no re-computed validation detail: a reason names the Stage 5 rule it came
  from and reuses that rule's client-safe message verbatim.
* ``data`` (the ``InvoiceDecision`` payload) is present only on a ``COMPLETED``
  attempt. It is ``null`` while the attempt is ``PROCESSING`` and on a
  ``FAILED`` attempt, which has no business outcome at all (spec §1.5) - a
  failed decision is a technical fault, never ``ACCEPTED``.
* ``failure_code`` / ``failure_message`` are the only technical-failure
  information exposed; the processing layer writes them client-safe (no paths,
  secrets, or stack traces). A ``NEEDS_REVIEW`` outcome is **not** a failure -
  it is a normal ``COMPLETED`` attempt whose ``outcome`` is ``NEEDS_REVIEW``.
* ``manual_review_requested`` on the request only ever *adds* a
  ``manual_review_requested`` reason to the attempt it starts or retries (spec
  §2.4). It cannot remove or downgrade a rule-derived reason, and there is no
  "force accept": a ``COMPLETED`` decision is terminal, so this flag has no way
  to revisit an outcome that already exists.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    model_validator,
)

from app.models.decision import DecisionOutcome, DecisionStatus
from app.schemas.decision import InvoiceDecision
from app.schemas.decision_persistence import invoice_decision_from_rows


class DecisionStartRequest(BaseModel):
    """Body for starting a decision attempt or retrying a failed one.

    ``manual_review_requested`` (default ``false``) asks Stage 6 to add one
    ``manual_review_requested`` reason to this attempt regardless of what the
    deterministic policy concludes (spec §2.4). It is strict: ``0``, ``1``,
    ``"false"`` and ``null`` are rejected rather than coerced, matching the
    engine boundary, because the flag can turn an otherwise accepted invoice
    into ``NEEDS_REVIEW``. Unknown keys are rejected. An empty body is valid
    and means ``manual_review_requested = false``. The same model serves start
    and retry - a retry builds a brand-new attempt, so the flag must be
    supplied again if it is still wanted.
    """

    model_config = ConfigDict(extra="forbid")

    manual_review_requested: bool = Field(default=False, strict=True)


class InvoiceDecisionResult(BaseModel):
    """Client-facing view of a single decision attempt.

    ``data`` is the full internal contract shape on a ``COMPLETED`` attempt:
    the business ``outcome`` and every ordered reason (a closed ``code``,
    whether it ``triggers_review``, the originating Stage 5 ``source_rule`` or
    ``null``, a Stage 4 ``field_path`` or ``null``, and a fixed client-safe
    ``message``). It is ``null`` on a ``PROCESSING`` or ``FAILED`` attempt.

    ``validation_id`` ties the result back to the Stage 5 attempt it was
    derived from. ``outcome`` is also surfaced at the top level (from the
    stored column) for list views; it agrees with ``data.outcome`` whenever
    ``data`` is present. There is no approval/rejection field, no confidence,
    no raw provider payload, and no other internal diagnostics.
    """

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    decision_id: uuid.UUID
    validation_id: uuid.UUID
    attempt_number: int = Field(ge=1)
    status: DecisionStatus
    outcome: DecisionOutcome | None
    policy_version: str

    started_at: AwareDatetime
    completed_at: AwareDatetime | None
    created_at: AwareDatetime
    updated_at: AwareDatetime

    failure_code: str | None
    failure_message: str | None

    data: InvoiceDecision | None

    @model_validator(mode="after")
    def _check_status_fields(self) -> "InvoiceDecisionResult":
        if self.status is DecisionStatus.PROCESSING:
            valid = (
                self.completed_at is None
                and self.failure_code is None
                and self.failure_message is None
                and self.outcome is None
                and self.data is None
            )
        elif self.status is DecisionStatus.COMPLETED:
            valid = (
                self.completed_at is not None
                and self.failure_code is None
                and self.failure_message is None
                and self.outcome is not None
                and self.data is not None
                and self.data.outcome == self.outcome
            )
        else:
            valid = (
                self.completed_at is not None
                and self.failure_code is not None
                and self.failure_message is not None
                and self.outcome is None
                and self.data is None
            )
        if not valid:
            raise ValueError("decision status and outcome/completion/failure fields disagree")
        return self

    @field_serializer("started_at", "completed_at", "created_at", "updated_at")
    def _serialize_utc(self, value: datetime | None) -> str | None:
        if value is None:
            return None
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    @classmethod
    def from_attempt(cls, attempt: object) -> "InvoiceDecisionResult":
        """Build the public result from a persisted ``DecisionAttempt``.

        The position-ordered reason rows are rebuilt into the nested contract
        (and re-validated in the process) only for a ``COMPLETED`` attempt; a
        ``PROCESSING`` or ``FAILED`` attempt carries no business outcome, so
        ``data`` stays ``null``. Nothing from the source validation other than
        its id is read.
        """
        completed = attempt.status is DecisionStatus.COMPLETED
        return cls.model_validate(
            {
                "decision_id": attempt.decision_id,
                "validation_id": attempt.validation_id,
                "attempt_number": attempt.attempt_number,
                "status": attempt.status,
                "outcome": attempt.outcome,
                "policy_version": attempt.policy_version,
                "started_at": attempt.started_at,
                "completed_at": attempt.completed_at,
                "created_at": attempt.created_at,
                "updated_at": attempt.updated_at,
                "failure_code": attempt.failure_code,
                "failure_message": attempt.failure_message,
                "data": (
                    invoice_decision_from_rows(attempt.reasons) if completed else None
                ),
            }
        )


__all__ = ["DecisionStartRequest", "InvoiceDecisionResult"]
