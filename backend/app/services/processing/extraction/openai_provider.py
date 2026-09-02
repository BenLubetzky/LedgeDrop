"""The real extraction adapter: OpenAI GPT-5-mini (Stage 3, step 12).

Implements :class:`ExtractionProvider` behind the same interface the fake
provider satisfies. This is the first *real* adapter LedgerDrop ships; Azure AI
Document Intelligence remains a candidate for a later migration (see
``docs/provider-selection.md``).

Design constraints this module exists to satisfy:

* **Schema-constrained output.** The model is called through the Responses API
  with a strict ``json_schema`` response format, so the API itself rejects any
  shape other than the one requested - the model cannot hand back free-form
  prose instead of structured data.
* **Nothing is trusted blind.** Whatever comes back is only ever mapped into
  plain ``{value, confidence}`` dicts and returned as a *payload* for
  :class:`~app.services.processing.extraction.service.ExtractionService` to
  validate against :class:`InvoiceExtraction`. A response that is missing,
  unparsable, or otherwise off-schema is mapped to a payload that fails that
  validation - never raised as if the provider itself had failed, and never
  patched up or guessed at.
* **No fabricated confidence.** GPT-5-mini has no calibrated per-field
  confidence the way a document-extraction model does; a self-reported number
  from the model would just be more free-form prose wearing a numeric mask.
  Every field's confidence is ``None`` here, full stop - the model is not even
  asked for one.
* **The original PDF, untouched.** The stored PDF bytes are sent to OpenAI's
  file input as-is; this module does no rendering or re-encoding of its own.

The raw API response is returned alongside the mapped payload in a typed
``ProviderResponse`` purely as internal audit data. It is never itself treated
as the payload.
"""

from __future__ import annotations

import base64
import json
import logging
from typing import Any

from openai import (
    APIConnectionError,
    APITimeoutError,
    AsyncOpenAI,
    OpenAIError,
    RateLimitError,
)
from pydantic import BaseModel, ConfigDict, ValidationError

from app.services.processing.extraction.preprocessing import PreparedDocument
from app.services.processing.extraction.provider import (
    ProviderRateLimitError,
    ProviderResponse,
    ProviderResult,
    ProviderTimeoutError,
    ProviderUnavailableError,
)

logger = logging.getLogger("app.extraction.openai_provider")

OPENAI_PROVIDER_NAME = "openai"
DEFAULT_OPENAI_MODEL = "gpt-5-mini"

_MAX_OUTPUT_TOKENS = 4096

_INSTRUCTIONS = """\
You are extracting structured data from one English-language business invoice \
supplied as a PDF. Read the whole document and return only the fields defined \
by the response schema.

Rules:
- Never invent a value. If a field is not present or not legible, its value is \
null.
- Dates: return the date exactly as printed on the document (same characters, \
same order). Do not reformat, reinterpret, or convert it.
- currency: the 3-letter ISO 4217 currency code, only if it is printed or \
unambiguously implied by a currency symbol/name on the document. Never guess \
and never default to a currency that is not evidenced in the document.
- Monetary amounts and quantities: a plain decimal number as a string (e.g. \
"1234.56"), with no currency symbols, letters, or thousands separators.
- line_items: one entry per distinct line item row on the invoice, in the \
order they appear. An empty list if the invoice has no line items table.
- Every field in the schema must be present in your response; use null for \
anything you cannot determine from the document.
"""

_NULLABLE_STRING = {"type": ["string", "null"]}

_LINE_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "description": _NULLABLE_STRING,
        "quantity": _NULLABLE_STRING,
        "unit_price": _NULLABLE_STRING,
        "line_total": _NULLABLE_STRING,
    },
    "required": ["description", "quantity", "unit_price", "line_total"],
    "additionalProperties": False,
}

_INVOICE_SCHEMA = {
    "type": "object",
    "properties": {
        "invoice_number": _NULLABLE_STRING,
        "invoice_date": _NULLABLE_STRING,
        "due_date": _NULLABLE_STRING,
        "vendor_name": _NULLABLE_STRING,
        "vendor_tax_id": _NULLABLE_STRING,
        "customer_name": _NULLABLE_STRING,
        "currency": _NULLABLE_STRING,
        "subtotal": _NULLABLE_STRING,
        "tax_amount": _NULLABLE_STRING,
        "total_amount": _NULLABLE_STRING,
        "line_items": {"type": "array", "items": _LINE_ITEM_SCHEMA},
    },
    "required": [
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
        "line_items",
    ],
    "additionalProperties": False,
}

_RESPONSE_FORMAT = {
    "type": "json_schema",
    "name": "invoice_extraction",
    "schema": _INVOICE_SCHEMA,
    "strict": True,
}

