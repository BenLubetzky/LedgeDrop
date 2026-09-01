"""Tests for the internal invoice extraction data contract (Stage 3, step 1)."""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.schemas.extraction import (
    ExtractedField,
    InvoiceExtraction,
    Money,
)


def _field(value=None, confidence=None) -> dict:
    return {"value": value, "confidence": confidence}


def _minimal_payload(**overrides) -> dict:
    payload = {
        "invoice_number": _field("INV-1"),
        "invoice_date": _field("2026-01-15", "0.98"),
        "due_date": _field(None, None),
        "vendor_name": _field("Acme GmbH", "0.9"),
        "vendor_tax_id": _field(None),
        "customer_name": _field("Beta Ltd"),
        "currency": _field("eur", "1"),
        "subtotal": _field("100.00"),
        "tax_amount": _field("19.00"),
        "total_amount": _field("119.00", "0.95"),
        "line_items": [],
    }
    payload.update(overrides)
    return payload


# --- happy path -----------------------------------------------------------


def test_minimal_valid_payload_parses() -> None:
    result = InvoiceExtraction.model_validate(_minimal_payload())

    assert result.invoice_number.value == "INV-1"
    assert result.invoice_number.confidence is None
    assert result.invoice_date.value == "2026-01-15"
    assert result.invoice_date.confidence == Decimal("0.98")
    # Missing values are represented as null, not "".
    assert result.due_date.value is None
    assert result.vendor_tax_id.value is None
    assert result.line_items == []


def test_money_and_quantity_are_decimal_without_float_artifacts() -> None:
    payload = _minimal_payload(
        subtotal=_field("0.10"),
        tax_amount=_field("0.20"),
        total_amount=_field("0.30"),
        line_items=[
            {
                "description": _field("Widget"),
                "quantity": _field("1.5"),
                "unit_price": _field("0.20"),
                "line_total": _field("0.30"),
            }
        ],
    )
    result = InvoiceExtraction.model_validate(payload)

    assert result.subtotal.value == Decimal("0.10")
    assert result.line_items[0].quantity.value == Decimal("1.5")
    # Serializes as a JSON string, exact - no 0.1 + 0.2 = 0.30000000000000004.
    dumped = result.model_dump_json()
    assert '"value":"0.30"' in dumped.replace(" ", "")


def test_no_document_level_confidence_field() -> None:
    assert "confidence" not in InvoiceExtraction.model_fields


# --- confidence bounds --------------------------------------------------- --


def test_confidence_may_be_null() -> None:
    ExtractedField[str].model_validate({"value": "x", "confidence": None})


@pytest.mark.parametrize("bad", ["1.5", "-0.01", "2"])
def test_confidence_out_of_range_is_rejected(bad: str) -> None:
    with pytest.raises(ValidationError):
        ExtractedField[str].model_validate({"value": "x", "confidence": bad})


@pytest.mark.parametrize("ok", ["0", "1", "0.5", "0.000", "1.0"])
def test_confidence_within_range_is_accepted(ok: str) -> None:
    parsed = ExtractedField[str].model_validate({"value": "x", "confidence": ok})
    assert parsed.confidence == Decimal(ok)


# --- dates --------------------------------------------------------------- --


@pytest.mark.parametrize(
    "raw_date",
    ["15/01/2026", "Jan 15, 2026", "2026-1-5", "2026-13-01", "2026-02-30", "20260115"],
)
def test_date_is_preserved_for_later_normalization(raw_date: str) -> None:
    parsed = InvoiceExtraction.model_validate(
        _minimal_payload(invoice_date=_field(raw_date))
    )
    assert parsed.invoice_date.value == raw_date


def test_date_must_still_be_structurally_a_string() -> None:
    with pytest.raises(ValidationError):
        InvoiceExtraction.model_validate(_minimal_payload(invoice_date=_field(20260115)))


def test_null_date_is_accepted() -> None:
    parsed = InvoiceExtraction.model_validate(_minimal_payload(due_date=_field(None)))
    assert parsed.due_date.value is None


# --- currency ---------------------------------------------------------- ----


def test_currency_is_upcased() -> None:
    parsed = InvoiceExtraction.model_validate(_minimal_payload(currency=_field("usd")))
    assert parsed.currency.value == "USD"


@pytest.mark.parametrize("bad", ["US", "EURO", "12", "€"])
def test_malformed_currency_is_rejected(bad: str) -> None:
    with pytest.raises(ValidationError):
        InvoiceExtraction.model_validate(_minimal_payload(currency=_field(bad)))


def test_currency_may_be_null() -> None:
    parsed = InvoiceExtraction.model_validate(_minimal_payload(currency=_field(None)))
    assert parsed.currency.value is None


# --- strictness ------------------------------------------------------- -----


def test_unknown_top_level_key_is_rejected() -> None:
    with pytest.raises(ValidationError):
        InvoiceExtraction.model_validate(_minimal_payload(model_notes="looks like an invoice"))


def test_unknown_key_inside_a_field_is_rejected() -> None:
    payload = _minimal_payload()
    payload["invoice_number"] = {"value": "INV-1", "confidence": None, "source_page": 1}
    with pytest.raises(ValidationError):
        InvoiceExtraction.model_validate(payload)


def test_both_value_and_confidence_keys_are_required() -> None:
    with pytest.raises(ValidationError):
        ExtractedField[str].model_validate({"value": "x"})
    with pytest.raises(ValidationError):
        ExtractedField[str].model_validate({"confidence": None})


def test_missing_scalar_field_is_rejected() -> None:
    payload = _minimal_payload()
    del payload["total_amount"]
    with pytest.raises(ValidationError):
        InvoiceExtraction.model_validate(payload)


@pytest.mark.parametrize("bad", ["nan", "inf", "-inf"])
def test_money_rejects_nan_and_infinity(bad: str) -> None:
    with pytest.raises(ValidationError):
        ExtractedField[Money].model_validate({"value": bad, "confidence": None})
