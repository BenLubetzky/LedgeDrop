"""Invoice extraction subsystem (Stage 3).

Converts a stored PDF into schema-constrained invoice data with per-field
confidence. Provider-specific behaviour stays behind :class:`ExtractionProvider`;
nothing here depends on a provider SDK.
"""

from app.services.processing.extraction.fake import (
    FAKE_PROVIDER_MODEL,
    FAKE_PROVIDER_NAME,
    FakeBehavior,
    FakeExtractionProvider,
)
from app.services.processing.extraction.preprocessing import (
    PreparedDocument,
    PreparedPage,
    PreprocessingError,
    PreprocessingErrorCode,
    TextLayer,
    prepare_document,
)
from app.services.processing.extraction.provider import (
    ExtractionProvider,
    ProviderError,
    ProviderPayload,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from app.services.processing.extraction.repository import ExtractionRepository
from app.services.processing.extraction.service import (
    ExtractionError,
    ExtractionService,
    ResultProducer,
)

__all__ = [
    "ExtractionRepository",
    "ExtractionService",
    "ExtractionError",
    "ResultProducer",
    "prepare_document",
    "PreparedDocument",
    "PreparedPage",
    "PreprocessingError",
    "PreprocessingErrorCode",
    "TextLayer",
    "ExtractionProvider",
    "ProviderPayload",
    "ProviderError",
    "ProviderTimeoutError",
    "ProviderRateLimitError",
    "ProviderUnavailableError",
    "FakeExtractionProvider",
    "FakeBehavior",
    "FAKE_PROVIDER_NAME",
    "FAKE_PROVIDER_MODEL",
]
