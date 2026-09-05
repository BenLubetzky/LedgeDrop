"""Shared FastAPI dependencies.

``get_db`` is re-exported from :mod:`app.database.session` unchanged so route
modules have a single import site for dependencies. It must not be wrapped in
another async generator here - that would swallow its commit/rollback handling.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.database.session import get_db
from app.models.document import Document
from app.services.processing.extraction import (
    ExtractionError,
    ExtractionService,
    ResultProducer,
)
from app.services.processing.extraction.fake import FakeExtractionProvider
from app.services.processing.extraction.openai_provider import OpenAIExtractionProvider
from app.services.processing.extraction.preprocessing import (
    PreprocessingError,
    prepare_document,
)
from app.services.processing.extraction.provider import ExtractionProvider, ProviderError
from app.services.processing.normalization import NormalizationService
from app.services.processing.pipeline import ProcessingPipeline
from app.services.processing.validation import ValidationService
from app.services.storage import LocalFileStorage

__all__ = [
    "get_db",
    "get_storage",
    "get_extraction_service",
    "get_extractor",
    "get_prepared_result_producer",
    "get_normalization_service",
    "get_validation_service",
    "get_pipeline",
]

_storage = LocalFileStorage(settings.upload_directory)


def get_storage() -> LocalFileStorage:
    """Return the process-wide local file storage service."""
    return _storage


def get_extraction_service(
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ExtractionService:
    """Build an :class:`ExtractionService` bound to the request's session."""
    return ExtractionService(db)


def get_normalization_service(
    db: Annotated[AsyncSession, Depends(get_db)],
) -> NormalizationService:
    """Build a :class:`NormalizationService` bound to the request's session.

    Normalization is fully deterministic and offline, so - unlike extraction -
    there is no provider to inject.
    """
    return NormalizationService(db)


def get_validation_service(
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ValidationService:
    """Build a :class:`ValidationService` bound to the request's session.

    Validation is fully deterministic and offline, so - like normalization -
    there is no provider to inject.
    """
    return ValidationService(db)


def get_pipeline(
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ProcessingPipeline:
    """Build the composed extraction+normalization+validation pipeline for this
    request.

    It builds the same per-stage services a caller would use directly, bound to
    the request's session - the pipeline only composes them.
    """
    return ProcessingPipeline(db)


def _build_extractor() -> ExtractionProvider:
    """Build the extraction provider the endpoints run on, per
    ``EXTRACTION_PROVIDER``.

    ``fake`` (default) is the deterministic offline double; ``openai`` calls
    GPT-5-mini for real (see ``docs/provider-selection.md``). Built once at
    import time - like ``_storage`` - so a real provider reuses one HTTP client
    across requests instead of opening one per call.
    """
    if settings.extraction_provider == "openai":
        return OpenAIExtractionProvider(
            api_key=settings.openai_api_key.get_secret_value(),
            model=settings.openai_model,
            timeout_seconds=settings.extraction_provider_timeout_seconds,
        )
    return FakeExtractionProvider()


_extractor = _build_extractor()


def get_extractor() -> ExtractionProvider:
    """The extraction provider the endpoints run on.

    Tests that need failure behaviour override this with a
    ``FakeExtractionProvider(FakeBehavior.<...>)``.
    """
    return _extractor


def get_prepared_result_producer(
    storage: Annotated[LocalFileStorage, Depends(get_storage)],
    provider: Annotated[ExtractionProvider, Depends(get_extractor)],
) -> ResultProducer:
    """Compose stored-PDF preprocessing with the configured provider.

    Preprocessing and provider failures both become an
    :class:`ExtractionError` carrying a client-safe code/message, so the
    extraction service records a clean ``FAILED`` attempt instead of a 500.
    """

    async def produce(document: Document) -> Any:
        try:
            prepared = await prepare_document(document, storage)
        except PreprocessingError as exc:
            raise ExtractionError(exc.message, code=exc.code.value) from exc
        try:
            return await provider.extract(prepared)
        except ProviderError as exc:
            raise ExtractionError(exc.safe_message, code=exc.code) from exc

    return produce
