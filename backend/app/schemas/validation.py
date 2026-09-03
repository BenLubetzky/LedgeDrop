"""Internal invoice-validation data contract (Stage 5, step 3).

This module is pure data definition. It describes the shape a completed Stage 4
normalization attempt is validated *into*: an overall attempt status plus a list
of structured findings. **No AI provider is called here, no external-network
call is made, and there is no database access.**

It is the *internal* contract - not the public API response (Stage 5 step 12)
and not a database row (step 6). Later steps derive the rule catalogue (step 4),
the persistence layout (step 6), and the public schemas (step 12) from these
models. The full boundary and the pinned policies live in
``docs/stage-5-validation.md``.

Boundary (spec Part 1): **Stage 5 asserts facts; it does not judge the
invoice.** This contract therefore has, by construction:

* no acceptance / rejection / approval / "valid" / "invalid" / score / verdict
  field, and no ``NEEDS_REVIEW`` or escalation vocabulary anywhere;
* a :class:`ValidationStatus` of exactly ``PROCESSING | COMPLETED | FAILED`` - a
  rule violation still *completes* the attempt with findings; ``FAILED`` is a
  technical fault only;
* a :class:`FindingSeverity` of exactly ``error | warning | info`` that grades
  *how much a human should care*, never a decision;
* a :class:`ValidationSummary` of descriptive counts only, re-derived from the
  finding list and cross-checked, so it can carry nothing the list does not.

A structured finding (:class:`ValidationFinding`) records:

* ``rule`` - a stable code from the closed catalogue (:class:`ValidationRule`,
  spec Part 3). The finding cannot name a rule outside the catalogue.
* ``severity`` - ``error | warning | info``.
* ``field_path`` - a Stage 4 field path (a canonical scalar name such as
  ``total_amount``, or ``line_items.<index>.<field>`` with a zero-based index)
  or ``null`` for an invoice-level finding. The vocabulary is reused verbatim
  from :mod:`app.schemas.normalization` so the two contracts cannot drift.
* ``expected`` / ``actual`` - the client-safe scalar values a reviewer needs to
  see: a string (for example a ``YYYY-MM-DD`` date) or a ``Decimal`` amount, or
  ``null`` when the rule has no meaningful pair. ``Decimal`` serializes as a
  JSON string; a binary ``float`` (or a bare ``int``) is rejected.
* ``message`` - a fixed, generic, client-safe sentence. Never a path, secret,
  stack trace, raw provider payload, or PII beyond what ``expected`` / ``actual``
  legitimately show (the invoice's own normalized values).
* ``context`` - a small JSON object for rule-specific facts (the threshold
  used, a matched sibling ``document_id``, a signed delta, ...). Every numeric
  value inside it must be an ``int`` or a ``Decimal``; a ``float`` anywhere in
  the structure is rejected so the "no binary floating point" rule (spec §2.9)
  holds end to end.

Unknown keys are rejected (``extra="forbid"``) on every model.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from decimal import Decimal
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.normalization import (
    NORMALIZED_LINE_ITEM_FIELD_NAMES,
    NORMALIZED_SCALAR_FIELD_NAMES,
)

__all__ = [
    "ValidationStatus",
    "FindingSeverity",
    "ValidationRule",
    "ValidationFinding",
    "ValidationSummary",
    "InvoiceValidation",
    "ValidatedInvoiceResult",
    "VALIDATION_RULE_CODES",
    "VALIDATION_FINDING_FIELD_NAMES",
]


# --- attempt status and finding grading ----------------------------------


class ValidationStatus(str, Enum):
    """Lifecycle state of one validation attempt.

    Exactly three members. There is deliberately no ``ACCEPTED`` / ``REJECTED``
    / ``NEEDS_REVIEW`` / ``ESCALATED``: a rule that fires produces a finding on
    a ``COMPLETED`` attempt, and only a technical fault yields ``FAILED``.
    """

    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class FindingSeverity(str, Enum):
    """How much a human should care about a finding - not a decision.

    * ``error`` - something required to process the invoice at all is missing or
      unusable.
    * ``warning`` - a probable data problem or inconsistency a reviewer should
      look at.
    * ``info`` - a fact worth recording for the later decision stage that is not
      itself a defect.
    """

    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


class ValidationRule(str, Enum):
    """Closed catalogue of deterministic validation rules (spec Part 3).

    The ``rule`` field of a finding is an enum over exactly these codes, the
    same way :class:`app.schemas.normalization.NormalizationErrorCode` is
    closed. Adding a rule is a deliberate contract change.
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


