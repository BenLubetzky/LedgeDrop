"""A deterministic, offline fake extraction provider (Stage 3, step 9).

Implements :class:`ExtractionProvider` with **no OCR, no parsing, and no external
calls**. It is what the extraction endpoints run on during development and what
the automated suite always uses, so tests never depend on an AI service.

Behaviour is chosen at construction (:class:`FakeBehavior`):

* ``SUCCESS``      - a fixed, schema-valid invoice payload whose identifying
  values are derived from the document id, so the same document always yields
  the same result and different documents yield visibly different ones.
* ``MALFORMED``    - a payload that does **not** satisfy the invoice contract
  (returned, not raised - the service rejects it as ``MALFORMED_EXTRACTION``).
* ``TIMEOUT``      - raises :class:`ProviderTimeoutError`.
* ``RATE_LIMITED`` - raises :class:`ProviderRateLimitError`.
* ``ERROR``        - raises :class:`ProviderUnavailableError`.

Step 12 adds the real adapter behind the same interface; this stays as the
test/dev double.
"""

from __future__ import annotations

import enum
from typing import Any

from app.services.processing.extraction.preprocessing import PreparedDocument
from app.services.processing.extraction.provider import (
    ProviderPayload,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)

FAKE_PROVIDER_NAME = "fake-deterministic"
FAKE_PROVIDER_MODEL = "v1"


class FakeBehavior(str, enum.Enum):
    SUCCESS = "SUCCESS"
    MALFORMED = "MALFORMED"
    TIMEOUT = "TIMEOUT"
    RATE_LIMITED = "RATE_LIMITED"
    ERROR = "ERROR"


def _pair(value: object, confidence: str | None) -> dict[str, Any]:
    return {"value": value, "confidence": confidence}


def deterministic_invoice_payload(document_id: object) -> dict[str, Any]:
    """The ``SUCCESS`` payload for a document id. A plain dict, exactly the shape
    :class:`InvoiceExtraction` expects, so the service validates it like any
    provider response."""
    tag = str(document_id).split("-")[0].upper()  # first 8 hex chars of the UUID
    return {
        "invoice_number": _pair(f"INV-{tag}", "0.95"),
        "invoice_date": _pair("2026-01-15", "0.9"),
        "due_date": _pair("2026-02-14", "0.6"),
        "vendor_name": _pair("Deterministic Test Vendor GmbH", "0.88"),
        "vendor_tax_id": _pair("DE999999999", "0.7"),
        "customer_name": _pair("LedgerDrop Demo Customer", "0.8"),
        "currency": _pair("EUR", "0.99"),
        "subtotal": _pair("100.00", "0.9"),
        "tax_amount": _pair("19.00", "0.9"),
        "total_amount": _pair("119.00", "0.95"),
        "line_items": [
            {
                "description": _pair("Consulting services", "0.9"),
                "quantity": _pair("10", "0.95"),
                "unit_price": _pair("10.00", "0.9"),
                "line_total": _pair("100.00", "0.9"),
            }
        ],
    }


def _malformed_payload() -> dict[str, Any]:
    # Confidence out of range and an unknown key: fails the contract on validation.
    return {"invoice_number": {"value": "INV-?", "confidence": "1.7"}, "note": "looks invoicey"}


class FakeExtractionProvider:
    """Deterministic offline :class:`ExtractionProvider`."""

    name = FAKE_PROVIDER_NAME

    def __init__(
        self,
        behavior: FakeBehavior = FakeBehavior.SUCCESS,
        *,
        model: str = FAKE_PROVIDER_MODEL,
    ) -> None:
        self.behavior = behavior
        self.model = model

    async def extract(self, prepared: PreparedDocument) -> ProviderPayload:
        if self.behavior is FakeBehavior.TIMEOUT:
            raise ProviderTimeoutError("The extraction provider did not respond in time.")
        if self.behavior is FakeBehavior.RATE_LIMITED:
            raise ProviderRateLimitError("The extraction provider is rate limiting requests.")
        if self.behavior is FakeBehavior.ERROR:
            raise ProviderUnavailableError("The extraction provider is currently unavailable.")
        if self.behavior is FakeBehavior.MALFORMED:
            return _malformed_payload()
        return deterministic_invoice_payload(prepared.document_id)


__all__ = [
    "FakeBehavior",
    "FakeExtractionProvider",
    "deterministic_invoice_payload",
    "FAKE_PROVIDER_NAME",
    "FAKE_PROVIDER_MODEL",
]
