"""Tests for the Stage 3 backend schemas (step 4): the flat<->nested
persistence mapping and the request / public response models.

No database and no AI provider are involved - these exercise pure schema and
mapping behaviour.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.models.extraction import ExtractionAttempt, ExtractionLineItem, ExtractionStatus
from app.schemas.extraction import InvoiceExtraction
from app.schemas.extraction_api import ExtractionStartRequest, InvoiceExtractionResult
from app.schemas.extraction_persistence import (
    LINE_ITEM_FIELD_NAMES,
    SCALAR_FIELD_NAMES,
    invoice_extraction_from_attempt,
    invoice_extraction_from_row,
    invoice_extraction_to_columns,
    line_item_columns,
    scalar_columns,
)


def _field(value=None, confidence=None) -> dict:
    return {"value": value, "confidence": confidence}


def _full_contract() -> InvoiceExtraction:
    return InvoiceExtraction.model_validate(
        {
            "invoice_number": _field("INV-28491", "0.97"),
            "invoice_date": _field("15 Jan 2026", "0.8"),
            "due_date": _field(None, None),
            "vendor_name": _field("Acme GmbH", "0.91"),
            "vendor_tax_id": _field("DE123456789"),
            "customer_name": _field("Beta Ltd", None),
            "currency": _field("eur", "1"),
            "subtotal": _field("100.00", "0.9"),
            "tax_amount": _field("19.00", "0.9"),
            "total_amount": _field("119.00", "0.95"),
            "line_items": [
                {
                    "description": _field("Widget", "0.99"),
                    "quantity": _field("2", "0.99"),
                    "unit_price": _field("10.00", "0.5"),
                    "line_total": _field("20.00", None),
                },
                {
                    "description": _field("Gadget"),
                    "quantity": _field("1.5"),
                    "unit_price": _field("66.00"),
                    "line_total": _field("99.00"),
                },
            ],
        }
    )


# --- field-name registries stay in step with the ORM -----------------------


def test_scalar_field_names_match_contract() -> None:
    assert SCALAR_FIELD_NAMES == (
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
    assert "line_items" not in SCALAR_FIELD_NAMES
    assert LINE_ITEM_FIELD_NAMES == ("description", "quantity", "unit_price", "line_total")


def test_flattened_keys_all_exist_as_orm_columns() -> None:
    attempt_columns = set(ExtractionAttempt.__table__.columns.keys())
    line_item_cols = set(ExtractionLineItem.__table__.columns.keys())

    produced = set(scalar_columns(_full_contract()))
    assert produced <= attempt_columns

    for row in line_item_columns(_full_contract()):
        assert set(row) <= line_item_cols


# --- flatten -------------------------------------------------------------- --


def test_scalar_columns_splits_each_field_into_value_and_confidence() -> None:
    cols = scalar_columns(_full_contract())

    assert cols["invoice_number_value"] == "INV-28491"
    assert cols["invoice_number_confidence"] == Decimal("0.97")
    # A missing value stays null, not "".
    assert cols["due_date_value"] is None
    assert cols["due_date_confidence"] is None
    # currency is upper-cased by the contract before it reaches persistence.
    assert cols["currency_value"] == "EUR"
    # No identity / status / provider columns leak in.
    assert "status" not in cols and "provider_name" not in cols and "extraction_id" not in cols


def test_line_item_columns_are_positional_and_flat() -> None:
    rows = line_item_columns(_full_contract())

    assert [r["position"] for r in rows] == [0, 1]
    assert rows[0]["description_value"] == "Widget"
    assert rows[0]["quantity_value"] == Decimal("2")
    assert rows[1]["line_total_value"] == Decimal("99.00")
    assert rows[1]["description_confidence"] is None


def test_empty_line_items_flattens_to_empty_list() -> None:
    contract = _full_contract().model_copy(update={"line_items": []})
    assert line_item_columns(contract) == []


# --- round trip --------------------------------------------------------- ----


def test_contract_survives_a_flatten_then_rebuild_round_trip() -> None:
    original = _full_contract()
    scalars, items = invoice_extraction_to_columns(original)

    rebuilt = invoice_extraction_from_row(scalars, items)

    assert rebuilt == original
    assert rebuilt.model_dump() == original.model_dump()


def test_rebuild_from_row_reenforces_confidence_bounds() -> None:
    scalars, items = invoice_extraction_to_columns(_full_contract())
    scalars["total_amount_confidence"] = Decimal("1.5")

    with pytest.raises(ValidationError):
        invoice_extraction_from_row(scalars, items)


# --- rebuild straight off an ORM attempt --------------------------------- ---


def _persisted_attempt(**overrides) -> ExtractionAttempt:
    """A transient ORM attempt carrying a completed extraction. No session."""
    scalars, items = invoice_extraction_to_columns(_full_contract())
    now = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    defaults = dict(
        extraction_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        attempt_number=1,
        status=ExtractionStatus.COMPLETED,
        provider_name="fake",
        provider_model="fake-v1",
        raw_response={"secret_provider_prose": "looks like an invoice to me"},
        started_at=now,
        completed_at=now,
        created_at=now,
        updated_at=now,
        failure_code=None,
        failure_message=None,
        **scalars,
    )
    defaults.update(overrides)
    attempt = ExtractionAttempt(**defaults)
    attempt.line_items = [ExtractionLineItem(**row) for row in items]
    return attempt


def test_invoice_extraction_from_attempt_matches_the_source_contract() -> None:
    attempt = _persisted_attempt()
    assert invoice_extraction_from_attempt(attempt) == _full_contract()


# --- request model ------------------------------------------------------ ----


def test_start_request_accepts_empty_body() -> None:
    assert ExtractionStartRequest.model_validate({}) is not None


def test_start_request_rejects_unknown_keys() -> None:
    with pytest.raises(ValidationError):
        ExtractionStartRequest.model_validate({"provider": "anthropic"})


# --- public result ----------------------------------------------------- -----


def test_public_result_shape_and_nested_data() -> None:
    result = InvoiceExtractionResult.from_attempt(_persisted_attempt())

    assert result.status is ExtractionStatus.COMPLETED
    assert result.provider_name == "fake"
    assert result.data.invoice_number.value == "INV-28491"
    assert result.data.invoice_number.confidence == Decimal("0.97")
    assert [li.description.value for li in result.data.line_items] == ["Widget", "Gadget"]


def test_public_result_never_exposes_raw_provider_response() -> None:
    attempt = _persisted_attempt()
    dumped = InvoiceExtractionResult.from_attempt(attempt).model_dump_json()

    assert "raw_response" not in dumped
    assert "secret_provider_prose" not in dumped
    # The model has no such field at all.
    assert "raw_response" not in InvoiceExtractionResult.model_fields


def test_public_result_serializes_decimals_as_strings_and_utc_timestamps() -> None:
    payload = json.loads(InvoiceExtractionResult.from_attempt(_persisted_attempt()).model_dump_json())

    assert payload["data"]["total_amount"]["value"] == "119.00"
    assert payload["data"]["total_amount"]["confidence"] == "0.95"
    assert payload["started_at"].endswith("Z")
    assert payload["completed_at"].endswith("Z")


def test_public_result_carries_client_safe_failure_information() -> None:
    attempt = _persisted_attempt(
        status=ExtractionStatus.FAILED,
        failure_code="PROVIDER_TIMEOUT",
        failure_message="The extraction provider did not respond in time.",
    )
    result = InvoiceExtractionResult.from_attempt(attempt)

    assert result.status is ExtractionStatus.FAILED
    assert result.failure_code == "PROVIDER_TIMEOUT"
    assert result.failure_message == "The extraction provider did not respond in time."


def test_public_result_omits_no_extracted_field() -> None:
    payload = json.loads(InvoiceExtractionResult.from_attempt(_persisted_attempt()).model_dump_json())

    for name in SCALAR_FIELD_NAMES:
        assert name in payload["data"], name
        assert set(payload["data"][name]) == {"value", "confidence"}
    assert "line_items" in payload["data"]