# A payload guaranteed to fail InvoiceExtraction validation: every required key
# is absent. Used when the model's response cannot be parsed as the requested
# shape at all - the service rejects it as MALFORMED_EXTRACTION, not a provider
# failure, matching how a truly malformed provider response is handled.
_UNPARSABLE_PAYLOAD: dict[str, Any] = {}


class _ModelLineItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str | None
    quantity: str | None
    unit_price: str | None
    line_total: str | None


class _ModelInvoice(BaseModel):
    model_config = ConfigDict(extra="forbid")

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
    line_items: list[_ModelLineItem]


def _pair(value: Any) -> dict[str, Any]:
    return {"value": value, "confidence": None}


def _map_line_item(item: _ModelLineItem) -> dict[str, Any]:
    return {
        "description": _pair(item.description),
        "quantity": _pair(item.quantity),
        "unit_price": _pair(item.unit_price),
        "line_total": _pair(item.line_total),
    }


def map_model_output(payload: Any) -> dict[str, Any]:
    """Map the model's decoded JSON into the ``{value, confidence}`` shape
    :class:`InvoiceExtraction` expects. Confidence is always ``None``.

    Defensive against a payload that does not match the requested schema (the
    API's strict mode should prevent this, but nothing from an external
    service is trusted to be well-shaped without checking).
    """
    try:
        invoice = _ModelInvoice.model_validate(payload)
    except ValidationError:
        return _UNPARSABLE_PAYLOAD

    return {
        "invoice_number": _pair(invoice.invoice_number),
        "invoice_date": _pair(invoice.invoice_date),
        "due_date": _pair(invoice.due_date),
        "vendor_name": _pair(invoice.vendor_name),
        "vendor_tax_id": _pair(invoice.vendor_tax_id),
        "customer_name": _pair(invoice.customer_name),
        "currency": _pair(invoice.currency),
        "subtotal": _pair(invoice.subtotal),
        "tax_amount": _pair(invoice.tax_amount),
        "total_amount": _pair(invoice.total_amount),
        "line_items": [_map_line_item(item) for item in invoice.line_items],
    }


class OpenAIExtractionProvider:
    """:class:`ExtractionProvider` backed by OpenAI's Responses API."""

    name = OPENAI_PROVIDER_NAME

    def __init__(
        self,
        *,
        api_key: str | None,
        model: str = DEFAULT_OPENAI_MODEL,
        timeout_seconds: float = 60,
        client: AsyncOpenAI | None = None,
    ) -> None:
        if client is None and not api_key:
            raise RuntimeError(
                "OpenAIExtractionProvider requires OPENAI_API_KEY to be set "
                "when EXTRACTION_PROVIDER=openai."
            )
        self.model = model
        self._timeout_seconds = timeout_seconds
        self._client = client or AsyncOpenAI(api_key=api_key, max_retries=0)

    async def extract(self, prepared: PreparedDocument) -> ProviderResult:
        encoded = base64.b64encode(prepared.pdf_bytes).decode("ascii")
        try:
            response = await self._client.responses.create(
                model=self.model,
                instructions=_INSTRUCTIONS,
                input=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "input_file",
                                "filename": f"{prepared.document_id}.pdf",
                                "file_data": f"data:application/pdf;base64,{encoded}",
                            },
                            {
                                "type": "input_text",
                                "text": "Extract the invoice fields from this document.",
                            },
                        ],
                    }
                ],
                text={"format": _RESPONSE_FORMAT},
                max_output_tokens=_MAX_OUTPUT_TOKENS,
                store=False,
                timeout=self._timeout_seconds,
            )
        except APITimeoutError as exc:
            raise ProviderTimeoutError(
                "The extraction provider did not respond in time."
            ) from exc
        except RateLimitError as exc:
            raise ProviderRateLimitError(
                "The extraction provider is rate limiting requests."
            ) from exc
        except (APIConnectionError, OpenAIError) as exc:
            logger.warning("OpenAI extraction call failed: %s", exc)
            raise ProviderUnavailableError(
                "The extraction provider is currently unavailable."
            ) from exc

        raw_response = response.model_dump(mode="json")

        try:
            decoded = json.loads(response.output_text)
        except (json.JSONDecodeError, TypeError):
            logger.warning("OpenAI extraction response was not valid JSON")
            return ProviderResponse(_UNPARSABLE_PAYLOAD, raw_response)

        return ProviderResponse(map_model_output(decoded), raw_response)


__all__ = [
    "OpenAIExtractionProvider",
    "OPENAI_PROVIDER_NAME",
    "DEFAULT_OPENAI_MODEL",
    "map_model_output",
]