# --- field-path and value guards ---------------------------------------

_SCALAR_FIELDS: frozenset[str] = frozenset(NORMALIZED_SCALAR_FIELD_NAMES)
_LINE_ITEM_FIELDS: frozenset[str] = frozenset(NORMALIZED_LINE_ITEM_FIELD_NAMES)


def _require_field_path(path: str | None) -> str | None:
    """Accept ``None`` or a Stage 4 field path; reject anything else.

    A valid non-null path is either one of the ten canonical scalar names or
    ``line_items.<index>.<field>`` with a zero-based index (no leading zeros)
    and a known line-item field. The index bound cannot be checked here - a
    single finding does not carry the line-item count - only the shape.
    """
    if path is None:
        return None
    if path in _SCALAR_FIELDS:
        return path
    head, _, rest = path.partition(".")
    if head == "line_items" and rest:
        index_str, _, leaf = rest.partition(".")
        if (
            index_str.isdigit()
            and str(int(index_str)) == index_str
            and leaf in _LINE_ITEM_FIELDS
        ):
            return path
    raise ValueError(f"malformed validation field_path: {path!r}")


def _require_scalar_value(value: Any) -> Any:
    """Accept only a string, a finite ``Decimal``, or ``None``.

    Runs in ``mode="before"`` so a ``float`` is rejected outright rather than
    silently coerced. A bare ``int`` is rejected too: the contract is strings or
    decimals, and an amount must arrive as ``Decimal`` to serialize as a string.
    """
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, bool):  # bool is an int subclass - exclude explicitly
        raise ValueError("expected/actual must be a string, a Decimal, or null")
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("expected/actual must be a finite Decimal")
        return value
    raise ValueError("expected/actual must be a string, a Decimal, or null")


def _require_json_context_value(value: Any, path: str = "context") -> None:
    """Require a JSON-safe value with no binary or non-finite numbers.

    Mapping keys must be strings so ``context`` stays a JSON object. Values may
    be finite ``Decimal`` instances, integers, strings, booleans, ``None``,
    UUIDs, lists, and nested mappings. Rejecting every other Python object here
    prevents a contract-valid finding from failing later when it is serialized
    for JSONB or an API response. The exact per-rule shape is fixed in step 4.
    """
    if isinstance(value, bool):
        return
    if isinstance(value, float):
        raise ValueError(f"{path} must not contain a binary float; use int or Decimal")
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError(f"{path} must not contain a non-finite Decimal")
        return
    if value is None or isinstance(value, (str, int, uuid.UUID)):
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{path} object keys must be strings")
            _require_json_context_value(item, f"{path}.{key}")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _require_json_context_value(item, f"{path}[{index}]")
        return
    raise ValueError(f"{path} contains a value that is not JSON-safe")


# --- the structured finding ------------------------------------------


