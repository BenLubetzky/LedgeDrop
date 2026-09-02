"""Tests for the OpenAI GPT-5-mini extraction adapter (Stage 3, step 12).

Every test here runs against a stub OpenAI client - never the real API - so the
suite stays offline and deterministic like every other Stage 3 test. The stub
only fills in what the adapter actually reads off a response
(``output_text`` / ``model_dump``), so these tests exercise the adapter's own
mapping, validation, and error-translation logic, not the SDK.
"""

from __future__ import annotations

import json
import uuid
from decimal import Decimal
from typing import Any

import httpx2
import openai
import pytest
from pydantic import ValidationError

from app.schemas.extraction import InvoiceExtraction
from app.services.processing.extraction.openai_provider import (
    DEFAULT_OPENAI_MODEL,
    OPENAI_PROVIDER_NAME,
    OpenAIExtractionProvider,
    map_model_output,
)
from app.services.processing.extraction.preprocessing import (
    PreparedDocument,
    PreparedPage,
    TextLayer,
)
from app.services.processing.extraction.provider import (
    ExtractionProvider,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    ProviderResponse,
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


_VALID_MODEL_JSON = {
    "invoice_number": "INV-100",
    "invoice_date": "15 Jan 2026",
    "due_date": None,
    "vendor_name": "Acme GmbH",
    "vendor_tax_id": None,
    "customer_name": "LedgerDrop Demo Customer",
    "currency": "eur",
    "subtotal": "100.00",
    "tax_amount": "19.00",
    "total_amount": "119.00",
    "line_items": [
        {
            "description": "Consulting",
            "quantity": "10",
            "unit_price": "10.00",
            "line_total": "100.00",
        }
    ],
}


class _StubResponse:
    def __init__(self, output_text: str, dumped: dict[str, Any] | None = None) -> None:
        self.output_text = output_text
        self._dumped = dumped if dumped is not None else {"id": "resp_stub", "output_text": output_text}

    def model_dump(self, mode: str = "python") -> dict[str, Any]:
        return self._dumped


class _StubResponses:
    def __init__(self, result: _StubResponse | BaseException) -> None:
        self._result = result
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> _StubResponse:
        self.calls.append(kwargs)
        if isinstance(self._result, BaseException):
            raise self._result
        return self._result


class _StubClient:
    def __init__(self, result: _StubResponse | BaseException) -> None:
        self.responses = _StubResponses(result)


def _provider(result: _StubResponse | BaseException) -> tuple[OpenAIExtractionProvider, _StubClient]:
    client = _StubClient(result)
    provider = OpenAIExtractionProvider(api_key=None, client=client)
    return provider, client


# --- interface / construction -----------------------------------------


def test_satisfies_the_provider_interface() -> None:
    provider, _ = _provider(_StubResponse(json.dumps(_VALID_MODEL_JSON)))
    assert isinstance(provider, ExtractionProvider)
    assert provider.name == OPENAI_PROVIDER_NAME
    assert provider.model == DEFAULT_OPENAI_MODEL


def test_construction_without_api_key_or_client_fails_fast() -> None:
    with pytest.raises(RuntimeError):
        OpenAIExtractionProvider(api_key=None)


# --- success -------------------------------------------------------


async def test_success_maps_to_a_schema_valid_payload_with_null_confidence() -> None:
    provider, client = _provider(_StubResponse(json.dumps(_VALID_MODEL_JSON)))

    result = await provider.extract(_prepared())
    assert isinstance(result, ProviderResponse)
    payload, raw_response = result.payload, result.raw_response

    invoice = InvoiceExtraction.model_validate(payload)
    assert invoice.invoice_number.value == "INV-100"
    assert invoice.currency.value == "EUR"  # upper-cased by the contract
    assert invoice.total_amount.value == Decimal("119.00")
    assert len(invoice.line_items) == 1
    assert invoice.line_items[0].quantity.value == Decimal("10")

    # No field ever carries a self-reported confidence.
    for name in InvoiceExtraction.model_fields:
        if name == "line_items":
            continue
        assert getattr(invoice, name).confidence is None
    assert invoice.line_items[0].quantity.confidence is None

    assert raw_response is not None
    assert len(client.responses.calls) == 1


async def test_extract_sends_the_pdf_and_a_strict_json_schema() -> None:
    provider, client = _provider(_StubResponse(json.dumps(_VALID_MODEL_JSON)))

    await provider.extract(_prepared())

    call = client.responses.calls[0]
    assert call["model"] == DEFAULT_OPENAI_MODEL
    file_part = call["input"][0]["content"][0]
    assert file_part["type"] == "input_file"
    assert file_part["file_data"].startswith("data:application/pdf;base64,")
    assert call["text"]["format"]["strict"] is True
    assert call["text"]["format"]["schema"]["additionalProperties"] is False
    assert call["store"] is False


# --- malformed (returned, not raised) --------------------------------


async def test_unparsable_output_returns_a_payload_that_fails_validation() -> None:
    provider, _ = _provider(_StubResponse("not json"))

    result = await provider.extract(_prepared())
    assert isinstance(result, ProviderResponse)
    payload, raw_response = result.payload, result.raw_response

    assert isinstance(payload, dict)
    with pytest.raises(ValidationError):
        InvoiceExtraction.model_validate(payload)
    assert raw_response is not None


def test_map_model_output_defends_against_an_off_schema_payload() -> None:
    assert map_model_output("not a dict") == {}
    assert map_model_output(None) == {}

    assert map_model_output({**_VALID_MODEL_JSON, "line_items": "not a list"}) == {}
    assert map_model_output({**_VALID_MODEL_JSON, "unexpected": "prose"}) == {}
    assert map_model_output({**_VALID_MODEL_JSON, "line_items": [{"description": "x"}]}) == {}


# --- failures (raised) -------------------------------------------


async def test_timeout_is_translated_to_a_provider_timeout_error() -> None:
    request = httpx2.Request("POST", "https://api.openai.com/v1/responses")
    provider, _ = _provider(openai.APITimeoutError(request=request))

    with pytest.raises(ProviderTimeoutError):
        await provider.extract(_prepared())


async def test_rate_limit_is_translated_to_a_provider_rate_limit_error() -> None:
    request = httpx2.Request("POST", "https://api.openai.com/v1/responses")
    response = httpx2.Response(status_code=429, request=request)
    provider, _ = _provider(openai.RateLimitError("rate limited", response=response, body=None))

    with pytest.raises(ProviderRateLimitError):
        await provider.extract(_prepared())


async def test_connection_error_is_translated_to_a_provider_unavailable_error() -> None:
    request = httpx2.Request("POST", "https://api.openai.com/v1/responses")
    provider, _ = _provider(openai.APIConnectionError(request=request))

    with pytest.raises(ProviderUnavailableError):
        await provider.extract(_prepared())


async def test_other_openai_errors_are_translated_to_a_provider_unavailable_error() -> None:
    provider, _ = _provider(openai.OpenAIError("something else went wrong"))

    with pytest.raises(ProviderUnavailableError):
        await provider.extract(_prepared())
