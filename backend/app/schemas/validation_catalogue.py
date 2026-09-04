"""Formal Stage 5 rule catalogue (step 4).

Turns the prose table in ``docs/stage-5-validation.md`` §3 into a structured,
per-rule specification: for every :class:`~app.schemas.validation.ValidationRule`
member, what the rule reads, when it does **not** run, how severe its finding
is, and the fixed client-safe sentence the finding carries.

This module is pure data. It calls no AI provider, makes no network call, and
touches no database. It also holds **no ⚠ policy value** - no threshold, no
tolerance, no date window, no confidence cut-off, and not even the required or
critical field list. Those judgement-call numbers stay in
``docs/stage-5-validation.md`` until the vendored ``policy`` module is added in
step 8; the deterministic rule functions (step 8) combine *this* catalogue with
*that* policy. What lives here is the part of §3 that is not a tunable number:
each rule's identity, its inputs, its skip conditions, its severity shape, its
finding's ``field_path`` shape and ``context`` keys, and its message text.

The catalogue is closed and exhaustive: :data:`RULE_CATALOGUE` has exactly one
:class:`RuleSpec` per ``ValidationRule`` member, in declaration order, checked at
import time so the two cannot drift.

Nothing here is executed against an invoice - :class:`RuleSpec.field_path` is a
*shape descriptor* (``None`` for an invoice-level finding, a literal Stage 4
scalar name, or an angle-bracket template such as ``"<critical field>"`` or
``"line_items.<i>.line_total"``). Step 8 resolves a template to a concrete
Stage 4 path per finding; the concrete path is what the
:class:`~app.schemas.validation.ValidationFinding` contract validates.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from app.schemas.normalization import NORMALIZED_SCALAR_FIELD_NAMES
from app.schemas.validation import FindingSeverity, ValidationRule

__all__ = [
    "RuleSpec",
    "RULE_SPECS",
    "RULE_CATALOGUE",
    "RULE_MESSAGES",
    "INPUT_TOKENS",
    "FIELD_PATH_TEMPLATES",
    "spec_for",
]


# --- vocabulary shared by the specs ------------------------------------

# Non-fieldname tokens a spec may list in ``inputs``. A token that is not one of
# these must be a Stage 4 canonical scalar name. These name *what* a rule reads,
# never a policy value: ``required_field_policy`` means "the §2.1 set", it does
# not embed the set.
INPUT_TOKENS: Final[frozenset[str]] = frozenset(
    {
        "normalized_scalars",  # every canonical scalar value on the invoice
        "normalized_line_items",  # the ordered normalized line items
        "normalization_errors",  # the Stage 4 field-error list, re-surfaced
        "extraction_confidence",  # per-field <field>_confidence from the source extraction
        "duplicate_candidates",  # other documents' latest COMPLETED normalizations (§2.5)
        "run_date",  # the attempt's own started_at, as a UTC date (§2.8)
        "required_field_policy",  # the §2.1 required/critical field set
        "high_value_policy",  # the §2.7 per-currency threshold map
        "reconciliation_tolerance",  # the §2.3/§2.4 numeric tolerance
        "confidence_threshold",  # the §2.6 critical-field minimum
        "date_window_policy",  # the §2.2 due-date / age windows
    }
)

# Angle-bracket ``field_path`` shapes that step 8 resolves per finding. A spec's
# ``field_path`` is ``None``, a literal scalar name, or one of these.
FIELD_PATH_TEMPLATES: Final[frozenset[str]] = frozenset(
    {
        "<required field>",  # the specific required scalar that is missing
        "<errored field>",  # the specific field a Stage 4 error is attached to
        "<critical field>",  # the specific critical scalar under confidence review
        "line_items.<i>.line_total",  # the offending line item, by its zero-based index
    }
)


@dataclass(frozen=True, slots=True)
class RuleSpec:
    """The formalised §3 entry for one validation rule.

    ``severity`` is ``None`` exactly when the rule's severity depends on the
    invoice (only :attr:`ValidationRule.NORMALIZATION_ERROR`); ``severity_note``
    then explains the branch and is ``None`` otherwise. ``skip_when`` describes
    when a rule is not evaluated; an empty tuple means it always evaluates, not
    that it necessarily emits a finding. ``emits_expected_actual`` says whether
    the finding fills the contract's ``expected`` / ``actual`` pair.
    ``context_keys`` lists the keys the finding's ``context`` object carries
    (order is presentation order, not significance).
    """

    rule: ValidationRule
    title: str
    description: str
    inputs: tuple[str, ...]
    field_path: str | None
    severity: FindingSeverity | None
    severity_note: str | None
    skip_when: tuple[str, ...]
    emits_expected_actual: bool
    context_keys: tuple[str, ...]
    message: str

    @property
    def is_invoice_level(self) -> bool:
        """The finding has no field anchor (``field_path`` is ``null``)."""
        return self.field_path is None

    @property
    def has_conditional_severity(self) -> bool:
        """Severity is decided per invoice rather than fixed by this spec."""
        return self.severity is None


# --- the catalogue, in ValidationRule / §3 order ----------------------

RULE_SPECS: Final[tuple[RuleSpec, ...]] = (
    RuleSpec(
        rule=ValidationRule.MISSING_REQUIRED_FIELD,
        title="Required field missing",
        description=(
            "A field the business needs to process the invoice at all has a "
            "null normalized value, whether it was absent in the source or "
            "Stage 4 could not normalize it."
        ),
        inputs=("normalized_scalars", "required_field_policy"),
        field_path="<required field>",
        severity=FindingSeverity.ERROR,
        severity_note=None,
        skip_when=(),
        emits_expected_actual=False,
        context_keys=(),
        message="A field required to process this invoice is missing.",
    ),
    RuleSpec(
        rule=ValidationRule.NORMALIZATION_ERROR,
        title="Normalization error present",
        description=(
            "Stage 4 recorded a field-level normalization error; Stage 5 "
            "re-surfaces it as a finding without re-evaluating the raw value."
        ),
        inputs=("normalization_errors", "required_field_policy"),
        field_path="<errored field>",
        severity=None,
        severity_note=(
            "error when the errored field is in the required set (§2.1), "
            "otherwise warning."
        ),
        skip_when=(),
        emits_expected_actual=False,
        context_keys=("code",),
        message="A field on this invoice could not be normalized to a valid value.",
    ),
    RuleSpec(
        rule=ValidationRule.DUE_DATE_BEFORE_INVOICE_DATE,
        title="Due date before invoice date",
        description="The due date falls earlier than the invoice date.",
        inputs=("invoice_date", "due_date"),
        field_path="due_date",
        severity=FindingSeverity.WARNING,
        severity_note=None,
        skip_when=("invoice_date is null", "due_date is null"),
        emits_expected_actual=True,
        context_keys=(),
        message="The due date is earlier than the invoice date.",
    ),
    RuleSpec(
        rule=ValidationRule.DUE_DATE_FAR_AFTER_INVOICE_DATE,
        title="Due date far after invoice date",
        description=(
            "The gap from invoice date to due date exceeds the plausible "
            "payment-term window."
        ),
        inputs=("invoice_date", "due_date", "date_window_policy"),
        field_path="due_date",
        severity=FindingSeverity.WARNING,
        severity_note=None,
        skip_when=("invoice_date is null", "due_date is null"),
        emits_expected_actual=True,
        context_keys=("max_gap_days",),
        message="The due date is much further after the invoice date than expected.",
    ),
    RuleSpec(
        rule=ValidationRule.INVOICE_DATE_IN_FUTURE,
        title="Invoice date in the future",
        description=(
            "The invoice date is later than the date the validation attempt "
            "ran (§2.8)."
        ),
        inputs=("invoice_date", "run_date"),
        field_path="invoice_date",
        severity=FindingSeverity.WARNING,
        severity_note=None,
        skip_when=("invoice_date is null",),
        emits_expected_actual=True,
        context_keys=("run_date",),
        message="The invoice date is in the future.",
    ),
    RuleSpec(
        rule=ValidationRule.INVOICE_DATE_IMPLAUSIBLY_OLD,
        title="Invoice date implausibly old",
        description=(
            "The invoice date is older than the plausible-age window measured "
            "back from the run date (§2.8)."
        ),
        inputs=("invoice_date", "run_date", "date_window_policy"),
        field_path="invoice_date",
        severity=FindingSeverity.WARNING,
        severity_note=None,
        skip_when=("invoice_date is null",),
        emits_expected_actual=True,
        context_keys=("run_date", "max_age_years"),
        message="The invoice date is implausibly far in the past.",
    ),
    RuleSpec(
        rule=ValidationRule.TOTALS_DO_NOT_RECONCILE,
        title="Subtotal plus tax does not equal total",
        description=(
            "The subtotal and tax amount do not add up to the invoice total "
            "within tolerance."
        ),
        inputs=(
            "subtotal",
            "tax_amount",
            "total_amount",
            "reconciliation_tolerance",
        ),
        field_path=None,
        severity=FindingSeverity.WARNING,
        severity_note=None,
        skip_when=(
            "subtotal is null",
            "tax_amount is null",
            "total_amount is null",
        ),
        emits_expected_actual=True,
        context_keys=("delta", "tolerance"),
        message="The subtotal plus tax does not equal the invoice total.",
    ),
    RuleSpec(
        rule=ValidationRule.LINE_ITEM_AMOUNT_MISMATCH,
        title="Line item quantity times unit price does not equal line total",
        description=(
            "For a line item, quantity multiplied by unit price does not equal "
            "the stated line total within tolerance."
        ),
        inputs=("normalized_line_items", "reconciliation_tolerance"),
        field_path="line_items.<i>.line_total",
        severity=FindingSeverity.WARNING,
        severity_note=None,
        skip_when=(
            "the line item's quantity is null",
            "the line item's unit_price is null",
            "the line item's line_total is null",
        ),
        emits_expected_actual=True,
        context_keys=("line_index", "delta", "tolerance"),
        message=(
            "A line item's quantity times unit price does not equal its line total."
        ),
    ),
    RuleSpec(
        rule=ValidationRule.LINE_ITEMS_DO_NOT_SUM,
        title="Sum of line totals does not reconcile",
        description=(
            "The line totals do not add up to the reconciliation target "
            "(subtotal, else total minus tax, else total) within the "
            "per-line tolerance."
        ),
        inputs=(
            "normalized_line_items",
            "subtotal",
            "tax_amount",
            "total_amount",
            "reconciliation_tolerance",
        ),
        field_path=None,
        severity=FindingSeverity.WARNING,
        severity_note=None,
        skip_when=(
            "there are no line items",
            "any line item has a null line_total",
            "no reconciliation target is available (subtotal and total_amount both null)",
        ),
        emits_expected_actual=True,
        context_keys=("target_basis", "line_count", "sum", "delta", "tolerance"),
        message="The line item totals do not add up to the reconciliation target.",
    ),
    RuleSpec(
        rule=ValidationRule.LINE_ITEM_SUM_NOT_CHECKED,
        title="Line total sum check skipped",
        description=(
            "Line items are present but at least one lacks a line total, so "
            "the sum check could not run; recorded so the decision stage knows "
            "the check was skipped, not passed."
        ),
        inputs=("normalized_line_items",),
        field_path=None,
        severity=FindingSeverity.INFO,
        severity_note=None,
        skip_when=(
            "there are no line items",
            "every line item has a line_total",
        ),
        emits_expected_actual=False,
        context_keys=("line_count", "missing_line_total_count"),
        message=(
            "The line item totals were not checked because at least one line "
            "total is missing."
        ),
    ),
    RuleSpec(
        rule=ValidationRule.LOW_CONFIDENCE_CRITICAL_FIELD,
        title="Critical field below its confidence threshold",
        description=(
            "A critical field has a value, and its per-field extraction "
            "confidence is below the critical-field minimum."
        ),
        inputs=(
            "normalized_scalars",
            "extraction_confidence",
            "required_field_policy",
            "confidence_threshold",
        ),
        field_path="<critical field>",
        severity=FindingSeverity.WARNING,
        severity_note=None,
        skip_when=(
            "the field's value is null",
            "the field's extraction confidence is null",
        ),
        emits_expected_actual=False,
        context_keys=("confidence", "threshold"),
        message="A critical field was extracted with low confidence.",
    ),
    RuleSpec(
        rule=ValidationRule.CRITICAL_FIELD_CONFIDENCE_UNAVAILABLE,
        title="Critical field confidence unavailable",
        description=(
            "A critical field has a value but the provider reported no "
            "confidence for it, so the confidence check could not be made. "
            "A null confidence is never treated as high or low (§2.6)."
        ),
        inputs=(
            "normalized_scalars",
            "extraction_confidence",
            "required_field_policy",
        ),
        field_path="<critical field>",
        severity=FindingSeverity.INFO,
        severity_note=None,
        skip_when=(
            "the field's value is null",
            "the field's extraction confidence is present",
        ),
        emits_expected_actual=False,
        context_keys=(),
        message="Extraction confidence is not available for a critical field.",
    ),
    RuleSpec(
        rule=ValidationRule.PROBABLE_DUPLICATE_INVOICE,
        title="Probable duplicate",
        description=(
            "Another document's latest completed normalization matches this "
            "invoice on vendor identity, invoice number, currency, and total "
            "(within tolerance); exact-match only, no fuzzy matching (§2.5)."
        ),
        inputs=(
            "vendor_tax_id",
            "vendor_name",
            "invoice_number",
            "currency",
            "total_amount",
            "duplicate_candidates",
            "reconciliation_tolerance",
        ),
        field_path=None,
        severity=FindingSeverity.WARNING,
        severity_note=None,
        skip_when=(
            "invoice_number is null",
            "both vendor_tax_id and vendor_name are null",
            "currency is null",
            "total_amount is null",
            "no other document matches the duplicate key",
        ),
        emits_expected_actual=False,
        context_keys=("matches", "tolerance"),
        message="This invoice appears to duplicate another invoice already in the system.",
    ),
    RuleSpec(
        rule=ValidationRule.HIGH_VALUE_INVOICE,
        title="High-value invoice detected",
        description=(
            "The absolute invoice total meets or exceeds the high-value "
            "threshold for its currency; a business-attention flag, not a "
            "data defect."
        ),
        inputs=("total_amount", "currency", "high_value_policy"),
        field_path=None,
        severity=FindingSeverity.INFO,
        severity_note=None,
        skip_when=("total_amount is null", "currency is null"),
        emits_expected_actual=False,
        context_keys=("threshold", "currency"),
        message="The invoice total meets or exceeds the high-value threshold.",
    ),
    RuleSpec(
        rule=ValidationRule.NO_LINE_ITEMS,
        title="No line items",
        description=(
            "The invoice carries no line items; recorded as a fact for the "
            "decision stage, not treated as a defect."
        ),
        inputs=("normalized_line_items",),
        field_path=None,
        severity=FindingSeverity.INFO,
        severity_note=None,
        skip_when=("the invoice has at least one line item",),
        emits_expected_actual=False,
        context_keys=(),
        message="The invoice has no line items.",
    ),
)


# --- derived views and lookup ----------------------------------------

RULE_CATALOGUE: Final[Mapping[ValidationRule, RuleSpec]] = MappingProxyType(
    {spec.rule: spec for spec in RULE_SPECS}
)

RULE_MESSAGES: Final[Mapping[ValidationRule, str]] = MappingProxyType(
    {spec.rule: spec.message for spec in RULE_SPECS}
)


def spec_for(rule: ValidationRule) -> RuleSpec:
    """Return the :class:`RuleSpec` for ``rule`` (always present)."""
    return RULE_CATALOGUE[rule]


# --- import-time coverage / shape guard ------------------------------

_VALID_FIELD_PATHS: Final[frozenset[str]] = (
    frozenset(NORMALIZED_SCALAR_FIELD_NAMES) | FIELD_PATH_TEMPLATES
)


def _validate_catalogue() -> None:
    """Fail the import if the catalogue drifts from the rule contract."""
    if tuple(spec.rule for spec in RULE_SPECS) != tuple(ValidationRule):
        raise RuntimeError(
            "RULE_SPECS must list every ValidationRule member exactly once, in "
            "declaration order"
        )
    for spec in RULE_SPECS:
        where = f"rule {spec.rule.value!r}"
        if (spec.severity is None) != (spec.severity_note is not None):
            raise RuntimeError(
                f"{where}: severity_note must be set exactly when severity is "
                "conditional (None)"
            )
        if not spec.message.strip() or "\n" in spec.message:
            raise RuntimeError(f"{where}: message must be a non-blank single line")
        if not spec.inputs:
            raise RuntimeError(f"{where}: inputs must not be empty")
        for token in spec.inputs:
            if token not in INPUT_TOKENS and token not in NORMALIZED_SCALAR_FIELD_NAMES:
                raise RuntimeError(f"{where}: unknown input token {token!r}")
        if spec.field_path is not None and spec.field_path not in _VALID_FIELD_PATHS:
            raise RuntimeError(f"{where}: unknown field_path shape {spec.field_path!r}")
        if len(set(spec.context_keys)) != len(spec.context_keys):
            raise RuntimeError(f"{where}: context_keys must be unique")


_validate_catalogue()
