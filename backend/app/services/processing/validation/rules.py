"""Deterministic Stage 5 validation rule functions (step 8).

One pure function per :class:`~app.schemas.validation.ValidationRule` member.
Each takes a :class:`RuleContext` - the Stage 4 normalized invoice plus the
per-critical-field extraction confidence, the duplicate candidates, and the run
date - and returns zero or more
:class:`~app.schemas.validation.ValidationFinding` objects.

* **Pure and deterministic.** No database, no AI, no network, no wall clock -
  the only "as-of" input is :attr:`RuleContext.run_date` (spec §2.8). The same
  context yields the same findings every time.
* **``Decimal`` only.** Every sum, product, and difference is computed in a wide
  :class:`~decimal.Context` so no operand or intermediate value is rounded
  (spec §2.9); no binary float is produced or accepted.
* **No duplication of pinned facts.** Each finding's ``message`` and default
  ``severity`` come from the step 4 catalogue
  (:mod:`app.schemas.validation_catalogue`); every ⚠ threshold comes from
  :mod:`.policy`.

The engine (step 9) loads the inputs, builds the :class:`RuleContext`, calls
:func:`run_rules`, and assembles / re-validates the ``InvoiceValidation``; these
functions do none of that.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Context, Decimal, localcontext
from types import MappingProxyType
from typing import Final

from app.schemas.normalization import NormalizedInvoice
from app.schemas.validation import FindingSeverity, ValidationFinding, ValidationRule
from app.schemas.validation_catalogue import spec_for
from app.services.processing.validation import policy

__all__ = [
    "DuplicateCandidate",
    "RuleContext",
    "RuleFunction",
    "RULE_FUNCTIONS",
    "run_rules",
]

def _arithmetic_context(*values: Decimal) -> Context:
    """Return an input-sized context that cannot round these finite operands.

    Stage 4 permits arbitrary finite ``Decimal`` values. Counting coefficient
    digits and exponent displacement gives ample precision for the additions,
    subtractions, products, and accumulated sums used by this module, including
    a carry digit per operand. This avoids a fixed precision silently changing
    an otherwise contract-valid large value.
    """
    precision = 10 + sum(
        len(value.as_tuple().digits) + abs(value.as_tuple().exponent) + 1
        for value in values
    )
    return Context(prec=max(precision, 10))


@dataclass(frozen=True, slots=True)
class DuplicateCandidate:
    """One other document's latest ``COMPLETED`` normalization, for §2.5 matching.

    The engine (step 9) builds these from the candidate query; the rule only
    compares fields. ``document_id`` and ``normalization_id`` identify the match
    in the finding's ``context``.
    """

    document_id: uuid.UUID
    normalization_id: uuid.UUID
    vendor_tax_id: str | None
    vendor_name: str | None
    invoice_number: str | None
    currency: str | None
    total_amount: Decimal | None


@dataclass(frozen=True, slots=True)
class RuleContext:
    """Everything a rule may read. Assembled by the engine, never by a rule."""

    invoice: NormalizedInvoice
    # The validation attempt's own start date, as a UTC calendar date (spec
    # §2.8) - the single "as-of" input.
    run_date: date
    # Per critical field (§2.1 / §2.6): a Decimal in [0, 1], or ``None`` when the
    # provider gave no calibrated confidence. A missing key is treated as
    # ``None`` (unavailable).
    confidence: Mapping[str, Decimal | None] = field(default_factory=dict)
    # Other documents' latest completed normalizations (§2.5). Empty by default.
    duplicate_candidates: Sequence[DuplicateCandidate] = ()


RuleFunction = Callable[[RuleContext], list[ValidationFinding]]


# --- shared helpers ---------------------------------------------------


def _finding(
    rule: ValidationRule,
    *,
    field_path: str | None = None,
    expected: str | Decimal | None = None,
    actual: str | Decimal | None = None,
    context: dict[str, object] | None = None,
    severity: FindingSeverity | None = None,
) -> ValidationFinding:
    """Build one finding, taking ``message`` and default ``severity`` from the catalogue."""
    spec = spec_for(rule)
    resolved = severity if severity is not None else spec.severity
    if resolved is None:  # only ``normalization_error``; its caller passes severity
        raise ValueError(f"{rule.value}: severity must be supplied for this rule")
    return ValidationFinding(
        rule=rule,
        severity=resolved,
        field_path=field_path,
        expected=expected,
        actual=actual,
        message=spec.message,
        context=dict(context) if context else {},
    )


def _is_required(field_path: str) -> bool:
    """True when ``field_path`` names (or is under) a §2.1 required scalar field."""
    return field_path.split(".", 1)[0] in policy.REQUIRED_FIELDS


def _present_critical_fields(inv: NormalizedInvoice) -> list[str]:
    return [name for name in policy.CRITICAL_FIELDS if getattr(inv, name) is not None]


# --- the rule functions, in ValidationRule / §3 order ----------------


def check_missing_required_field(ctx: RuleContext) -> list[ValidationFinding]:
    """§2.1 - one `error` finding per required field whose normalized value is null."""
    inv = ctx.invoice
    return [
        _finding(ValidationRule.MISSING_REQUIRED_FIELD, field_path=name)
        for name in policy.REQUIRED_FIELDS
        if getattr(inv, name) is None
    ]


def check_normalization_error(ctx: RuleContext) -> list[ValidationFinding]:
    """§3 - re-surface each Stage 4 field error, `error` on a required field else `warning`."""
    out: list[ValidationFinding] = []
    for err in ctx.invoice.errors:
        severity = (
            FindingSeverity.ERROR
            if _is_required(err.field_path)
            else FindingSeverity.WARNING
        )
        out.append(
            _finding(
                ValidationRule.NORMALIZATION_ERROR,
                field_path=err.field_path,
                severity=severity,
                context={"code": err.code.value},
            )
        )
    return out


def check_due_date_before_invoice_date(ctx: RuleContext) -> list[ValidationFinding]:
    """§2.2 - `warning` when both dates are present and due date precedes invoice date."""
    inv = ctx.invoice
    if inv.invoice_date is None or inv.due_date is None:
        return []
    if date.fromisoformat(inv.due_date) < date.fromisoformat(inv.invoice_date):
        return [
            _finding(
                ValidationRule.DUE_DATE_BEFORE_INVOICE_DATE,
                field_path="due_date",
                expected=inv.invoice_date,
                actual=inv.due_date,
            )
        ]
    return []


def check_due_date_far_after_invoice_date(ctx: RuleContext) -> list[ValidationFinding]:
    """§2.2 - `warning` when the due date is more than the gap window after the invoice date."""
    inv = ctx.invoice
    if inv.invoice_date is None or inv.due_date is None:
        return []
    invoice_date = date.fromisoformat(inv.invoice_date)
    due_date = date.fromisoformat(inv.due_date)
    # Compare the gap rather than adding to ``invoice_date``: addition can
    # overflow for valid ISO dates near year 9999.
    if (due_date - invoice_date).days > policy.DUE_DATE_MAX_GAP_DAYS:
        return [
            _finding(
                ValidationRule.DUE_DATE_FAR_AFTER_INVOICE_DATE,
                field_path="due_date",
                expected=inv.invoice_date,
                actual=inv.due_date,
                context={"max_gap_days": policy.DUE_DATE_MAX_GAP_DAYS},
            )
        ]
    return []


def check_invoice_date_in_future(ctx: RuleContext) -> list[ValidationFinding]:
    """§2.2 / §2.8 - `warning` when the invoice date is after the run date."""
    inv = ctx.invoice
    if inv.invoice_date is None:
        return []
    if date.fromisoformat(inv.invoice_date) > ctx.run_date:
        return [
            _finding(
                ValidationRule.INVOICE_DATE_IN_FUTURE,
                field_path="invoice_date",
                expected=ctx.run_date.isoformat(),
                actual=inv.invoice_date,
                context={"run_date": ctx.run_date.isoformat()},
            )
        ]
    return []


def check_invoice_date_implausibly_old(ctx: RuleContext) -> list[ValidationFinding]:
    """§2.2 / §2.8 - `warning` when the invoice date is before the plausible-age window."""
    inv = ctx.invoice
    if inv.invoice_date is None:
        return []
    earliest = policy.earliest_plausible_invoice_date(ctx.run_date)
    if date.fromisoformat(inv.invoice_date) < earliest:
        return [
            _finding(
                ValidationRule.INVOICE_DATE_IMPLAUSIBLY_OLD,
                field_path="invoice_date",
                expected=earliest.isoformat(),
                actual=inv.invoice_date,
                context={
                    "run_date": ctx.run_date.isoformat(),
                    "max_age_years": policy.INVOICE_DATE_MAX_AGE_YEARS,
                },
            )
        ]
    return []


def check_totals_do_not_reconcile(ctx: RuleContext) -> list[ValidationFinding]:
    """§2.3 - `warning` when ``subtotal + tax_amount`` is not ``total_amount`` within tolerance."""
    inv = ctx.invoice
    if inv.subtotal is None or inv.tax_amount is None or inv.total_amount is None:
        return []
    with localcontext(
        _arithmetic_context(inv.subtotal, inv.tax_amount, inv.total_amount)
    ):
        expected = inv.subtotal + inv.tax_amount
        delta = expected - inv.total_amount
    if delta.copy_abs() <= policy.RECONCILIATION_TOLERANCE:
        return []
    return [
        _finding(
            ValidationRule.TOTALS_DO_NOT_RECONCILE,
            field_path=None,
            expected=expected,
            actual=inv.total_amount,
            context={"delta": delta, "tolerance": policy.RECONCILIATION_TOLERANCE},
        )
    ]


def check_line_item_amount_mismatch(ctx: RuleContext) -> list[ValidationFinding]:
    """§2.4 - per line item, `warning` when ``quantity × unit_price`` is not ``line_total``."""
    out: list[ValidationFinding] = []
    for index, item in enumerate(ctx.invoice.line_items):
        if item.quantity is None or item.unit_price is None or item.line_total is None:
            continue
        with localcontext(
            _arithmetic_context(item.quantity, item.unit_price, item.line_total)
        ):
            expected = item.quantity * item.unit_price
            delta = expected - item.line_total
        if delta.copy_abs() <= policy.RECONCILIATION_TOLERANCE:
            continue
        out.append(
            _finding(
                ValidationRule.LINE_ITEM_AMOUNT_MISMATCH,
                field_path=f"line_items.{index}.line_total",
                expected=expected,
                actual=item.line_total,
                context={
                    "line_index": index,
                    "delta": delta,
                    "tolerance": policy.RECONCILIATION_TOLERANCE,
                },
            )
        )
    return out


def _line_sum_target(inv: NormalizedInvoice) -> tuple[Decimal, str] | None:
    """The §2.4 reconciliation target and its ``target_basis`` label, or ``None``."""
    if inv.subtotal is not None:
        return inv.subtotal, "subtotal"
    if inv.total_amount is not None and inv.tax_amount is not None:
        with localcontext(_arithmetic_context(inv.total_amount, inv.tax_amount)):
            return inv.total_amount - inv.tax_amount, "total_less_tax"
    if inv.total_amount is not None:
        return inv.total_amount, "total"
    return None


def check_line_items_do_not_sum(ctx: RuleContext) -> list[ValidationFinding]:
    """§2.4 - `warning` when the line totals do not add up to the target within tolerance."""
    items = ctx.invoice.line_items
    if not items or any(item.line_total is None for item in items):
        return []
    target = _line_sum_target(ctx.invoice)
    if target is None:
        return []
    target_value, basis = target
    line_totals = [item.line_total for item in items]
    with localcontext(_arithmetic_context(target_value, *line_totals)):
        total = Decimal(0)
        for item in items:
            total += item.line_total  # every line_total is non-null here
        delta = total - target_value
    tolerance = policy.line_sum_tolerance(len(items))
    if delta.copy_abs() <= tolerance:
        return []
    return [
        _finding(
            ValidationRule.LINE_ITEMS_DO_NOT_SUM,
            field_path=None,
            expected=target_value,
            actual=total,
            context={
                "target_basis": basis,
                "line_count": len(items),
                "sum": total,
                "delta": delta,
                "tolerance": tolerance,
            },
        )
    ]


def check_line_item_sum_not_checked(ctx: RuleContext) -> list[ValidationFinding]:
    """§2.4 - `info` when line items exist but at least one has no ``line_total``."""
    items = ctx.invoice.line_items
    if not items:
        return []
    missing = sum(1 for item in items if item.line_total is None)
    if missing == 0:
        return []
    return [
        _finding(
            ValidationRule.LINE_ITEM_SUM_NOT_CHECKED,
            field_path=None,
            context={
                "line_count": len(items),
                "missing_line_total_count": missing,
            },
        )
    ]


def check_low_confidence_critical_field(ctx: RuleContext) -> list[ValidationFinding]:
    """§2.6 - `warning` per critical field whose value is present and confidence < the minimum."""
    out: list[ValidationFinding] = []
    for name in _present_critical_fields(ctx.invoice):
        confidence = ctx.confidence.get(name)
        if confidence is None:
            continue
        if confidence < policy.CRITICAL_FIELD_CONFIDENCE_MIN:
            out.append(
                _finding(
                    ValidationRule.LOW_CONFIDENCE_CRITICAL_FIELD,
                    field_path=name,
                    context={
                        "confidence": confidence,
                        "threshold": policy.CRITICAL_FIELD_CONFIDENCE_MIN,
                    },
                )
            )
    return out


def check_critical_field_confidence_unavailable(
    ctx: RuleContext,
) -> list[ValidationFinding]:
    """§2.6 - `info` per critical field whose value is present but confidence is ``None``."""
    return [
        _finding(
            ValidationRule.CRITICAL_FIELD_CONFIDENCE_UNAVAILABLE,
            field_path=name,
        )
        for name in _present_critical_fields(ctx.invoice)
        if ctx.confidence.get(name) is None
    ]


def check_probable_duplicate_invoice(ctx: RuleContext) -> list[ValidationFinding]:
    """§2.5 - one `warning` when >=1 other document matches the exact-match duplicate key."""
    inv = ctx.invoice
    if (
        inv.invoice_number is None
        or (inv.vendor_tax_id is None and inv.vendor_name is None)
        or inv.currency is None
        or inv.total_amount is None
    ):
        return []

    matches: list[dict[str, str]] = []
    for cand in ctx.duplicate_candidates:
        if inv.vendor_tax_id is not None and cand.vendor_tax_id is not None:
            vendor_ok = inv.vendor_tax_id == cand.vendor_tax_id
        elif inv.vendor_name is not None and cand.vendor_name is not None:
            vendor_ok = inv.vendor_name == cand.vendor_name
        else:
            vendor_ok = False
        if not vendor_ok:
            continue
        if cand.invoice_number != inv.invoice_number:
            continue
        if cand.currency != inv.currency:
            continue
        if cand.total_amount is None:
            continue
        with localcontext(_arithmetic_context(inv.total_amount, cand.total_amount)):
            total_delta = inv.total_amount - cand.total_amount
        if total_delta.copy_abs() > policy.RECONCILIATION_TOLERANCE:
            continue
        matches.append(
            {
                "document_id": str(cand.document_id),
                "normalization_id": str(cand.normalization_id),
            }
        )

    if not matches:
        return []
    matches.sort(key=lambda m: (m["document_id"], m["normalization_id"]))
    return [
        _finding(
            ValidationRule.PROBABLE_DUPLICATE_INVOICE,
            field_path=None,
            context={
                "matches": matches,
                "tolerance": policy.RECONCILIATION_TOLERANCE,
            },
        )
    ]


def check_high_value_invoice(ctx: RuleContext) -> list[ValidationFinding]:
    """§2.7 - `info` when ``abs(total_amount)`` is at or above the currency's threshold."""
    inv = ctx.invoice
    if inv.total_amount is None or inv.currency is None:
        return []
    threshold = policy.high_value_threshold(inv.currency)
    magnitude = inv.total_amount.copy_abs()
    if magnitude < threshold:
        return []
    return [
        _finding(
            ValidationRule.HIGH_VALUE_INVOICE,
            field_path=None,
            context={"threshold": threshold, "currency": inv.currency},
        )
    ]


