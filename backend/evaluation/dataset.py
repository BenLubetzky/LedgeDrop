"""Loader for the extraction evaluation dataset.

``load_cases()`` reads ``expected.json`` and returns typed :class:`EvalCase`
records. ``ExpectedInvoice.as_contract_payload()`` renders the ground truth in
the exact shape :class:`app.schemas.extraction.InvoiceExtraction` accepts (each
value wrapped as ``{"value": ..., "confidence": null}``), so a provider's output
can be diffed against it field by field.

This module has no dependency on the extraction service or any provider.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

EVAL_DIR = Path(__file__).resolve().parent
INVOICES_DIR = EVAL_DIR / "invoices"
MANIFEST_PATH = EVAL_DIR / "expected.json"

SCALAR_FIELDS: tuple[str, ...] = (
    "invoice_number",
    "invoice_date",
    "due_date",
    "vendor_name",
    "vendor_tax_id",
    "customer_name",
    "currency",
    "subtotal",
    "tax_amount",
    "total_amount",
)
LINE_ITEM_FIELDS: tuple[str, ...] = ("description", "quantity", "unit_price", "line_total")

CRITICAL_FIELDS: tuple[str, ...] = (
    "invoice_number",
    "invoice_date",
    "vendor_name",
    "currency",
    "total_amount",
)

# Every category the dataset is expected to exercise (see docs/stage-3-extraction.md).
REQUIRED_CATEGORIES: frozenset[str] = frozenset(
    {
        "digital",
        "scanned",
        "multi_page",
        "incomplete",
        "multi_line_items",
        "unusual_layout",
        "low_quality",
        "not_invoice",
    }
)


@dataclass(frozen=True)
class ExpectedLineItem:
    description: str | None
    quantity: str | None
    unit_price: str | None
    line_total: str | None


@dataclass(frozen=True)
class ExpectedInvoice:
    invoice_number: str | None
    invoice_date: str | None
    due_date: str | None
    vendor_name: str | None
    vendor_tax_id: str | None
    customer_name: str | None
    currency: str | None
    subtotal: str | None
    tax_amount: str | None
    total_amount: str | None
    line_items: tuple[ExpectedLineItem, ...]

    def as_contract_payload(self) -> dict[str, Any]:
        """Ground truth in ``InvoiceExtraction`` shape (confidences are ``None``)."""

        def field(value: str | None) -> dict[str, Any]:
            return {"value": value, "confidence": None}

        payload: dict[str, Any] = {name: field(getattr(self, name)) for name in SCALAR_FIELDS}
        payload["line_items"] = [
            {name: field(getattr(item, name)) for name in LINE_ITEM_FIELDS}
            for item in self.line_items
        ]
        return payload


@dataclass(frozen=True)
class EvalCase:
    id: str
    path: Path
    category: str
    is_invoice: bool
    readable: bool
    text_layer: str  # "DIGITAL" | "ABSENT" | "UNREADABLE"
    notes: str
    expected: ExpectedInvoice | None


def _parse_expected(raw: dict[str, Any] | None) -> ExpectedInvoice | None:
    if raw is None:
        return None
    line_items = tuple(
        ExpectedLineItem(**{name: item.get(name) for name in LINE_ITEM_FIELDS})
        for item in raw.get("line_items", [])
    )
    scalars = {name: raw.get(name) for name in SCALAR_FIELDS}
    return ExpectedInvoice(line_items=line_items, **scalars)


def load_cases() -> list[EvalCase]:
    entries = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    cases: list[EvalCase] = []
    for entry in entries:
        cases.append(
            EvalCase(
                id=entry["id"],
                path=EVAL_DIR / entry["file"],
                category=entry["category"],
                is_invoice=entry["is_invoice"],
                readable=entry["readable"],
                text_layer=entry["text_layer"],
                notes=entry["notes"],
                expected=_parse_expected(entry.get("expected")),
            )
        )
    return cases


__all__ = [
    "EVAL_DIR",
    "INVOICES_DIR",
    "MANIFEST_PATH",
    "SCALAR_FIELDS",
    "LINE_ITEM_FIELDS",
    "CRITICAL_FIELDS",
    "REQUIRED_CATEGORIES",
    "ExpectedLineItem",
    "ExpectedInvoice",
    "EvalCase",
    "load_cases",
]
