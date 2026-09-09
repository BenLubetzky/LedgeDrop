"""Pure tests for the Stage 8 merge projection (no database)."""

from __future__ import annotations

from decimal import Decimal

from app.schemas.correction import CorrectionOperation
from app.schemas.normalization import NormalizedInvoice
from app.services.processing.correction.projection import (
    SubmittedInvoice,
    merge_correction,
)

_BASE_SCALARS = {
    "invoice_number": "INV-1",
    "invoice_date": "2026-01-01",
    "due_date": None,
    "vendor_name": "Acme",
    "vendor_tax_id": None,
    "customer_name": "Bob",
    "currency": "EUR",
    "subtotal": Decimal("100.00"),
    "tax_amount": Decimal("20.00"),
    "total_amount": Decimal("120.00"),
}


def _base(**overrides) -> NormalizedInvoice:
    scalars = {**_BASE_SCALARS, **overrides.pop("scalars", {})}
    return NormalizedInvoice.model_validate(
        {
            **scalars,
            "line_items": overrides.pop(
                "line_items",
                [
                    {
                        "description": "Widget",
                        "quantity": Decimal("1"),
                        "unit_price": Decimal("100.00"),
                        "line_total": Decimal("100.00"),
                    }
                ],
            ),
            "errors": overrides.pop("errors", []),
        }
    )


def _submitted(base: NormalizedInvoice, **scalar_overrides) -> SubmittedInvoice:
    scalars = {
        name: (str(v) if (v := getattr(base, name)) is not None else None)
        for name in _BASE_SCALARS
    }
    scalars.update(scalar_overrides)
    return SubmittedInvoice(
        scalars=scalars,
        line_items=[
            {
                k: (str(v) if (v := getattr(li, k)) is not None else None)
                for k in ("description", "quantity", "unit_price", "line_total")
            }
            for li in base.line_items
        ],
    )


def test_untouched_submission_produces_no_entries() -> None:
    base = _base()
    _, entries = merge_correction(base, _submitted(base))
    assert entries == []


def test_scalar_change_is_a_set_field_entry_with_before_and_after() -> None:
    base = _base()
    merged, entries = merge_correction(
        base, _submitted(base, total_amount="130.00", vendor_name="New Vendor")
    )
    by_path = {e.field_path: e for e in entries}
    assert set(by_path) == {"total_amount", "vendor_name"}
    assert by_path["total_amount"].operation is CorrectionOperation.SET_FIELD
    assert by_path["total_amount"].previous_value == "120.00"
    assert by_path["total_amount"].normalized_value == "130.00"
    assert merged.vendor_name == "New Vendor"


def test_unparseable_value_yields_error_entry_and_merged_error() -> None:
    base = _base()
    merged, entries = merge_correction(base, _submitted(base, currency="ZZZ"))
    entry = next(e for e in entries if e.field_path == "currency")
    assert entry.error_code is not None
    assert entry.normalized_value is None
    assert merged.currency is None
    assert any(err.field_path == "currency" for err in merged.errors)


def test_appended_line_item_adds_marker_plus_leaf_sets() -> None:
    base = _base()
    submitted = _submitted(base)
    submitted.line_items.append(
        {"description": "Extra", "quantity": "2", "unit_price": "5", "line_total": "10"}
    )
    merged, entries = merge_correction(base, submitted)
    ops = [(e.operation, e.field_path) for e in entries]
    assert (CorrectionOperation.ADD_LINE_ITEM, "line_items.1") in ops
    assert (CorrectionOperation.SET_FIELD, "line_items.1.description") in ops
    assert len(merged.line_items) == 2


def test_dropped_line_item_is_a_remove_marker() -> None:
    base = _base()
    submitted = SubmittedInvoice(scalars=_submitted(base).scalars, line_items=[])
    merged, entries = merge_correction(base, submitted)
    assert [e.operation for e in entries] == [CorrectionOperation.REMOVE_LINE_ITEM]
    assert entries[0].field_path == "line_items.0"
    assert merged.line_items == []


def test_merge_is_idempotent_for_a_clean_submission() -> None:
    base = _base()
    merged, entries = merge_correction(
        base, _submitted(base, vendor_name="Corrected Co")
    )
    again = SubmittedInvoice(
        scalars={
            name: (str(v) if (v := getattr(merged, name)) is not None else None)
            for name in _BASE_SCALARS
        },
        line_items=[
            {
                k: (str(v) if (v := getattr(li, k)) is not None else None)
                for k in ("description", "quantity", "unit_price", "line_total")
            }
            for li in merged.line_items
        ],
    )
    merged2, entries2 = merge_correction(merged, again)
    assert entries2 == []
    assert merged2.model_dump() == merged.model_dump()
