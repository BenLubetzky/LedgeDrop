"""The extraction provider boundary (Stage 3, step 9).

A *provider* takes provider-ready content - a :class:`PreparedDocument` from the
preprocessing layer - and returns an invoice payload that the extraction service
validates against :class:`InvoiceExtraction` before anything is persisted. The
rest of the application depends only on this module, never on a provider SDK.

The interface is intentionally tiny:

* :class:`ExtractionProvider` - a ``name`` and one ``async extract`` method.
* :class:`ProviderError` and its subclasses - the failure modes a provider is
  expected to surface (timeout, rate limit, unavailable). ``code`` and
  ``safe_message`` are already client-safe; the composition layer turns them
  into an :class:`~app.services.processing.extraction.service.ExtractionError`.

A provider must never make the malformed / invalid case a raised error - it
returns the payload and the service rejects it, so "the provider answered but
the answer was unusable" and "the provider failed" stay distinguishable.

A provider may optionally return a :class:`ProviderResponse` instead of a bare
payload. Its ``raw_response`` is provider-native data kept only for internal
audit (never returned by a public endpoint; see
:class:`~app.models.extraction.ExtractionAttempt.raw_response`). Providers with
nothing to audit (e.g. the fake) return a bare payload.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from app.schemas.extraction import InvoiceExtraction
from app.services.processing.extraction.preprocessing import PreparedDocument

# What a provider hands back: either the already-built contract or a raw mapping
# (e.g. decoded JSON) for the service to validate.
ProviderPayload = InvoiceExtraction | Mapping[str, Any]

@dataclass(frozen=True)
class ProviderResponse:
    """A validated payload plus optional provider-native audit data."""

    payload: ProviderPayload
    raw_response: Mapping[str, Any] | None = None


ProviderResult = ProviderPayload | ProviderResponse


@runtime_checkable
class ExtractionProvider(Protocol):
    """Structural type for anything that can extract an invoice from prepared input."""

    name: str

    async def extract(self, prepared: PreparedDocument) -> ProviderResult: ...


class ProviderError(Exception):
    """A provider failed to produce a result. ``code`` / ``safe_message`` are safe
    to surface to a client and to store on a failed attempt."""

    code: str = "PROVIDER_ERROR"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.safe_message = message
        if code is not None:
            self.code = code


class ProviderTimeoutError(ProviderError):
    code = "PROVIDER_TIMEOUT"


class ProviderRateLimitError(ProviderError):
    code = "PROVIDER_RATE_LIMITED"


class ProviderUnavailableError(ProviderError):
    code = "PROVIDER_UNAVAILABLE"


__all__ = [
    "ProviderPayload",
    "ProviderResponse",
    "ProviderResult",
    "ExtractionProvider",
    "ProviderError",
    "ProviderTimeoutError",
    "ProviderRateLimitError",
    "ProviderUnavailableError",
]
