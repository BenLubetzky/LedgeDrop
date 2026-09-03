"""Tests for the internal normalized invoice data contract (Stage 4, step 1)."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.schemas.normalization import (
    NORMALIZED_LINE_ITEM_FIELD_NAMES,
    NORMALIZED_SCALAR_FIELD_NAMES,
    NormalizationError,
    NormalizationErrorCode,
    NormalizedInvoice,
    NormalizedInvoiceResult,
    NormalizedLineItem,
)


def _line_item(**overrides) -> dict:
    item = {
        "description": "Widget",
        "quantity": "2",
        "unit_price": "10.00",
        "line_total": "20.00",
    }
    item.update(overrides)
    return item


def _payload(**overrides) -> dict:
    payload = {
        "invoice_number": "INV-1",
        "invoice_date": "2026-01-15",
        "due_date": None,
        "vendor_name": "Acme GmbH",
        "vendor_tax_id": None,
        "customer_name": "Beta Ltd",
        "currency": "EUR",
        "subtotal": "100.00",
        "tax_amount": "19.00",
        "total_amount": "119.00",
        "line_items": [],
        "errors": [],
    }
    payload.update(overrides)
    return payload


def _error(**overrides) -> dict:
    err = {
        "field_path": "invoice_date",
        "raw_value": "31/02/2026",
        "code": "invalid_date",
        "message": "The invoice date is not a real calendar date.",
    }
    err.update(overrides)
    return err


# --- happy path --------------------------------------------------------


def test_minimal_valid_payload_parses() -> None:
    result = NormalizedInvoice.model_validate(_payload())

    assert result.invoice_number == "INV-1"
    assert result.invoice_date == "2026-01-15"
    assert result.due_date is None
    assert result.vendor_tax_id is None
    assert result.currency == "EUR"
    assert result.subtotal == Decimal("100.00")
    assert result.line_items == []
    assert result.errors == []


def test_full_payload_with_line_items_and_errors_parses() -> None:
    result = NormalizedInvoice.model_validate(
        _payload(
            line_items=[
                _line_item(),
                _line_item(description="Gadget", quantity="1", unit_price=None),
            ],
            errors=[_error(field_path="line_items.1.unit_price", code="invalid_number")],
        )
    )

    assert len(result.line_items) == 2
    assert result.line_items[1].description == "Gadget"
    assert result.line_items[1].quantity == Decimal("1")
    assert result.errors[0].field_path == "line_items.1.unit_price"
    assert result.errors[0].code is NormalizationErrorCode.INVALID_NUMBER


@pytest.mark.parametrize("name", NORMALIZED_SCALAR_FIELD_NAMES)
def test_every_scalar_field_may_be_null(name: str) -> None:
    result = NormalizedInvoice.model_validate(_payload(**{name: None}))
    assert getattr(result, name) is None


def test_no_confidence_is_carried_on_the_normalized_contract() -> None:
    assert "confidence" not in NormalizedInvoice.model_fields
    assert "confidence" not in NormalizedLineItem.model_fields


# --- dates -----------------------------------------------------------


def test_canonical_date_is_accepted() -> None:
    result = NormalizedInvoice.model_validate(_payload(invoice_date="2026-12-31"))
    assert result.invoice_date == "2026-12-31"


@pytest.mark.parametrize("bad", ["2026-1-5", "15/01/2026", "Jan 15, 2026", "20260115", "2026-01-15T00:00:00"])
def test_non_canonical_date_is_rejected(bad: str) -> None:
    with pytest.raises(ValidationError):
        NormalizedInvoice.model_validate(_payload(invoice_date=bad))


@pytest.mark.parametrize("bad", ["2026-02-30", "2026-13-01", "2026-00-10"])
def test_impossible_calendar_date_is_rejected(bad: str) -> None:
    with pytest.raises(ValidationError):
        NormalizedInvoice.model_validate(_payload(invoice_date=bad))


def test_null_date_is_accepted() -> None:
    result = NormalizedInvoice.model_validate(_payload(due_date=None))
    assert result.due_date is None


# --- currency ------------------------------------------------------


def test_currency_is_upcased() -> None:
    result = NormalizedInvoice.model_validate(_payload(currency="usd"))
    assert result.currency == "USD"


@pytest.mark.parametrize("bad", ["US", "EURO", "12", "€", "e u"])
def test_malformed_currency_is_rejected(bad: str) -> None:
    with pytest.raises(ValidationError):
        NormalizedInvoice.model_validate(_payload(currency=bad))


def test_currency_may_be_null() -> None:
    result = NormalizedInvoice.model_validate(_payload(currency=None))
    assert result.currency is None


# --- text ----------------------------------------------------------


@pytest.mark.parametrize("bad", ["", "   ", "\t", "\n"])
def test_empty_or_whitespace_text_is_rejected(bad: str) -> None:
    with pytest.raises(ValidationError):
        NormalizedInvoice.model_validate(_payload(vendor_name=bad))


@pytest.mark.parametrize("bad", [" Acme", "Acme ", " Acme "])
def test_untrimmed_text_is_rejected(bad: str) -> None:
    with pytest.raises(ValidationError):
        NormalizedInvoice.model_validate(_payload(vendor_name=bad))


def test_identifier_stays_a_string() -> None:
    result = NormalizedInvoice.model_validate(_payload(invoice_number="007"))
    assert result.invoice_number == "007"


# --- money and quantities ----------------------------------------


def test_money_and_quantity_are_decimal_without_float_artifacts() -> None:
    result = NormalizedInvoice.model_validate(
        _payload(
            subtotal="0.10",
            tax_amount="0.20",
            total_amount="0.30",
            line_items=[_line_item(quantity="1.5", unit_price="0.20", line_total="0.30")],
        )
    )

    assert result.subtotal == Decimal("0.10")
    assert result.line_items[0].quantity == Decimal("1.5")
    dumped = result.model_dump_json().replace(" ", "")
    assert '"total_amount":"0.30"' in dumped


@pytest.mark.parametrize("bad", ["nan", "inf", "-inf"])
def test_money_rejects_nan_and_infinity(bad: str) -> None:
    with pytest.raises(ValidationError):
        NormalizedInvoice.model_validate(_payload(total_amount=bad))


# --- structured errors -----------------------------------------


def test_error_records_field_path_raw_value_code_and_message() -> None:
    err = NormalizationError.model_validate(_error())
    assert err.field_path == "invoice_date"
    assert err.raw_value == "31/02/2026"
    assert err.code is NormalizationErrorCode.INVALID_DATE
    assert err.message.startswith("The invoice date")


def test_error_raw_value_may_be_null() -> None:
    err = NormalizationError.model_validate(_error(raw_value=None))
    assert err.raw_value is None


def test_error_code_must_be_in_the_closed_set() -> None:
    with pytest.raises(ValidationError):
        NormalizationError.model_validate(_error(code="totally_made_up"))


@pytest.mark.parametrize("path", ["total_amount", "currency", "line_items.0.unit_price"])
def test_known_error_field_paths_are_accepted(path: str) -> None:
    payload = _payload(
        total_amount=None,
        currency=None,
        line_items=[_line_item(unit_price=None)],
    )
    NormalizedInvoice.model_validate(
        {**payload, "errors": [_error(field_path=path)]}
    )


@pytest.mark.parametrize("path", ["not_a_field", "line_items.0.bogus", "line_items.x.unit_price", "line_items"])
def test_unknown_error_field_path_is_rejected(path: str) -> None:
    with pytest.raises(ValidationError):
        NormalizedInvoice.model_validate(
            _payload(line_items=[_line_item()], errors=[_error(field_path=path)])
        )


def test_error_path_referencing_missing_line_item_is_rejected() -> None:
    with pytest.raises(ValidationError):
        NormalizedInvoice.model_validate(
            _payload(line_items=[], errors=[_error(field_path="line_items.0.quantity")])
        )


@pytest.mark.parametrize("path", ["total_amount", "line_items.0.unit_price"])
def test_field_with_error_must_have_null_normalized_value(path: str) -> None:
    with pytest.raises(ValidationError):
        NormalizedInvoice.model_validate(
            _payload(line_items=[_line_item()], errors=[_error(field_path=path)])
        )


def test_line_item_index_in_error_path_must_be_canonical() -> None:
    with pytest.raises(ValidationError):
        NormalizationError.model_validate(_error(field_path="line_items.00.quantity"))


# --- strictness ----------------------------------------------


def test_unknown_top_level_key_is_rejected() -> None:
    with pytest.raises(ValidationError):
        NormalizedInvoice.model_validate(_payload(model_notes="looks fine"))


def test_unknown_key_inside_a_line_item_is_rejected() -> None:
    with pytest.raises(ValidationError):
        NormalizedInvoice.model_validate(
            _payload(line_items=[_line_item(source_page=1)])
        )


def test_unknown_key_inside_an_error_is_rejected() -> None:
    with pytest.raises(ValidationError):
        NormalizationError.model_validate(_error(severity="high"))


@pytest.mark.parametrize("missing", [*NORMALIZED_SCALAR_FIELD_NAMES, "line_items", "errors"])
def test_missing_required_field_is_rejected(missing: str) -> None:
    payload = _payload()
    del payload[missing]
    with pytest.raises(ValidationError):
        NormalizedInvoice.model_validate(payload)


# --- source extraction reference ---------------------------


def test_result_binds_normalized_invoice_to_source_extraction() -> None:
    extraction_id = uuid.uuid4()
    result = NormalizedInvoiceResult.model_validate(
        {"source_extraction_id": str(extraction_id), "normalized": _payload()}
    )
    assert result.source_extraction_id == extraction_id
    assert isinstance(result.normalized, NormalizedInvoice)


def test_result_requires_the_source_extraction_reference() -> None:
    with pytest.raises(ValidationError):
        NormalizedInvoiceResult.model_validate({"normalized": _payload()})


# --- derived name tuples ----------------------------------


def test_scalar_field_names_match_the_model() -> None:
    assert set(NORMALIZED_SCALAR_FIELD_NAMES) == {
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
    }
    assert "line_items" not in NORMALIZED_SCALAR_FIELD_NAMES
    assert "errors" not in NORMALIZED_SCALAR_FIELD_NAMES


def test_line_item_field_names_match_the_model() -> None:
    assert NORMALIZED_LINE_ITEM_FIELD_NAMES == (
        "description",
        "quantity",
        "unit_price",
        "line_total",
    )
