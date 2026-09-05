"""Internal invoice-decision data contract (Stage 6, package 1).

This module is pure data definition. It describes the shape a completed
Stage 5 validation attempt is decided *into*: a business outcome plus the
ordered list of reasons that explain it. **No AI provider is called here, no
external-network call is made, and there is no database access.**

It is the *internal* contract - not the public API response and not a
database row (both come in a later package). The full boundary and the
pinned decision policy live in ``docs/stage-6-decision.md``.

Boundary (spec Part 1): **Stage 6 decides accept vs. review; it does not
approve, reject, or otherwise close out an invoice.** This contract therefore
has, by construction:

* a :class:`DecisionOutcome` of exactly ``ACCEPTED | NEEDS_REVIEW`` - no
  ``REJECTED`` / ``APPROVED`` / ``DENIED`` / ``ESCALATED``. Automatic
  rejection and human approval/rejection are later, separate work;
* a :class:`DecisionStatus` of exactly ``PROCESSING | COMPLETED | FAILED`` -
  the technical lifecycle of one decision attempt, mirroring
  :class:`app.schemas.validation.ValidationStatus`. A decision attempt that
  reaches an outcome - including ``NEEDS_REVIEW`` - is ``COMPLETED``;
  ``FAILED`` is a technical fault only, never a business outcome;
* a :class:`DecisionReason` list that never discards a Stage 5 finding's
  significance: every finding that maps to a reason keeps its reason in the
  list even when it does not gate the outcome, so an ``ACCEPTED`` decision
  can still show, for example, that a critical field's confidence was
  unavailable or that a line-item sum could not be checked. (A high-value
  invoice is gating under the current policy and therefore needs review.)

A structured reason (:class:`DecisionReason`) records:

* ``code`` - a stable code from the closed catalogue
  (:class:`DecisionReasonCode`, spec Part 2). Fifteen codes mirror a
  :class:`~app.schemas.validation.ValidationRule` value exactly (same
  string); one (``manual_review_requested``) has no Stage 5 counterpart.
* ``triggers_review`` - whether *this* reason, by itself, requires
  ``NEEDS_REVIEW``. Stored per reason rather than re-derived from the current
  policy table at read time, so a past decision's explanation stays accurate
  even if the policy is retuned later.
* ``source_rule`` - the :class:`~app.schemas.validation.ValidationRule` this
  reason came from, or ``null`` exactly for ``manual_review_requested``.
* ``field_path`` - the same Stage 4 field path the originating finding
  carried (or ``null`` for an invoice-level reason or the manual override).
* ``message`` - a fixed, generic, client-safe sentence.

A reason does **not** repeat a finding's ``expected`` / ``actual`` /
``context``; the full detail for a given reason stays on the linked Stage 5
validation attempt, identified here by ``source_validation_id`` and persisted
by package 2, so nothing is duplicated or allowed to drift out of step with
it.

Unknown keys are rejected (``extra="forbid"``) on every model.
"""

from __future__ import annotations

import uuid
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.validation import ValidationRule, require_stage4_field_path

__all__ = [
    "DecisionStatus",
    "DecisionOutcome",
    "DecisionReasonCode",
    "DecisionReason",
    "InvoiceDecision",
    "DecidedInvoiceResult",
    "DECISION_REASON_CODES",
    "DECISION_REASON_FIELD_NAMES",
]


# --- attempt status and business outcome ---------------------------------


class DecisionStatus(str, Enum):
    """Lifecycle state of one decision attempt.

    Exactly three members, mirroring :class:`ValidationStatus`. A
    ``NEEDS_REVIEW`` outcome is a successful, ``COMPLETED`` decision - it is
    not a ``FAILED`` attempt.
    """

    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class DecisionOutcome(str, Enum):
    """The business outcome of one completed decision attempt.

    Exactly two members. There is deliberately no ``REJECTED`` / ``APPROVED``
    / ``DENIED`` / ``ESCALATED``: Stage 6 either clears an invoice or routes
    it to a human; it never auto-rejects and it never records a human's
    approval or rejection (that is later, separate work).
    """

    ACCEPTED = "ACCEPTED"
    NEEDS_REVIEW = "NEEDS_REVIEW"


