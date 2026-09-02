"""Document upload and retrieval routes.

Stage 2 endpoints:

* ``POST   /documents``                  upload + validate + store + record
* ``GET    /documents``                  list metadata, newest first
* ``GET    /documents/{document_id}``     one document's metadata
* ``GET    /documents/{document_id}/file`` stream the stored PDF
* ``DELETE /documents/{document_id}``     remove the record and stored PDF

Internal filesystem paths (``file_location``) and the file hash are never part of
any response.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Sequence
from functools import partial
from typing import Annotated

import anyio
from fastapi import APIRouter, Depends, File, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, get_storage
from app.core.config import settings
from app.core.errors import (
    BadRequestError,
    NotFoundError,
    PayloadTooLargeError,
    UnprocessableEntityError,
    UnsupportedMediaTypeError,
)
from app.models.document import Document, DocumentStatus
from app.schemas.document import DocumentRead
from app.services.pdf import NOT_A_PDF, PdfValidationError, inspect_pdf
from app.services.storage import LocalFileStorage, StorageError

router = APIRouter(prefix="/documents", tags=["documents"])

_READ_CHUNK = 1024 * 1024  # 1 MiB
_MAX_ORIGINAL_FILENAME_LENGTH = 512


async def _read_within_limit(upload: UploadFile, limit_bytes: int) -> bytes:
    """Read the whole upload into memory, aborting if it exceeds ``limit_bytes``.

    The size is measured from the bytes actually received - the client-supplied
    ``Content-Length`` / part size is never trusted on its own.
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await upload.read(_READ_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > limit_bytes:
            raise PayloadTooLargeError(
                f"The file exceeds the {settings.max_file_size_mb} MB limit.",
                code="FILE_TOO_LARGE",
            )
        chunks.append(chunk)
    return b"".join(chunks)


@router.post(
    "",
    response_model=DocumentRead,
    status_code=status.HTTP_201_CREATED,
    summary="Upload and store one PDF",
)
async def upload_document(
    db: Annotated[AsyncSession, Depends(get_db)],
    storage: Annotated[LocalFileStorage, Depends(get_storage)],
    file: Annotated[UploadFile, File(description="A single PDF file, 20 MB / 10 pages max.")],
) -> Document:
    filename = (file.filename or "").strip()
    if not filename:
        raise BadRequestError("A file must be supplied.", code="FILE_REQUIRED")
    if len(filename) > _MAX_ORIGINAL_FILENAME_LENGTH:
        raise BadRequestError(
            f"The filename must be {_MAX_ORIGINAL_FILENAME_LENGTH} characters or fewer.",
            code="FILENAME_TOO_LONG",
        )
    if not filename.lower().endswith(".pdf"):
        raise UnsupportedMediaTypeError(
            "The file must have a .pdf extension.", code="NOT_A_PDF"
        )

    data = await _read_within_limit(file, settings.max_file_size_bytes)
    if not data:
        raise BadRequestError("The uploaded file is empty.", code="EMPTY_FILE")

    try:
        inspection = await anyio.to_thread.run_sync(
            partial(inspect_pdf, data, max_pages=settings.max_pdf_pages)
        )
    except PdfValidationError as exc:
        error_cls = UnsupportedMediaTypeError if exc.code == NOT_A_PDF else UnprocessableEntityError
        raise error_cls(exc.message, code=exc.code) from exc

    file_hash = hashlib.sha256(data).hexdigest()
    document_id = uuid.uuid4()

    # Write the file first, then finish the database transaction before returning
    # a response. If persistence fails at either flush or commit, compensate by
    # removing the stored file so the upload leaves no orphaned resource.
    stored = await storage.save_bytes(document_id, data)

    document = Document(
        document_id=document_id,
        original_filename=filename,
        file_location=stored.location,
        file_hash=file_hash,
        file_size_bytes=len(data),
        page_count=inspection.page_count,
        status=DocumentStatus.UPLOADED,
    )
    db.add(document)
    try:
        await db.flush()
        await db.commit()
    except Exception:
        await db.rollback()
        await storage.delete(document_id)
        raise

    return document


async def _get_or_404(db: AsyncSession, document_id: uuid.UUID) -> Document:
    document = await db.get(Document, document_id)
    if document is None:
        raise NotFoundError("No document exists with that ID.", code="DOCUMENT_NOT_FOUND")
    return document


@router.get(
    "",
    response_model=list[DocumentRead],
    summary="List uploaded documents, newest first",
)
async def list_documents(
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Sequence[Document]:
    # document_id is a secondary key so ordering is stable when two uploads share
    # an uploaded_at timestamp.
    result = await db.execute(
        select(Document).order_by(
            Document.uploaded_at.desc(), Document.document_id.desc()
        )
    )
    return result.scalars().all()


@router.get(
    "/{document_id}",
    response_model=DocumentRead,
    summary="Get one document's metadata",
)
async def get_document(
    document_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Document:
    return await _get_or_404(db, document_id)


@router.delete(
    "/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a document and its stored PDF",
)
async def delete_document(
    document_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    storage: Annotated[LocalFileStorage, Depends(get_storage)],
) -> None:
    document = await _get_or_404(db, document_id)

    # Commit the database deletion first. A storage cleanup failure can leave an
    # inaccessible orphan to clean up later, while deleting the file first could
    # leave a live document record pointing at a file that no longer exists.
    await db.delete(document)
    await db.commit()
    await storage.delete(document_id)


@router.get(
    "/{document_id}/file",
    summary="Stream the stored PDF",
    response_class=FileResponse,
)
async def download_document_file(
    document_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    storage: Annotated[LocalFileStorage, Depends(get_storage)],
) -> FileResponse:
    document = await _get_or_404(db, document_id)
    try:
        path = await storage.path_for(document.file_location)
    except StorageError as exc:
        # The record exists but the stored file is missing or unreadable. Do not
        # leak the resolved path or the underlying reason.
        raise NotFoundError(
            "The stored file for this document is unavailable.",
            code="FILE_NOT_FOUND",
        ) from exc

    return FileResponse(
        path,
        media_type="application/pdf",
        filename=document.original_filename,
        content_disposition_type="inline",
    )
