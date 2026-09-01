"""PDF inspection for upload validation.

This module answers two questions about a blob of bytes claiming to be a PDF:
"can it actually be opened as a PDF?" and "how many pages does it have?". It is
deliberately not part of ``services/processing`` - it gates uploads, it does not
read invoice content.

It raises :class:`PdfValidationError` (a plain domain exception) rather than an
HTTP error, so it stays independent of the web layer. The route translates the
error code into a response.
"""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO

from pypdf import PdfReader

PDF_MAGIC = b"%PDF-"

# PdfValidationError.code values
NOT_A_PDF = "NOT_A_PDF"
PDF_UNREADABLE = "PDF_UNREADABLE"
TOO_MANY_PAGES = "TOO_MANY_PAGES"


class PdfValidationError(Exception):
    """A PDF failed structural validation. ``code`` is a stable machine string."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True)
class PdfInspection:
    page_count: int


def inspect_pdf(data: bytes, *, max_pages: int) -> PdfInspection:
    """Validate ``data`` as a readable PDF within ``max_pages``.

    Raises :class:`PdfValidationError` with code:

    * ``NOT_A_PDF``        - missing the ``%PDF-`` signature
    * ``PDF_UNREADABLE``   - signature present but the structure cannot be parsed
                             (truncated, corrupt, or password-protected)
    * ``TOO_MANY_PAGES``   - parsed successfully but exceeds ``max_pages``
    """
    if not data.startswith(PDF_MAGIC):
        raise PdfValidationError(NOT_A_PDF, "The file is not a PDF.")

    try:
        reader = PdfReader(BytesIO(data), strict=False)
        page_count = len(reader.pages)
    except Exception as exc:  # pypdf raises assorted error types on malformed input
        raise PdfValidationError(PDF_UNREADABLE, "The PDF could not be read.") from exc

    if page_count < 1:
        raise PdfValidationError(PDF_UNREADABLE, "The PDF contains no pages.")

    if page_count > max_pages:
        raise PdfValidationError(
            TOO_MANY_PAGES,
            f"The PDF has {page_count} pages; the maximum is {max_pages}.",
        )

    return PdfInspection(page_count=page_count)