class DecisionReasonCode(str, Enum):
    """Closed catalogue of decision reasons (spec Part 2).

    The first fifteen members share their string value with the matching
    :class:`~app.schemas.validation.ValidationRule` member on purpose, so a
    reason and the finding it came from are trivially correlated. The
    sixteenth, ``manual_review_requested``, is Stage 6's own: a caller can
    ask for review directly, independent of any Stage 5 finding.
    """

    MISSING_REQUIRED_FIELD = "missing_required_field"
    NORMALIZATION_ERROR = "normalization_error"
    DUE_DATE_BEFORE_INVOICE_DATE = "due_date_before_invoice_date"
    DUE_DATE_FAR_AFTER_INVOICE_DATE = "due_date_far_after_invoice_date"
    INVOICE_DATE_IN_FUTURE = "invoice_date_in_future"
    INVOICE_DATE_IMPLAUSIBLY_OLD = "invoice_date_implausibly_old"
    TOTALS_DO_NOT_RECONCILE = "totals_do_not_reconcile"
    LINE_ITEM_AMOUNT_MISMATCH = "line_item_amount_mismatch"
    LINE_ITEMS_DO_NOT_SUM = "line_items_do_not_sum"
    LINE_ITEM_SUM_NOT_CHECKED = "line_item_sum_not_checked"
    LOW_CONFIDENCE_CRITICAL_FIELD = "low_confidence_critical_field"
    CRITICAL_FIELD_CONFIDENCE_UNAVAILABLE = "critical_field_confidence_unavailable"
    PROBABLE_DUPLICATE_INVOICE = "probable_duplicate_invoice"
    HIGH_VALUE_INVOICE = "high_value_invoice"
    NO_LINE_ITEMS = "no_line_items"
    MANUAL_REVIEW_REQUESTED = "manual_review_requested"


# --- the structured reason ------------------------------------------


class DecisionReason(BaseModel):
    """One fact Stage 6 cites to explain a decision.

    A reason is a self-describing record of policy applied to one Stage 5
    finding (or to a caller's manual-review request): which finding it came
    from, whether it gated the outcome, and a client-safe sentence. It never
    itself carries a verdict field beyond ``triggers_review``.
    """

    model_config = ConfigDict(extra="forbid")

    code: DecisionReasonCode
    triggers_review: bool = Field(strict=True)
    source_rule: ValidationRule | None
    field_path: str | None
    message: str = Field(min_length=1)

    @field_validator("field_path")
    @classmethod
    def _check_field_path(cls, value: str | None) -> str | None:
        return require_stage4_field_path(value)

    @field_validator("message")
    @classmethod
    def _check_message(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("message must not be blank")
        return value

    @model_validator(mode="after")
    def _check_source_rule_matches_code(self) -> "DecisionReason":
        if self.code is DecisionReasonCode.MANUAL_REVIEW_REQUESTED:
            if self.source_rule is not None:
                raise ValueError("manual_review_requested has no source_rule")
        elif self.source_rule is None or self.source_rule.value != self.code.value:
            raise ValueError("source_rule must match code for a rule-derived reason")
        return self


# --- the full decision payload ----------------------------------------


class InvoiceDecision(BaseModel):
    """The schema-constrained result of one decision attempt.

    Identity-free, mirroring
    :class:`app.schemas.validation.InvoiceValidation`: the link to the source
    validation attempt is on :class:`DecidedInvoiceResult`, and the
    persistence identity (decision id, attempt number, status, timestamps,
    technical-failure fields) is added by a later package.

    ``outcome`` is fully determined by ``reasons``: ``NEEDS_REVIEW`` exactly
    when at least one reason has ``triggers_review = true``, ``ACCEPTED``
    otherwise. This is a contract invariant, not just an engine convention -
    an ``InvoiceDecision`` that disagrees with its own reasons cannot be
    constructed.
    """

    model_config = ConfigDict(extra="forbid")

    outcome: DecisionOutcome
    reasons: list[DecisionReason]

    @model_validator(mode="after")
    def _check_outcome_matches_reasons(self) -> "InvoiceDecision":
        expected = _outcome_for(self.reasons)
        if self.outcome != expected:
            raise ValueError("outcome does not match reasons")
        return self

    @classmethod
    def from_reasons(cls, reasons: list[DecisionReason]) -> "InvoiceDecision":
        """Build a payload with the outcome derived from ``reasons``.

        ``reasons`` order is preserved verbatim - callers (the package 3
        engine) are responsible for ordering it, typically Stage 5 catalogue
        order followed by a manual-review reason, if any, last.
        """
        materialized = list(reasons)
        return cls(outcome=_outcome_for(materialized), reasons=materialized)


def _outcome_for(reasons: list[DecisionReason]) -> DecisionOutcome:
    if any(reason.triggers_review for reason in reasons):
        return DecisionOutcome.NEEDS_REVIEW
    return DecisionOutcome.ACCEPTED


class DecidedInvoiceResult(BaseModel):
    """An :class:`InvoiceDecision` bound to the validation attempt it came from.

    Mirrors :class:`app.schemas.validation.ValidatedInvoiceResult`: it
    preserves the reference to the source validation attempt and nothing
    else.
    """

    model_config = ConfigDict(extra="forbid")

    source_validation_id: uuid.UUID
    decision: InvoiceDecision


# Derived name tuples so downstream code (the package 2 persistence layer and
# the package 5 API schemas) cannot silently drift out of step with this
# contract.
DECISION_REASON_CODES: tuple[str, ...] = tuple(code.value for code in DecisionReasonCode)
DECISION_REASON_FIELD_NAMES: tuple[str, ...] = tuple(DecisionReason.model_fields)
