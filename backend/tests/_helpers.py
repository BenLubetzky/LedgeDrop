"""Small shared helpers for the test suite."""

from __future__ import annotations

from io import BytesIO

from pypdf import PdfWriter


def make_pdf(pages: int = 1) -> bytes:
    """Return the bytes of a minimal valid PDF with ``pages`` blank pages."""
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=72, height=72)
    buffer = BytesIO()
    writer.write(buffer)
    return buffer.getvalue()
