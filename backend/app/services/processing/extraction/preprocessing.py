"""PDF preprocessing for extraction (Stage 3, step 8).

Turns a *stored* original PDF into provider-ready input:

* resolve and read the PDF through the storage service (never an arbitrary path);
* pull the embedded text layer out of digital PDFs, page by page;
* decide whether that text is actually useful, or the page is really a scan;
* package everything a future OCR / vision step needs (per-page flags plus the
  untouched PDF bytes) without rendering anything here.

It is deliberately **independent of any AI provider** and of the extraction
service: it takes a :class:`Document` and a storage handle, returns a
:class:`PreparedDocument`, and never writes. Page rasterization for OCR is left
to the provider adapter (it needs a rendering dependency this project has not
taken on yet); this layer only *identifies* the pages that will need it.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from enum import Enum
from functools import partial
from io import BytesIO

import anyio
from pypdf import PdfReader

from app.core.config import settings
from app.models.document import Document
from app.services.storage import LocalFileStorage, StorageError

logger = logging.getLogger("app.extraction.preprocessing")

_PDF_MAGIC = b"%PDF-"

# A page whose normalized embedded text is shorter than this is treated as
# having no useful text layer - i.e. a scanned image page that will need OCR.
# Real invoice pages carry hundreds of characters; the margin here is wide.
MIN_USEFUL_CHARS_PER_PAGE = 16

_WS_RUN = re.compile(r"[ \t ]+")
_BLANK_LINES = re.compile(r"\n{3,}")


class PreprocessingErrorCode(str, Enum):
    PDF_UNAVAILABLE = "PDF_UNAVAILABLE"  # storage has no readable file for the record
    PDF_UNREADABLE = "PDF_UNREADABLE"  # bytes are present but not a parseable PDF
    TOO_MANY_PAGES = "TOO_MANY_PAGES"  # exceeds the configured page limit


class PreprocessingError(Exception):
    """A stored PDF could not be prepared. ``code`` is a stable machine string."""

    def __init__(self, code: PreprocessingErrorCode, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


class TextLayer(str, Enum):
    """How much usable embedded text the PDF has."""

    DIGITAL = "DIGITAL"  # every page has a usable text layer
    PARTIAL = "PARTIAL"  # some pages have text, some will need OCR
    ABSENT = "ABSENT"  # no usable text on any page - a full scan


@dataclass(frozen=True)
class PreparedPage:
    number: int  # 1-based
    text: str  # normalized embedded text ("" if none)
    char_count: int
    has_text_layer: bool

    @property
    def needs_ocr(self) -> bool:
        return not self.has_text_layer


@dataclass(frozen=True)
class PreparedDocument:
    document_id: uuid.UUID
    page_count: int
    pages: tuple[PreparedPage, ...]
    text_layer: TextLayer
    text: str  # all page text joined by form feeds, in page order
    char_count: int
    pdf_bytes: bytes  # the original bytes, untouched - for a future OCR/vision renderer

    @property
    def needs_ocr(self) -> bool:
        """True unless every page already has a usable text layer."""
        return self.text_layer is not TextLayer.DIGITAL

    @property
    def ocr_page_numbers(self) -> tuple[int, ...]:
        return tuple(p.number for p in self.pages if p.needs_ocr)


def _normalize(raw: str) -> str:
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    text = _WS_RUN.sub(" ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    text = _BLANK_LINES.sub("\n\n", text)
    return text.strip()


def _classify(pages: tuple[PreparedPage, ...]) -> TextLayer:
    with_text = sum(1 for p in pages if p.has_text_layer)
    if with_text == 0:
        return TextLayer.ABSENT
    if with_text == len(pages):
        return TextLayer.DIGITAL
    return TextLayer.PARTIAL


def _prepare_bytes(
    data: bytes, *, document_id: uuid.UUID, max_pages: int
) -> PreparedDocument:
    """Parse ``data`` and build the prepared representation. CPU-bound; run in a
    worker thread."""
    if not data.startswith(_PDF_MAGIC):
        raise PreprocessingError(
            PreprocessingErrorCode.PDF_UNREADABLE, "The stored file is not a PDF."
        )
    try:
        reader = PdfReader(BytesIO(data), strict=False)
        page_count = len(reader.pages)
    except Exception as exc:  # pypdf raises assorted error types on malformed input
        raise PreprocessingError(
            PreprocessingErrorCode.PDF_UNREADABLE, "The stored PDF could not be parsed."
        ) from exc

    if page_count < 1:
        raise PreprocessingError(
            PreprocessingErrorCode.PDF_UNREADABLE, "The stored PDF contains no pages."
        )
    if page_count > max_pages:
        raise PreprocessingError(
            PreprocessingErrorCode.TOO_MANY_PAGES,
            f"The PDF has {page_count} pages; the maximum is {max_pages}.",
        )

    pages: list[PreparedPage] = []
    for index, page in enumerate(reader.pages, start=1):
        try:
            extracted = page.extract_text() or ""
        except Exception:  # pypdf raises assorted types on odd content streams
            logger.warning(
                "text extraction failed for document %s page %d", document_id, index
            )
            extracted = ""
        normalized = _normalize(extracted)
        char_count = len(normalized)
        pages.append(
            PreparedPage(
                number=index,
                text=normalized,
                char_count=char_count,
                has_text_layer=char_count >= MIN_USEFUL_CHARS_PER_PAGE,
            )
        )

    page_tuple = tuple(pages)
    return PreparedDocument(
        document_id=document_id,
        page_count=page_count,
        pages=page_tuple,
        text_layer=_classify(page_tuple),
        text="\f".join(p.text for p in page_tuple),
        char_count=sum(p.char_count for p in page_tuple),
        pdf_bytes=data,
    )


async def prepare_document(
    document: Document,
    storage: LocalFileStorage,
    *,
    max_pages: int | None = None,
) -> PreparedDocument:
    """Resolve, read, and prepare the stored PDF for ``document``.

    Raises :class:`PreprocessingError` with a ``PDF_UNAVAILABLE`` /
    ``PDF_UNREADABLE`` / ``TOO_MANY_PAGES`` code. The stored file is only read.
    """
    limit = settings.max_pdf_pages if max_pages is None else max_pages
    try:
        path = await storage.path_for(document.file_location)
    except StorageError as exc:
        raise PreprocessingError(
            PreprocessingErrorCode.PDF_UNAVAILABLE,
            "The stored PDF for this document is unavailable.",
        ) from exc

    try:
        data = await anyio.to_thread.run_sync(path.read_bytes)
    except OSError as exc:  # pragma: no cover - race between path_for and read
        raise PreprocessingError(
            PreprocessingErrorCode.PDF_UNAVAILABLE,
            "The stored PDF for this document could not be read.",
        ) from exc

    return await anyio.to_thread.run_sync(
        partial(
            _prepare_bytes,
            data,
            document_id=document.document_id,
            max_pages=limit,
        )
    )


__all__ = [
    "MIN_USEFUL_CHARS_PER_PAGE",
    "PreprocessingError",
    "PreprocessingErrorCode",
    "TextLayer",
    "PreparedPage",
    "PreparedDocument",
    "prepare_document",
]