def check_no_line_items(ctx: RuleContext) -> list[ValidationFinding]:
    """§3 - `info` when the invoice carries no line items."""
    if ctx.invoice.line_items:
        return []
    return [_finding(ValidationRule.NO_LINE_ITEMS, field_path=None)]


# --- registry + ordered evaluation --------------------------------

RULE_FUNCTIONS: Final[Mapping[ValidationRule, RuleFunction]] = MappingProxyType({
    ValidationRule.MISSING_REQUIRED_FIELD: check_missing_required_field,
    ValidationRule.NORMALIZATION_ERROR: check_normalization_error,
    ValidationRule.DUE_DATE_BEFORE_INVOICE_DATE: check_due_date_before_invoice_date,
    ValidationRule.DUE_DATE_FAR_AFTER_INVOICE_DATE: check_due_date_far_after_invoice_date,
    ValidationRule.INVOICE_DATE_IN_FUTURE: check_invoice_date_in_future,
    ValidationRule.INVOICE_DATE_IMPLAUSIBLY_OLD: check_invoice_date_implausibly_old,
    ValidationRule.TOTALS_DO_NOT_RECONCILE: check_totals_do_not_reconcile,
    ValidationRule.LINE_ITEM_AMOUNT_MISMATCH: check_line_item_amount_mismatch,
    ValidationRule.LINE_ITEMS_DO_NOT_SUM: check_line_items_do_not_sum,
    ValidationRule.LINE_ITEM_SUM_NOT_CHECKED: check_line_item_sum_not_checked,
    ValidationRule.LOW_CONFIDENCE_CRITICAL_FIELD: check_low_confidence_critical_field,
    ValidationRule.CRITICAL_FIELD_CONFIDENCE_UNAVAILABLE: (
        check_critical_field_confidence_unavailable
    ),
    ValidationRule.PROBABLE_DUPLICATE_INVOICE: check_probable_duplicate_invoice,
    ValidationRule.HIGH_VALUE_INVOICE: check_high_value_invoice,
    ValidationRule.NO_LINE_ITEMS: check_no_line_items,
})

if tuple(RULE_FUNCTIONS) != tuple(ValidationRule):
    raise RuntimeError(
        "RULE_FUNCTIONS must map every ValidationRule member exactly once, in "
        "declaration order"
    )


def run_rules(ctx: RuleContext) -> list[ValidationFinding]:
    """Run every rule in catalogue order and concatenate the findings.

    Deterministic: identical ``ctx`` -> identical list. The engine (step 9) wraps
    this with input loading and ``InvoiceValidation`` assembly; the ordering here
    is the persisted finding order (step 6 ``position``).
    """
    findings: list[ValidationFinding] = []
    for rule in ValidationRule:
        findings.extend(RULE_FUNCTIONS[rule](ctx))
    return findings
