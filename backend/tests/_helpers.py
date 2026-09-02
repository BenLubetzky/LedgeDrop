"""Small shared helpers for the test suite."""

from __future__ import annotations

from io import BytesIO

from pypdf import PdfWriter


def make_pdf(pages: int = 1) -> bytes:
    """Return the bytes of a minimal valid PDF with ``pages`` blank pages.

    Blank pages carry no text layer, so this stands in for a scanned document.
    """
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=72, height=72)
    buffer = BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def make_text_pdf(pages: list[str]) -> bytes:
    """Return the bytes of a minimal digital PDF, one page per string in ``pages``.

    Each non-empty string is drawn as a line of text on its page, so
    ``page.extract_text()`` returns it. An empty string yields a page with no
    content stream - a page with no text layer, like a scan.

    The file is assembled by hand (no reportlab dependency): objects are written
    sequentially and the cross-reference table is built from their real byte
    offsets.
    """
    font_obj = 3
    next_obj = 4
    page_specs: list[tuple[int, int | None, str]] = []
    for text in pages:
        page_no = next_obj
        next_obj += 1
        content_no: int | None = None
        if text:
            content_no = next_obj
            next_obj += 1
        page_specs.append((page_no, content_no, text))

    kids = " ".join(f"{page_no} 0 R" for page_no, _, _ in page_specs)
    objects: list[tuple[int, bytes]] = [
        (1, b"<< /Type /Catalog /Pages 2 0 R >>"),
        (2, f"<< /Type /Pages /Kids [{kids}] /Count {len(page_specs)} >>".encode()),
        (font_obj, b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"),
    ]
    for page_no, content_no, text in page_specs:
        contents = f" /Contents {content_no} 0 R" if content_no is not None else ""
        objects.append(
            (
                page_no,
                (
                    "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                    f"/Resources << /Font << /F1 {font_obj} 0 R >> >>{contents} >>"
                ).encode(),
            )
        )
        if content_no is not None:
            escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            stream = f"BT /F1 18 Tf 72 720 Td ({escaped}) Tj ET".encode()
            objects.append(
                (content_no, b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream))
            )

    objects.sort()
    out = bytearray(b"%PDF-1.4\n")
    offsets: dict[int, int] = {}
    for num, body in objects:
        offsets[num] = len(out)
        out += f"{num} 0 obj\n".encode() + body + b"\nendobj\n"

    xref_pos = len(out)
    highest = objects[-1][0]
    out += f"xref\n0 {highest + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for num in range(1, highest + 1):
        out += f"{offsets[num]:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {highest + 1} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n".encode()
    return bytes(out)
