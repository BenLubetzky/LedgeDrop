"""Tests for the Stage 4 normalization backend schemas (step 5): the
nested<->flat persistence bridge and the request / public response models.

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

from app.models.normalization import (
    NormalizationAttempt,
    NormalizationFieldError,
    NormalizationLineItem,
    NormalizationStatus,
)
from app.schemas.normalization import NormalizedInvoice
from app.schemas.normalization_api import (
    InvoiceNormalizationResult,
    NormalizationStartRequest,
)
from app.schemas.normalization_persistence import (
    ERROR_FIELD_NAMES,
    error_rows,
    line_item_rows,
    normalized_invoice_from_attempt,
    normalized_invoice_from_rows,
    normalized_invoice_to_columns,
    scalar_columns,
)


def _full_contract() -> NormalizedInvoice:
    """A completed normalization: canonical values, two line items, two field
    errors (each on a field that is correctly left null)."""
    return NormalizedInvoice.model_validate(
        {
            "invoice_number": "INV-28491",
            "invoice_date": "2026-01-15",
            "due_date": None,  # errored below
            "vendor_name": "Acme GmbH",
            "vendor_tax_id": "DE123456789",
            "customer_name": "Beta Ltd",
            "currency": "EUR",
            "subtotal": "100.00",
            "tax_amount": "19.00",
            "total_amount": "119.00",
            "line_items": [
                {
                    "description": "Widget",
                    "quantity": "2",
                    "unit_price": "10.00",
                    "line_total": "20.00",
                },
                {
                    "description": "Gadget",
                    "quantity": "1.5",
                    "unit_price": None,  # errored below
                    "line_total": "99.00",
                },
            ],
            "errors": [
                {
                    "field_path": "due_date",
                    "raw_value": "31/02/2026",
                    "code": "invalid_date",
                    "message": "The due date could not be recognized in a supported format.",
                },
                {
                    "field_path": "line_items.1.unit_price",
                    "raw_value": "1,2,3",
                    "code": "invalid_number",
                    "message": "The unit price is not a valid number.",
                },
            ],
        }
    )


# --- field-name registries -----------------------------------------------


def test_error_field_names_are_the_four_contract_columns() -> None:
    assert ERROR_FIELD_NAMES == ("field_path", "raw_value", "code", "message")


def test_flattened_keys_all_exist_as_orm_columns() -> None:
    attempt_cols = set(NormalizationAttempt.__table__.columns.keys())
    line_item_cols = set(NormalizationLineItem.__table__.columns.keys())
    error_cols = set(NormalizationFieldError.__table__.columns.keys())

    contract = _full_contract()
    assert set(scalar_columns(contract)) <= attempt_cols
    for row in line_item_rows(contract):
        assert set(row) <= line_item_cols
    for row in error_rows(contract):
        assert set(row) <= error_cols


# --- flatten ----------------------------------------------------------


def test_scalar_columns_are_single_canonical_values_without_confidence() -> None:
    cols = scalar_columns(_full_contract())

    assert cols["invoice_number"] == "INV-28491"
    assert cols["invoice_date"] == "2026-01-15"
    assert cols["due_date"] is None
    assert cols["currency"] == "EUR"
    assert cols["total_amount"] == Decimal("119.00")
    # no confidence, no identity/status columns
    assert not any(k.endswith("_confidence") for k in cols)
    assert "status" not in cols and "normalization_id" not in cols and "extraction_id" not in cols


def test_line_item_rows_are_positional_and_flat() -> None:
    rows = line_item_rows(_full_contract())

    assert [r["position"] for r in rows] == [0, 1]
    assert rows[0]["description"] == "Widget"
    assert rows[0]["quantity"] == Decimal("2")
    assert rows[1]["unit_price"] is None
    assert rows[1]["line_total"] == Decimal("99.00")


def test_empty_line_items_flattens_to_empty_list() -> None:
    contract = _full_contract().model_copy(update={"line_items": [], "errors": []})
    assert line_item_rows(contract) == []


def test_error_rows_carry_path_raw_value_code_and_message() -> None:
    rows = error_rows(_full_contract())

    assert [r["field_path"] for r in rows] == ["due_date", "line_items.1.unit_price"]
    assert rows[0]["raw_value"] == "31/02/2026"
    assert rows[0]["code"].value == "invalid_date"
    assert rows[1]["message"] == "The unit price is not a valid number."


# --- round trip -----------------------------------------------------


def test_contract_survives_a_flatten_then_rebuild_round_trip() -> None:
    original = _full_contract()
    scalars, items, errs = normalized_invoice_to_columns(original)

    rebuilt = normalized_invoice_from_rows(scalars, items, errs)

    assert rebuilt == original
    assert rebuilt.model_dump() == original.model_dump()


def test_rebuild_orders_line_items_by_persisted_position() -> None:
    original = _full_contract()
    scalars, items, errs = normalized_invoice_to_columns(original)

    rebuilt = normalized_invoice_from_rows(scalars, reversed(items), errs)

    assert rebuilt == original


@pytest.mark.parametrize("positions", [[0, 0], [0, 2], [1, 2]])
def test_rebuild_rejects_duplicate_or_non_contiguous_positions(
    positions: list[int],
) -> None:
    scalars, items, errs = normalized_invoice_to_columns(_full_contract())
    for item, position in zip(items, positions, strict=True):
        item["position"] = position

    with pytest.raises(ValueError, match="unique and contiguous"):
        normalized_invoice_from_rows(scalars, items, errs)


def test_rebuild_reenforces_the_date_shape() -> None:
    scalars, items, errs = normalized_invoice_to_columns(_full_contract())
    scalars["invoice_date"] = "2026-13-01"  # not a real calendar date

    with pytest.raises(ValidationError):
        normalized_invoice_from_rows(scalars, items, errs)


def test_rebuild_reenforces_the_errored_field_is_null_rule() -> None:
    scalars, items, errs = normalized_invoice_to_columns(_full_contract())
    scalars["due_date"] = "2026-02-01"  # a value, but due_date still has an error row

    with pytest.raises(ValidationError):
        normalized_invoice_from_rows(scalars, items, errs)


# --- rebuild straight off an ORM attempt ---------------------------


def _persisted_attempt(**overrides) -> NormalizationAttempt:
    """A transient ORM attempt carrying a completed normalization. No session."""
    scalars, items, errs = normalized_invoice_to_columns(_full_contract())
    now = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
    defaults = dict(
        normalization_id=uuid.uuid4(),
        extraction_id=uuid.uuid4(),
        attempt_number=1,
        status=NormalizationStatus.COMPLETED,
        started_at=now,
        completed_at=now,
        created_at=now,
        updated_at=now,
        failure_code=None,
        failure_message=None,
        **scalars,
    )
    defaults.update(overrides)
    attempt = NormalizationAttempt(**defaults)
    attempt.line_items = [NormalizationLineItem(**row) for row in items]
    attempt.errors = [NormalizationFieldError(**row) for row in errs]
    return attempt


def test_normalized_invoice_from_attempt_matches_the_source_contract() -> None:
    assert normalized_invoice_from_attempt(_persisted_attempt()) == _full_contract()


# --- request model ---------------------------------------------


def test_start_request_accepts_empty_body() -> None:
    assert NormalizationStartRequest.model_validate({}) is not None


def test_start_request_rejects_unknown_keys() -> None:
    with pytest.raises(ValidationError):
        NormalizationStartRequest.model_validate({"locale": "en-GB"})


# --- public result -------------------------------------------


def test_public_result_shape_and_nested_data() -> None:
    result = InvoiceNormalizationResult.from_attempt(_persisted_attempt())

    assert result.status is NormalizationStatus.COMPLETED
    assert isinstance(result.extraction_id, uuid.UUID)
    assert result.data.invoice_number == "INV-28491"
    assert [li.description for li in result.data.line_items] == ["Widget", "Gadget"]
    assert [e.field_path for e in result.data.errors] == [
        "due_date",
        "line_items.1.unit_price",
    ]


def test_public_result_serializes_decimals_as_strings_and_utc_timestamps() -> None:
    payload = json.loads(
        InvoiceNormalizationResult.from_attempt(_persisted_attempt()).model_dump_json()
    )

    assert payload["data"]["total_amount"] == "119.00"
    assert payload["data"]["line_items"][0]["quantity"] == "2"
    assert payload["started_at"].endswith("Z")
    assert payload["completed_at"].endswith("Z")


def test_public_result_never_exposes_confidence_or_internal_fields() -> None:
    dumped = InvoiceNormalizationResult.from_attempt(_persisted_attempt()).model_dump_json()

    assert "confidence" not in dumped
    assert "raw_response" not in dumped
    assert "document_id" not in InvoiceNormalizationResult.model_fields


def test_public_result_carries_client_safe_failure_information() -> None:
    attempt = _persisted_attempt(
        status=NormalizationStatus.FAILED,
        failure_code="SOURCE_EXTRACTION_UNREADABLE",
        failure_message="The source extraction could not be loaded.",
    )
    result = InvoiceNormalizationResult.from_attempt(attempt)

    assert result.status is NormalizationStatus.FAILED
    assert result.failure_code == "SOURCE_EXTRACTION_UNREADABLE"
    assert result.failure_message == "The source extraction could not be loaded."


def test_public_result_rejects_unknown_keys() -> None:
    payload = InvoiceNormalizationResult.from_attempt(_persisted_attempt()).model_dump()
    payload["raw_response"] = {"internal": True}

    with pytest.raises(ValidationError):
        InvoiceNormalizationResult.model_validate(payload)


def test_public_result_rejects_naive_timestamps() -> None:
    payload = InvoiceNormalizationResult.from_attempt(_persisted_attempt()).model_dump()
    payload["started_at"] = datetime(2026, 9, 3, 12, 0)

    with pytest.raises(ValidationError):
        InvoiceNormalizationResult.model_validate(payload)


@pytest.mark.parametrize(
    "overrides",
    [
        {
            "status": NormalizationStatus.PROCESSING,
            "completed_at": datetime.now(timezone.utc),
        },
        {"status": NormalizationStatus.COMPLETED, "completed_at": None},
        {
            "status": NormalizationStatus.FAILED,
            "failure_code": None,
            "failure_message": None,
        },
    ],
)
def test_public_result_rejects_inconsistent_status_fields(overrides: dict) -> None:
    payload = InvoiceNormalizationResult.from_attempt(_persisted_attempt()).model_dump()
    payload.update(overrides)

    with pytest.raises(ValidationError):
        InvoiceNormalizationResult.model_validate(payload)


def test_public_result_omits_no_canonical_field() -> None:
    payload = json.loads(
        InvoiceNormalizationResult.from_attempt(_persisted_attempt()).model_dump_json()
    )

    for name in (
        "invoice_number", "invoice_date", "due_date", "vendor_name", "vendor_tax_id",
        "customer_name", "currency", "subtotal", "tax_amount", "total_amount",
    ):
        assert name in payload["data"], name
    assert "line_items" in payload["data"] and "errors" in payload["data"]
