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
from app.services.processing.extraction.preprocessing import (
    PreprocessingError,
    prepare_document,
)
from app.services.processing.extraction.provider import ExtractionProvider, ProviderError
from app.services.storage import LocalFileStorage

__all__ = [
    "get_db",
    "get_storage",
    "get_extraction_service",
    "get_extractor",
    "get_prepared_result_producer",
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


def get_extractor() -> ExtractionProvider:
    """The extraction provider the endpoints run on.

    The deterministic offline fake until a real provider is integrated
    (Stage 3, steps 11-12). Tests that need failure behaviour override this with
    a ``FakeExtractionProvider(FakeBehavior.<...>)``.
    """
    return FakeExtractionProvider()


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
