"""Tests for the deterministic Stage 5 rule functions (step 8).

No database and no AI provider - these exercise the pure functions in
``app/services/processing/validation/rules.py`` against hand-built
``RuleContext`` objects, including the tolerance edges of every ⚠ threshold.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.schemas.validation import (
    InvoiceValidation,
    ValidationFinding,
    ValidationRule,
)
from app.services.processing.validation import policy
from app.services.processing.validation import rules as rules_mod
from app.services.processing.validation.rules import (
    RULE_FUNCTIONS,
    DuplicateCandidate,
    RuleContext,
    run_rules,
)

RUN_DATE = date(2026, 6, 1)

_DEFAULT_INVOICE: dict = {
    "invoice_number": "INV-1",
    "invoice_date": "2026-01-15",
    "due_date": "2026-02-14",
    "vendor_name": "Acme GmbH",
    "vendor_tax_id": "DE123456789",
    "customer_name": "Buyer Ltd",
    "currency": "EUR",
    "subtotal": Decimal("100.00"),
    "tax_amount": Decimal("19.00"),
    "total_amount": Decimal("119.00"),
    "line_items": [],
    "errors": [],
}


def _invoice(**overrides):
    from app.schemas.normalization import NormalizedInvoice

    data = {**_DEFAULT_INVOICE, **overrides}
    return NormalizedInvoice.model_validate(data)


def _li(**overrides) -> dict:
    item = {"description": "Item", "quantity": None, "unit_price": None, "line_total": None}
    item.update(overrides)
    return item


def _full_confidence() -> dict[str, Decimal]:
    return {name: Decimal("0.99") for name in policy.CRITICAL_FIELDS}


def _ctx(invoice=None, *, run_date: date = RUN_DATE, confidence=None, candidates=()):
    return RuleContext(
        invoice=invoice if invoice is not None else _invoice(),
        run_date=run_date,
        confidence=_full_confidence() if confidence is None else confidence,
        duplicate_candidates=candidates,
    )


def _rules(rule: ValidationRule, ctx: RuleContext) -> list[ValidationFinding]:
    return RULE_FUNCTIONS[rule](ctx)


# --- missing_required_field (§2.1) ----------------------------------


@pytest.mark.parametrize(
    "field", ["invoice_number", "invoice_date", "vendor_name", "currency", "total_amount"]
)
def test_missing_required_field_fires_per_null_required_field(field: str) -> None:
    findings = _rules(
        ValidationRule.MISSING_REQUIRED_FIELD, _ctx(_invoice(**{field: None}))
    )
    assert [(f.field_path, f.severity.value) for f in findings] == [(field, "error")]


@pytest.mark.parametrize(
    "field", ["due_date", "vendor_tax_id", "customer_name", "subtotal", "tax_amount"]
)
def test_missing_required_field_ignores_optional_fields(field: str) -> None:
    assert _rules(
        ValidationRule.MISSING_REQUIRED_FIELD, _ctx(_invoice(**{field: None}))
    ) == []


def test_missing_required_field_none_when_all_present() -> None:
    assert _rules(ValidationRule.MISSING_REQUIRED_FIELD, _ctx()) == []


# --- normalization_error (§3, severity keyed to required-ness) ------


def test_normalization_error_on_required_field_is_error() -> None:
    inv = _invoice(
        total_amount=None,
        errors=[
            {
                "field_path": "total_amount",
                "raw_value": "1,2,3",
                "code": "invalid_number",
                "message": "bad",
            }
        ],
    )
    (finding,) = _rules(ValidationRule.NORMALIZATION_ERROR, _ctx(inv))
    assert finding.severity.value == "error"
    assert finding.field_path == "total_amount"
    assert finding.context == {"code": "invalid_number"}


def test_normalization_error_on_optional_scalar_is_warning() -> None:
    inv = _invoice(
        subtotal=None,
        errors=[
            {
                "field_path": "subtotal",
                "raw_value": "x",
                "code": "ambiguous_number",
                "message": "bad",
            }
        ],
    )
    (finding,) = _rules(ValidationRule.NORMALIZATION_ERROR, _ctx(inv))
    assert finding.severity.value == "warning"


def test_normalization_error_on_line_item_is_warning() -> None:
    inv = _invoice(
        line_items=[_li(quantity=None, unit_price=Decimal("1"), line_total=Decimal("1"))],
        errors=[
            {
                "field_path": "line_items.0.quantity",
                "raw_value": "??",
                "code": "invalid_number",
                "message": "bad",
            }
        ],
    )
    (finding,) = _rules(ValidationRule.NORMALIZATION_ERROR, _ctx(inv))
    assert finding.severity.value == "warning"
    assert finding.field_path == "line_items.0.quantity"


def test_missing_and_normalization_error_co_occur_for_a_null_required_field() -> None:
    inv = _invoice(
        currency=None,
        errors=[
            {
                "field_path": "currency",
                "raw_value": "ZZZ",
                "code": "unknown_currency",
                "message": "bad",
            }
        ],
    )
    fired = {f.rule for f in run_rules(_ctx(inv))}
    assert {
        ValidationRule.MISSING_REQUIRED_FIELD,
        ValidationRule.NORMALIZATION_ERROR,
    } <= fired


# --- date rules (§2.2 / §2.8) -------------------------------------


@pytest.mark.parametrize(
    ("invoice_date", "due_date", "fires"),
    [
        ("2026-02-10", "2026-02-09", True),
        ("2026-02-10", "2026-02-10", False),
        ("2026-02-10", "2026-02-11", False),
        (None, "2026-02-09", False),
        ("2026-02-10", None, False),
    ],
)
def test_due_date_before_invoice_date(
    invoice_date: str | None, due_date: str | None, fires: bool
) -> None:
    findings = _rules(
        ValidationRule.DUE_DATE_BEFORE_INVOICE_DATE,
        _ctx(_invoice(invoice_date=invoice_date, due_date=due_date)),
    )
    assert bool(findings) is fires
    if fires:
        assert findings[0].expected == invoice_date
        assert findings[0].actual == due_date
        assert findings[0].field_path == "due_date"


@pytest.mark.parametrize(
    ("gap_days", "fires"),
    [(365, False), (366, True), (10, False)],
)
def test_due_date_far_after_invoice_date_boundary(gap_days: int, fires: bool) -> None:
    invoice_date = "2026-01-01"
    due = (date.fromisoformat(invoice_date) + timedelta(days=gap_days)).isoformat()
    findings = _rules(
        ValidationRule.DUE_DATE_FAR_AFTER_INVOICE_DATE,
        _ctx(_invoice(invoice_date=invoice_date, due_date=due)),
    )
    assert bool(findings) is fires
    if fires:
        assert findings[0].context == {"max_gap_days": 365}


def test_due_date_gap_does_not_overflow_at_max_iso_date() -> None:
    inv = _invoice(invoice_date="9999-12-31", due_date="9999-12-31")
    assert _rules(ValidationRule.DUE_DATE_FAR_AFTER_INVOICE_DATE, _ctx(inv)) == []


@pytest.mark.parametrize(
    ("invoice_date", "fires"),
    [("2026-06-01", False), ("2026-06-02", True), ("2025-06-01", False)],
)
def test_invoice_date_in_future_boundary(invoice_date: str, fires: bool) -> None:
    findings = _rules(
        ValidationRule.INVOICE_DATE_IN_FUTURE,
        _ctx(_invoice(invoice_date=invoice_date)),
    )
    assert bool(findings) is fires
    if fires:
        assert findings[0].expected == RUN_DATE.isoformat()
        assert findings[0].actual == invoice_date
        assert findings[0].context == {"run_date": "2026-06-01"}


def test_invoice_date_implausibly_old_boundary() -> None:
    earliest = policy.earliest_plausible_invoice_date(RUN_DATE)  # 2016-06-01
    assert _rules(
        ValidationRule.INVOICE_DATE_IMPLAUSIBLY_OLD,
        _ctx(_invoice(invoice_date=earliest.isoformat())),
    ) == []
    older = (earliest - timedelta(days=1)).isoformat()
    (finding,) = _rules(
        ValidationRule.INVOICE_DATE_IMPLAUSIBLY_OLD,
        _ctx(_invoice(invoice_date=older)),
    )
    assert finding.expected == earliest.isoformat()
    assert finding.actual == older
    assert finding.context == {"run_date": "2026-06-01", "max_age_years": 10}


# --- totals_do_not_reconcile (§2.3) ------------------------------


@pytest.mark.parametrize(
    ("subtotal", "tax", "total", "fires"),
    [
        ("100.00", "19.00", "119.00", False),
        ("100.00", "19.00", "119.01", False),  # exactly 0.01 off -> within tolerance
        ("100.00", "19.00", "119.02", True),  # 0.02 off -> fires
        ("-100.00", "-19.00", "-119.00", False),  # negative totals reconcile
        ("-100.00", "-19.00", "-119.50", True),
    ],
)
def test_totals_do_not_reconcile_tolerance(
    subtotal: str, tax: str, total: str, fires: bool
) -> None:
    findings = _rules(
        ValidationRule.TOTALS_DO_NOT_RECONCILE,
        _ctx(
            _invoice(
                subtotal=Decimal(subtotal),
                tax_amount=Decimal(tax),
                total_amount=Decimal(total),
            )
        ),
    )
    assert bool(findings) is fires
    if fires:
        f = findings[0]
        assert f.field_path is None
        assert f.expected == Decimal(subtotal) + Decimal(tax)
        assert f.actual == Decimal(total)
        assert f.context["tolerance"] == Decimal("0.01")
        assert f.context["delta"] == (Decimal(subtotal) + Decimal(tax)) - Decimal(total)


@pytest.mark.parametrize("missing", ["subtotal", "tax_amount", "total_amount"])
def test_totals_do_not_reconcile_skips_when_a_component_is_null(missing: str) -> None:
    assert _rules(
        ValidationRule.TOTALS_DO_NOT_RECONCILE, _ctx(_invoice(**{missing: None}))
    ) == []


# --- line_item_amount_mismatch (§2.4) ---------------------------


@pytest.mark.parametrize(
    ("qty", "price", "line_total", "fires"),
    [
        ("2", "10.00", "20.00", False),
        ("2", "10.00", "20.01", False),  # 0.01 off
        ("2", "10.00", "20.02", True),  # 0.02 off
        ("3", "-5.00", "-15.00", False),
    ],
)
def test_line_item_amount_mismatch_tolerance(
    qty: str, price: str, line_total: str, fires: bool
) -> None:
    inv = _invoice(
        line_items=[
            _li(
                quantity=Decimal(qty),
                unit_price=Decimal(price),
                line_total=Decimal(line_total),
            )
        ]
    )
    findings = _rules(ValidationRule.LINE_ITEM_AMOUNT_MISMATCH, _ctx(inv))
    assert bool(findings) is fires
    if fires:
        f = findings[0]
        assert f.field_path == "line_items.0.line_total"
        assert f.context["line_index"] == 0
        assert f.expected == Decimal(qty) * Decimal(price)


def test_line_item_amount_mismatch_skips_incomplete_item_and_indexes_the_right_one() -> None:
    inv = _invoice(
        line_items=[
            _li(quantity=Decimal("1"), unit_price=None, line_total=Decimal("1")),
            _li(
                quantity=Decimal("2"),
                unit_price=Decimal("10"),
                line_total=Decimal("25"),
            ),
        ]
    )
    findings = _rules(ValidationRule.LINE_ITEM_AMOUNT_MISMATCH, _ctx(inv))
    assert [f.field_path for f in findings] == ["line_items.1.line_total"]


# --- line_items_do_not_sum (§2.4) -----------------------------


def test_line_items_sum_to_subtotal_within_tolerance() -> None:
    inv = _invoice(
        subtotal=Decimal("30.00"),
        line_items=[
            _li(line_total=Decimal("10.00")),
            _li(line_total=Decimal("20.00")),
        ],
    )
    assert _rules(ValidationRule.LINE_ITEMS_DO_NOT_SUM, _ctx(inv)) == []


def test_line_items_do_not_sum_reports_basis_and_context() -> None:
    inv = _invoice(
        subtotal=Decimal("30.00"),
        line_items=[
            _li(line_total=Decimal("10.00")),
            _li(line_total=Decimal("25.00")),
        ],
    )
    (f,) = _rules(ValidationRule.LINE_ITEMS_DO_NOT_SUM, _ctx(inv))
    assert f.field_path is None
    assert f.context["target_basis"] == "subtotal"
    assert f.context["line_count"] == 2
    assert f.context["sum"] == Decimal("35.00")
    assert f.context["delta"] == Decimal("5.00")
    assert f.expected == Decimal("30.00")
    assert f.actual == Decimal("35.00")


@pytest.mark.parametrize(
    ("subtotal", "tax", "total", "basis", "target"),
    [
        (None, "14.00", "119.00", "total_less_tax", "105.00"),  # 119 - 14
        (None, None, "35.00", "total", "35.00"),
    ],
)
def test_line_items_do_not_sum_target_precedence(
    subtotal, tax, total, basis: str, target: str
) -> None:
    inv = _invoice(
        subtotal=None if subtotal is None else Decimal(subtotal),
        tax_amount=None if tax is None else Decimal(tax),
        total_amount=None if total is None else Decimal(total),
        line_items=[_li(line_total=Decimal("10.00")), _li(line_total=Decimal("90.00"))],
    )
    (f,) = _rules(ValidationRule.LINE_ITEMS_DO_NOT_SUM, _ctx(inv))
    assert f.context["target_basis"] == basis
    assert f.expected == Decimal(target)


def test_line_items_do_not_sum_skips_without_a_target() -> None:
    inv = _invoice(
        subtotal=None,
        tax_amount=None,
        total_amount=None,
        line_items=[_li(line_total=Decimal("10.00"))],
    )
    assert _rules(ValidationRule.LINE_ITEMS_DO_NOT_SUM, _ctx(inv)) == []


def test_line_items_do_not_sum_skips_when_a_line_total_is_null() -> None:
    inv = _invoice(
        subtotal=Decimal("10.00"),
        line_items=[_li(line_total=Decimal("10.00")), _li(line_total=None)],
    )
    assert _rules(ValidationRule.LINE_ITEMS_DO_NOT_SUM, _ctx(inv)) == []


@pytest.mark.parametrize(("over", "fires"), [("0.03", False), ("0.04", True)])
def test_line_items_do_not_sum_per_line_tolerance_growth(over: str, fires: bool) -> None:
    # three lines -> tolerance max(0.01, 0.03) = 0.03
    inv = _invoice(
        subtotal=Decimal("30.00"),
        line_items=[
            _li(line_total=Decimal("10.00")),
            _li(line_total=Decimal("10.00")),
            _li(line_total=(Decimal("10.00") + Decimal(over))),
        ],
    )
    findings = _rules(ValidationRule.LINE_ITEMS_DO_NOT_SUM, _ctx(inv))
    assert bool(findings) is fires
    if fires:
        assert findings[0].context["tolerance"] == Decimal("0.03")


# --- line_item_sum_not_checked (§2.4) ------------------------


def test_line_item_sum_not_checked_counts_missing_totals() -> None:
    inv = _invoice(
        line_items=[
            _li(line_total=Decimal("1.00")),
            _li(line_total=None),
            _li(line_total=None),
        ]
    )
    (f,) = _rules(ValidationRule.LINE_ITEM_SUM_NOT_CHECKED, _ctx(inv))
    assert f.severity.value == "info"
    assert f.context == {"line_count": 3, "missing_line_total_count": 2}


def test_line_item_sum_not_checked_silent_when_all_totals_present() -> None:
    inv = _invoice(line_items=[_li(line_total=Decimal("1.00"))])
    assert _rules(ValidationRule.LINE_ITEM_SUM_NOT_CHECKED, _ctx(inv)) == []


def test_line_item_sum_not_checked_silent_without_line_items() -> None:
    assert _rules(ValidationRule.LINE_ITEM_SUM_NOT_CHECKED, _ctx()) == []


# --- confidence rules (§2.6) --------------------------------


@pytest.mark.parametrize(
    ("confidence", "fires"),
    [("0.70", False), ("0.69", True), ("0.6999", True), ("1", False)],
)
def test_low_confidence_critical_field_boundary(confidence: str, fires: bool) -> None:
    conf = _full_confidence()
    conf["vendor_name"] = Decimal(confidence)
    findings = _rules(
        ValidationRule.LOW_CONFIDENCE_CRITICAL_FIELD, _ctx(confidence=conf)
    )
    assert bool(findings) is fires
    if fires:
        assert findings[0].field_path == "vendor_name"
        assert findings[0].context == {
            "confidence": Decimal(confidence),
            "threshold": Decimal("0.70"),
        }


def test_low_confidence_skips_null_confidence_and_absent_value() -> None:
    conf = _full_confidence()
    conf["currency"] = None
    inv = _invoice(vendor_name=None)  # absent value -> no confidence finding
    conf.pop("vendor_name", None)
    assert _rules(
        ValidationRule.LOW_CONFIDENCE_CRITICAL_FIELD, _ctx(inv, confidence=conf)
    ) == []


def test_low_confidence_ignores_non_critical_fields() -> None:
    conf = {**_full_confidence(), "subtotal": Decimal("0.01")}
    assert _rules(
        ValidationRule.LOW_CONFIDENCE_CRITICAL_FIELD, _ctx(confidence=conf)
    ) == []


def test_confidence_unavailable_fires_only_for_present_value_and_missing_confidence() -> None:
    conf = {"invoice_number": Decimal("0.9")}  # only one of five present
    inv = _invoice(total_amount=None)  # absent -> not reported
    findings = _rules(
        ValidationRule.CRITICAL_FIELD_CONFIDENCE_UNAVAILABLE, _ctx(inv, confidence=conf)
    )
    reported = {f.field_path for f in findings}
    assert reported == {"invoice_date", "vendor_name", "currency"}
    assert all(f.severity.value == "info" and f.context == {} for f in findings)


def test_confidence_unavailable_silent_when_all_confidences_present() -> None:
    assert _rules(
        ValidationRule.CRITICAL_FIELD_CONFIDENCE_UNAVAILABLE, _ctx()
    ) == []


# --- probable_duplicate_invoice (§2.5) ---------------------


def _candidate(**overrides) -> DuplicateCandidate:
    base = {
        "document_id": uuid.UUID(int=1),
        "normalization_id": uuid.UUID(int=2),
        "vendor_tax_id": "DE123456789",
        "vendor_name": "Acme GmbH",
        "invoice_number": "INV-1",
        "currency": "EUR",
        "total_amount": Decimal("119.00"),
    }
    base.update(overrides)
    return DuplicateCandidate(**base)


def test_duplicate_exact_match_on_tax_id() -> None:
    (f,) = _rules(
        ValidationRule.PROBABLE_DUPLICATE_INVOICE, _ctx(candidates=[_candidate()])
    )
    assert f.field_path is None
    assert f.context["tolerance"] == Decimal("0.01")
    assert f.context["matches"] == [
        {
            "document_id": str(uuid.UUID(int=1)),
            "normalization_id": str(uuid.UUID(int=2)),
        }
    ]


def test_duplicate_matches_on_vendor_name_when_a_tax_id_is_null() -> None:
    inv = _invoice(vendor_tax_id=None)
    cand = _candidate(vendor_tax_id=None)
    assert len(_rules(ValidationRule.PROBABLE_DUPLICATE_INVOICE, _ctx(inv, candidates=[cand]))) == 1


@pytest.mark.parametrize(
    "override",
    [
        {"invoice_number": "INV-2"},
        {"currency": "USD"},
        {"vendor_tax_id": "DE999", "vendor_name": "Other"},
        {"total_amount": Decimal("119.02")},
    ],
)
def test_duplicate_no_match_when_a_key_field_differs(override: dict) -> None:
    assert _rules(
        ValidationRule.PROBABLE_DUPLICATE_INVOICE,
        _ctx(candidates=[_candidate(**override)]),
    ) == []


def test_duplicate_total_within_tolerance_matches() -> None:
    assert len(
        _rules(
            ValidationRule.PROBABLE_DUPLICATE_INVOICE,
            _ctx(candidates=[_candidate(total_amount=Decimal("119.01"))]),
        )
    ) == 1


def test_duplicate_multi_match_is_one_finding_sorted_deterministically() -> None:
    c1 = _candidate(document_id=uuid.UUID(int=5), normalization_id=uuid.UUID(int=9))
    c2 = _candidate(document_id=uuid.UUID(int=3), normalization_id=uuid.UUID(int=1))
    (f,) = _rules(
        ValidationRule.PROBABLE_DUPLICATE_INVOICE, _ctx(candidates=[c1, c2])
    )
    ids = [m["document_id"] for m in f.context["matches"]]
    assert ids == sorted(ids)
    assert ids[0] == str(uuid.UUID(int=3))


@pytest.mark.parametrize(
    "override",
    [
        {"invoice_number": None},
        {"currency": None},
        {"total_amount": None},
        {"vendor_tax_id": None, "vendor_name": None},
    ],
)
def test_duplicate_skips_when_a_key_field_is_null_on_the_invoice(override: dict) -> None:
    assert _rules(
        ValidationRule.PROBABLE_DUPLICATE_INVOICE,
        _ctx(_invoice(**override), candidates=[_candidate()]),
    ) == []


def test_duplicate_no_candidates_no_finding() -> None:
    assert _rules(ValidationRule.PROBABLE_DUPLICATE_INVOICE, _ctx()) == []


# --- high_value_invoice (§2.7) ------------------------------


@pytest.mark.parametrize(
    ("currency", "total", "fires"),
    [
        ("EUR", "10000", True),  # exactly at threshold
        ("EUR", "9999.99", False),
        ("EUR", "-10000.00", True),  # abs() applied
        ("JPY", "1500000", True),
        ("JPY", "1499999", False),
        ("SEK", "10000", True),  # default threshold
        ("SEK", "9999", False),
    ],
)
def test_high_value_invoice_threshold(currency: str, total: str, fires: bool) -> None:
    findings = _rules(
        ValidationRule.HIGH_VALUE_INVOICE,
        _ctx(_invoice(currency=currency, total_amount=Decimal(total))),
    )
    assert bool(findings) is fires
    if fires:
        assert findings[0].severity.value == "info"
        assert findings[0].context["currency"] == currency
        assert findings[0].context["threshold"] == policy.high_value_threshold(currency)


@pytest.mark.parametrize("missing", ["total_amount", "currency"])
def test_high_value_invoice_skips_when_input_null(missing: str) -> None:
    assert _rules(
        ValidationRule.HIGH_VALUE_INVOICE, _ctx(_invoice(**{missing: None}))
    ) == []


# --- no_line_items (§3) ------------------------------------


def test_no_line_items_fires_on_empty() -> None:
    (f,) = _rules(ValidationRule.NO_LINE_ITEMS, _ctx())
    assert f.severity.value == "info" and f.field_path is None


def test_no_line_items_silent_with_items() -> None:
    inv = _invoice(line_items=[_li(line_total=Decimal("1"))])
    assert _rules(ValidationRule.NO_LINE_ITEMS, _ctx(inv)) == []


# --- cross-cutting ---------------------------------------


def _rich_ctx() -> RuleContext:
    inv = _invoice(
        invoice_number=None,
        invoice_date="2027-01-01",  # future
        due_date="2026-12-01",  # before invoice_date
        currency="EUR",
        subtotal=Decimal("100.00"),
        tax_amount=Decimal("19.00"),
        total_amount=Decimal("130.00"),  # does not reconcile
        line_items=[
            _li(
                quantity=Decimal("2"),
                unit_price=Decimal("10.00"),
                line_total=Decimal("25.00"),
            ),
            _li(line_total=None),
        ],
        errors=[
            {
                "field_path": "invoice_number",
                "raw_value": "  ",
                "code": "invalid_number",
                "message": "bad",
            }
        ],
    )
    conf = {"vendor_name": Decimal("0.40"), "currency": None, "total_amount": Decimal("0.99")}
    cand = _candidate(invoice_number="INV-1")  # invoice_number is null -> dup skips
    return RuleContext(
        invoice=inv, run_date=RUN_DATE, confidence=conf, duplicate_candidates=[cand]
    )


def test_run_rules_is_deterministic() -> None:
    ctx = _rich_ctx()
    once = [f.model_dump(mode="json") for f in run_rules(ctx)]
    twice = [f.model_dump(mode="json") for f in run_rules(ctx)]
    assert once == twice
    assert once  # the rich context trips several rules


def test_run_rules_output_is_contract_valid_and_aggregates() -> None:
    findings = run_rules(_rich_ctx())
    payload = InvoiceValidation.from_findings(findings)
    assert payload.summary.total == len(findings)
    # round-trips through JSON unchanged
    assert (
        InvoiceValidation.model_validate_json(payload.model_dump_json()).model_dump_json()
        == payload.model_dump_json()
    )


def test_no_binary_float_anywhere_in_findings() -> None:
    def _walk(node: object) -> None:
        # A JSON number with a fractional part / exponent parses back to float;
        # every Stage 5 numeric leaf must serialise as a string instead.
        if isinstance(node, float):
            raise AssertionError("float leaf in a finding payload")
        if isinstance(node, dict):
            for value in node.values():
                _walk(value)
        if isinstance(node, list):
            for value in node:
                _walk(value)

    for finding in run_rules(_rich_ctx()):
        _walk(json.loads(finding.model_dump_json()))


def test_rule_functions_cover_every_validation_rule_in_order() -> None:
    assert tuple(RULE_FUNCTIONS) == tuple(ValidationRule)
    assert len(RULE_FUNCTIONS) == 15


def test_decimal_arithmetic_is_exact_not_float() -> None:
    # 0.1 * 3 is 0.30000000000000004 in binary float; Decimal keeps it exact,
    # so a line total of exactly 0.30 must reconcile.
    inv = _invoice(
        line_items=[
            _li(
                quantity=Decimal("3"),
                unit_price=Decimal("0.10"),
                line_total=Decimal("0.30"),
            )
        ]
    )
    assert _rules(ValidationRule.LINE_ITEM_AMOUNT_MISMATCH, _ctx(inv)) == []


def test_decimal_arithmetic_does_not_round_values_larger_than_fifty_digits() -> None:
    quantity = Decimal("9" * 60)
    expected = Decimal("9" * 60 + ".00")
    inv = _invoice(
        line_items=[
            _li(quantity=quantity, unit_price=Decimal("1.00"), line_total=expected)
        ]
    )
    assert _rules(ValidationRule.LINE_ITEM_AMOUNT_MISMATCH, _ctx(inv)) == []


def test_rule_function_registry_is_immutable() -> None:
    with pytest.raises(TypeError):
        RULE_FUNCTIONS[ValidationRule.NO_LINE_ITEMS] = lambda _ctx: []  # type: ignore[index]


# --- no AI, no network ---------------------------------


_NETWORK_IMPORT_RE = re.compile(
    r"^\s*(?:import|from)\s+(openai|anthropic|httpx|requests|urllib|http\.client|aiohttp|socket)\b",
    re.MULTILINE,
)


def test_validation_package_source_has_no_ai_or_network_import() -> None:
    pkg = Path(rules_mod.__file__).parent
    offenders = {
        path.name: _NETWORK_IMPORT_RE.findall(path.read_text(encoding="utf-8"))
        for path in pkg.rglob("*.py")
        if _NETWORK_IMPORT_RE.search(path.read_text(encoding="utf-8"))
    }
    assert offenders == {}, offenders


def test_run_rules_makes_no_socket_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    import socket

    def _forbidden(*_a, **_k):
        raise AssertionError("validation attempted a network connection")

    monkeypatch.setattr(socket.socket, "connect", _forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", _forbidden)
    monkeypatch.setattr(socket, "create_connection", _forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", _forbidden)

    assert run_rules(_rich_ctx())  # exercises every rule branch, no network