class ValidationFinding(BaseModel):
    """One fact Stage 5 asserts about the invoice under validation.

    A finding is *data*, not a verdict: it names a rule, grades how much a
    reviewer should care, points at a field where it can, and shows the safe
    expected / actual values. It has no field that expresses a disposition.
    """

    model_config = ConfigDict(extra="forbid")

    rule: ValidationRule
    severity: FindingSeverity
    field_path: str | None
    expected: str | Decimal | None
    actual: str | Decimal | None
    message: str = Field(min_length=1)
    context: dict[str, Any]

    @field_validator("field_path")
    @classmethod
    def _check_field_path(cls, value: str | None) -> str | None:
        return _require_field_path(value)

    @field_validator("expected", "actual", mode="before")
    @classmethod
    def _check_scalar_value(cls, value: Any) -> Any:
        return _require_scalar_value(value)

    @field_validator("message")
    @classmethod
    def _check_message(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("message must not be blank")
        return value

    @field_validator("context")
    @classmethod
    def _check_context(cls, value: dict[str, Any]) -> dict[str, Any]:
        _require_json_context_value(value)
        return value


# --- the descriptive summary --------------------------------------------


class ValidationSummary(BaseModel):
    """Finding counts only - a convenience view of the list, never a decision.

    ``total`` must equal ``error + warning + info``. The decision stage applies
    its own thresholds to these counts later; Stage 5 draws no conclusion from
    them.
    """

    model_config = ConfigDict(extra="forbid")

    total: int = Field(ge=0, strict=True)
    error: int = Field(ge=0, strict=True)
    warning: int = Field(ge=0, strict=True)
    info: int = Field(ge=0, strict=True)

    @model_validator(mode="after")
    def _check_total(self) -> "ValidationSummary":
        if self.total != self.error + self.warning + self.info:
            raise ValueError("summary total must equal error + warning + info")
        return self

    @classmethod
    def from_findings(cls, findings: list["ValidationFinding"]) -> "ValidationSummary":
        counts = dict.fromkeys(FindingSeverity, 0)
        for finding in findings:
            counts[finding.severity] += 1
        return cls(
            total=len(findings),
            error=counts[FindingSeverity.ERROR],
            warning=counts[FindingSeverity.WARNING],
            info=counts[FindingSeverity.INFO],
        )


# --- the full validation payload -------------------------------------


class InvoiceValidation(BaseModel):
    """The schema-constrained result of one validation attempt.

    Identity-free, mirroring
    :class:`app.schemas.normalization.NormalizedInvoice`: the link to the source
    normalization attempt is on :class:`ValidatedInvoiceResult`, and the
    persistence identity (validation id, attempt number, status, timestamps,
    technical-failure fields) is added by the step 6 model and step 12 schemas.

    ``findings`` may be empty (no rule fired). ``summary`` is re-derived from
    ``findings`` and cross-checked here, so it can never carry information the
    finding list does not.
    """

    model_config = ConfigDict(extra="forbid")

    findings: list[ValidationFinding]
    summary: ValidationSummary

    @model_validator(mode="after")
    def _check_summary_matches_findings(self) -> "InvoiceValidation":
        if self.summary != ValidationSummary.from_findings(self.findings):
            raise ValueError("summary does not match findings")
        return self

    @classmethod
    def from_findings(cls, findings: list[ValidationFinding]) -> "InvoiceValidation":
        """Build a payload with the summary tallied from ``findings``."""
        materialized = list(findings)
        return cls(
            findings=materialized,
            summary=ValidationSummary.from_findings(materialized),
        )


class ValidatedInvoiceResult(BaseModel):
    """An :class:`InvoiceValidation` bound to the normalization attempt it came from.

    Step 3 keeps this minimal, mirroring
    :class:`app.schemas.normalization.NormalizedInvoiceResult`: it preserves the
    reference to the source normalization attempt and nothing else. It expresses
    no verdict - "validated" here means only "run through Stage 5", exactly as
    "normalized" means "run through Stage 4".
    """

    model_config = ConfigDict(extra="forbid")

    source_normalization_id: uuid.UUID
    validation: InvoiceValidation


# Derived name tuples so downstream code (step 4 catalogue, step 6 persistence,
# step 12 schemas) cannot silently drift out of step with this contract.
VALIDATION_RULE_CODES: tuple[str, ...] = tuple(rule.value for rule in ValidationRule)
VALIDATION_FINDING_FIELD_NAMES: tuple[str, ...] = tuple(ValidationFinding.model_fields)
