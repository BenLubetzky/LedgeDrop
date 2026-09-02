"""Tests for the deterministic fake extraction provider (Stage 3, step 9).

The fake makes no external calls and covers every behaviour the extraction
service must handle: success, malformed output, timeout, rate limit, and general
provider failure.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.schemas.extraction import InvoiceExtraction
from app.services.processing.extraction.preprocessing import (
    PreparedDocument,
    PreparedPage,
    TextLayer,
)
from app.services.processing.extraction.provider import (
    ExtractionProvider,
    ProviderError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from app.services.processing.extraction.fake import (
    FAKE_PROVIDER_NAME,
    FakeBehavior,
    FakeExtractionProvider,
    deterministic_invoice_payload,
)


def _prepared(document_id: uuid.UUID | None = None) -> PreparedDocument:
    document_id = document_id or uuid.uuid4()
    page = PreparedPage(number=1, text="", char_count=0, has_text_layer=False)
    return PreparedDocument(
        document_id=document_id,
        page_count=1,
        pages=(page,),
        text_layer=TextLayer.ABSENT,
        text="",
        char_count=0,
        pdf_bytes=b"%PDF-1.4",
    )


# --- interface -------------------------------------------------------


def test_fake_satisfies_the_provider_interface() -> None:
    provider = FakeExtractionProvider()
    assert isinstance(provider, ExtractionProvider)
    assert provider.name == FAKE_PROVIDER_NAME
    assert provider.model == "v1"


# --- success -------------------------------------------------------


async def test_success_returns_a_schema_valid_payload() -> None:
    prepared = _prepared()
    payload = await FakeExtractionProvider(FakeBehavior.SUCCESS).extract(prepared)

    invoice = InvoiceExtraction.model_validate(payload)
    assert invoice.invoice_number.value.startswith("INV-")
    assert invoice.total_amount.value == Decimal("119.00")
    assert invoice.currency.value == "EUR"
    assert len(invoice.line_items) == 1
    # every scalar field is present and every confidence is a decimal in [0, 1]
    for name in ("invoice_number", "vendor_name", "subtotal", "due_date"):
        field = getattr(invoice, name)
        assert field.confidence is None or Decimal(0) <= field.confidence <= Decimal(1)


async def test_success_is_deterministic_per_document() -> None:
    document_id = uuid.uuid4()
    provider = FakeExtractionProvider()

    first = await provider.extract(_prepared(document_id))
    again = await provider.extract(_prepared(document_id))
    other = await provider.extract(_prepared(uuid.uuid4()))

    assert first == again
    assert first["invoice_number"]["value"] != other["invoice_number"]["value"]


def test_deterministic_payload_helper_is_pure() -> None:
    document_id = uuid.uuid4()
    assert deterministic_invoice_payload(document_id) == deterministic_invoice_payload(document_id)


# --- malformed (returned, not raised) --------------------------------


async def test_malformed_behaviour_returns_output_that_fails_validation() -> None:
    payload = await FakeExtractionProvider(FakeBehavior.MALFORMED).extract(_prepared())

    # It is a value, not an exception...
    assert isinstance(payload, dict)
    # ...but it does not satisfy the invoice contract.
    with pytest.raises(ValidationError):
        InvoiceExtraction.model_validate(payload)


# --- failures (raised) --------------------------------------------


@pytest.mark.parametrize(
    ("behavior", "error", "code"),
    [
        (FakeBehavior.TIMEOUT, ProviderTimeoutError, "PROVIDER_TIMEOUT"),
        (FakeBehavior.RATE_LIMITED, ProviderRateLimitError, "PROVIDER_RATE_LIMITED"),
        (FakeBehavior.ERROR, ProviderUnavailableError, "PROVIDER_UNAVAILABLE"),
    ],
)
async def test_failure_behaviours_raise_typed_provider_errors(
    behavior: FakeBehavior, error: type[ProviderError], code: str
) -> None:
    with pytest.raises(error) as excinfo:
        await FakeExtractionProvider(behavior).extract(_prepared())

    assert isinstance(excinfo.value, ProviderError)
    assert excinfo.value.code == code
    assert excinfo.value.safe_message  # non-empty, client-safe


# --- no external calls -------------------------------------------


async def test_no_behaviour_opens_a_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    import socket

    def _forbidden(*args, **kwargs):  # pragma: no cover - only hit on regression
        raise AssertionError("the fake provider must not touch the network")

    monkeypatch.setattr(socket, "socket", _forbidden)

    for behavior in FakeBehavior:
        provider = FakeExtractionProvider(behavior)
        try:
            await provider.extract(_prepared())
        except ProviderError:
            pass  # expected for the failure behaviours
