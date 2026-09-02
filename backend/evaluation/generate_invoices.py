"""Deterministically (re)generate the evaluation invoice PDFs.

Run ``python -m evaluation.generate_invoices`` from ``backend/`` to rewrite
``evaluation/invoices/``. The digital PDFs render the ground truth from
``expected.json`` verbatim, so the files and the manifest cannot drift; a test
asserts the committed bytes match a fresh run.

The PDFs are assembled by hand (no reportlab dependency): objects are written in
order and the cross-reference table is built from their real byte offsets.
"""

from __future__ import annotations

import zlib
from pathlib import Path

from evaluation.dataset import EvalCase, ExpectedInvoice, INVOICES_DIR, load_cases

_CORRUPT_BYTES = b"%PDF-1.4\n1 0 obj<< /Type /Catalog >>\n%%EOF\x00\x00truncated"

# A deliberately small embedded bitmap alphabet.  The scanned fixture must be
# image-only (so it has no extractable text layer), but it must also contain a
# real, human-readable invoice.  Keeping the glyphs here avoids relying on an
# installed font, imaging library, OCR engine, or network resource.
_GLYPHS = {
    " ": ("00000",) * 7,
    "-": ("00000", "00000", "00000", "11111", "00000", "00000", "00000"),
    ".": ("00000", "00000", "00000", "00000", "00000", "01100", "01100"),
    ":": ("00000", "01100", "01100", "00000", "01100", "01100", "00000"),
    "|": ("00100",) * 7,
    "0": ("01110", "10001", "10011", "10101", "11001", "10001", "01110"),
    "1": ("00100", "01100", "00100", "00100", "00100", "00100", "01110"),
    "2": ("01110", "10001", "00001", "00010", "00100", "01000", "11111"),
    "3": ("11110", "00001", "00001", "01110", "00001", "00001", "11110"),
    "4": ("00010", "00110", "01010", "10010", "11111", "00010", "00010"),
    "5": ("11111", "10000", "10000", "11110", "00001", "00001", "11110"),
    "6": ("01110", "10000", "10000", "11110", "10001", "10001", "01110"),
    "7": ("11111", "00001", "00010", "00100", "01000", "01000", "01000"),
    "8": ("01110", "10001", "10001", "01110", "10001", "10001", "01110"),
    "9": ("01110", "10001", "10001", "01111", "00001", "00001", "01110"),
    "A": ("01110", "10001", "10001", "11111", "10001", "10001", "10001"),
    "B": ("11110", "10001", "10001", "11110", "10001", "10001", "11110"),
    "C": ("01111", "10000", "10000", "10000", "10000", "10000", "01111"),
    "D": ("11110", "10001", "10001", "10001", "10001", "10001", "11110"),
    "E": ("11111", "10000", "10000", "11110", "10000", "10000", "11111"),
    "F": ("11111", "10000", "10000", "11110", "10000", "10000", "10000"),
    "G": ("01111", "10000", "10000", "10111", "10001", "10001", "01111"),
    "H": ("10001", "10001", "10001", "11111", "10001", "10001", "10001"),
    "I": ("01110", "00100", "00100", "00100", "00100", "00100", "01110"),
    "J": ("00111", "00010", "00010", "00010", "10010", "10010", "01100"),
    "K": ("10001", "10010", "10100", "11000", "10100", "10010", "10001"),
    "L": ("10000", "10000", "10000", "10000", "10000", "10000", "11111"),
    "M": ("10001", "11011", "10101", "10101", "10001", "10001", "10001"),
    "N": ("10001", "11001", "10101", "10011", "10001", "10001", "10001"),
    "O": ("01110", "10001", "10001", "10001", "10001", "10001", "01110"),
    "P": ("11110", "10001", "10001", "11110", "10000", "10000", "10000"),
    "Q": ("01110", "10001", "10001", "10001", "10101", "10010", "01101"),
    "R": ("11110", "10001", "10001", "11110", "10100", "10010", "10001"),
    "S": ("01111", "10000", "10000", "01110", "00001", "00001", "11110"),
    "T": ("11111", "00100", "00100", "00100", "00100", "00100", "00100"),
    "U": ("10001", "10001", "10001", "10001", "10001", "10001", "01110"),
    "V": ("10001", "10001", "10001", "10001", "10001", "01010", "00100"),
    "W": ("10001", "10001", "10001", "10101", "10101", "10101", "01010"),
    "X": ("10001", "10001", "01010", "00100", "01010", "10001", "10001"),
    "Y": ("10001", "10001", "01010", "00100", "00100", "00100", "00100"),
    "Z": ("11111", "00001", "00010", "00100", "01000", "10000", "11111"),
}


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def build_pdf(pages: list[list[str]]) -> bytes:
    """A minimal multi-page PDF. ``pages`` is a list of pages, each a list of
    text lines. An empty line list produces a page with no text layer."""
    font_obj = 3
    next_obj = 4
    specs: list[tuple[int, int | None, list[str]]] = []
    for lines in pages:
        page_no = next_obj
        next_obj += 1
        content_no: int | None = None
        if lines:
            content_no = next_obj
            next_obj += 1
        specs.append((page_no, content_no, lines))

    kids = " ".join(f"{page_no} 0 R" for page_no, _, _ in specs)
    objects: list[tuple[int, bytes]] = [
        (1, b"<< /Type /Catalog /Pages 2 0 R >>"),
        (2, f"<< /Type /Pages /Kids [{kids}] /Count {len(specs)} >>".encode()),
        (font_obj, b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"),
    ]
    for page_no, content_no, lines in specs:
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
            body = b"BT /F1 11 Tf 14 TL 72 720 Td\n"
            for line in lines:
                body += f"({_escape(line)}) Tj T*\n".encode()
            body += b"ET"
            objects.append(
                (content_no, b"<< /Length %d >>\nstream\n%s\nendstream" % (len(body), body))
            )

    objects.sort()
    out = bytearray(b"%PDF-1.4\n")
    offsets: dict[int, int] = {}
    for num, obj_body in objects:
        offsets[num] = len(out)
        out += f"{num} 0 obj\n".encode() + obj_body + b"\nendobj\n"

    xref_pos = len(out)
    highest = objects[-1][0]
    out += f"xref\n0 {highest + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for num in range(1, highest + 1):
        out += f"{offsets[num]:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {highest + 1} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n"
    ).encode()
    return bytes(out)


def build_image_only_pdf(lines: list[str]) -> bytes:
    """Render uppercase text into pixels and embed that raster as the only page content."""
    width, height, scale = 540, 700, 2
    pixels = bytearray([255]) * (width * height)
    x0, y0, line_step = 24, 28, 24
    for line_number, line in enumerate(lines):
        y = y0 + line_number * line_step
        for char_number, char in enumerate(line.upper()):
            glyph = _GLYPHS.get(char, _GLYPHS[" "])
            x = x0 + char_number * 12
            for row, bits in enumerate(glyph):
                for column, bit in enumerate(bits):
                    if bit == "1":
                        for dy in range(scale):
                            start = (y + row * scale + dy) * width + x + column * scale
                            if x + column * scale + scale <= width:
                                pixels[start : start + scale] = b"\x00" * scale

    image = zlib.compress(bytes(pixels), level=9)
    content = b"q 540 0 0 700 36 46 cm /Im0 Do Q"
    objects = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        3: (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /XObject << /Im0 4 0 R >> >> /Contents 5 0 R >>"
        ),
        4: (
            f"<< /Type /XObject /Subtype /Image /Width {width} /Height {height} "
            f"/ColorSpace /DeviceGray /BitsPerComponent 8 /Filter /FlateDecode "
            f"/Length {len(image)} >>\nstream\n".encode()
            + image
            + b"\nendstream"
        ),
        5: b"<< /Length %d >>\nstream\n%s\nendstream" % (len(content), content),
    }
    out = bytearray(b"%PDF-1.4\n")
    offsets = {}
    for number, body in objects.items():
        offsets[number] = len(out)
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_pos = len(out)
    out += b"xref\n0 6\n0000000000 65535 f \n"
    for number in range(1, 6):
        out += f"{offsets[number]:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n".encode()
    return bytes(out)


def _line_item_lines(invoice: ExpectedInvoice) -> list[str]:
    lines = []
    for item in invoice.line_items:
        lines.append(
            f"  {item.description or '-'} | qty {item.quantity or '-'} | "
            f"unit {item.unit_price or '-'} | line total {item.line_total or '-'}"
        )
    return lines


def _header_lines(invoice: ExpectedInvoice, *, unusual: bool) -> list[str]:
    if unusual:
        return [
            "FABRICATION ORDER / RECHNUNG",
            f"Client .......... {invoice.customer_name}",
            f"Issued by ....... {invoice.vendor_name}  (VAT {invoice.vendor_tax_id})",
            f"Ref {invoice.invoice_number}   dated {invoice.invoice_date}   terms: {invoice.due_date}",
            f"Amounts in {invoice.currency}",
        ]
    header = [
        "INVOICE",
        f"Invoice number: {invoice.invoice_number}",
        f"Invoice date: {invoice.invoice_date}",
    ]
    if invoice.due_date is not None:
        header.append(f"Due date: {invoice.due_date}")
    header.append(f"Vendor: {invoice.vendor_name}")
    if invoice.vendor_tax_id is not None:
        header.append(f"Vendor tax ID: {invoice.vendor_tax_id}")
    if invoice.customer_name is not None:
        header.append(f"Bill to: {invoice.customer_name}")
    header.append(f"Currency: {invoice.currency}")
    return header


def _totals_lines(invoice: ExpectedInvoice) -> list[str]:
    totals = []
    if invoice.subtotal is not None:
        totals.append(f"Subtotal: {invoice.subtotal}")
    if invoice.tax_amount is not None:
        totals.append(f"Tax: {invoice.tax_amount}")
    totals.append(f"Total due: {invoice.total_amount}")
    return totals


def _render(case: EvalCase) -> bytes:
    if case.id == "low_quality":
        return _CORRUPT_BYTES
    if case.id == "not_an_invoice":
        return build_pdf(
            [
                [
                    "Weekly standup - 2026-01-14",
                    "Attendees: Dana, Priya, Marco, Lena",
                    "Done: shipped the upload page; fixed the page-count check.",
                    "Next: wire the extraction endpoint to the fake provider.",
                    "Blockers: none. Next sync 2026-01-21.",
                ]
            ]
        )

    invoice = case.expected
    assert invoice is not None
    if case.id == "scanned_no_text":
        item = invoice.line_items[0]
        return build_image_only_pdf(
            _header_lines(invoice, unusual=False)
            + [
                "Line item:",
                f"Description: {item.description}",
                f"Quantity: {item.quantity}",
                f"Unit price: {item.unit_price}",
                f"Line total: {item.line_total}",
            ]
            + _totals_lines(invoice)
        )

    unusual = case.id == "digital_unusual_layout"
    header = _header_lines(invoice, unusual=unusual)
    items = ["Line items:", *_line_item_lines(invoice)]
    totals = _totals_lines(invoice)

    if case.id == "digital_multi_page":
        return build_pdf([header + totals, ["Line items (continued):", *_line_item_lines(invoice)]])
    return build_pdf([header + items + totals])


def build_all() -> dict[str, bytes]:
    """Return ``{case id: pdf bytes}`` for every case, without writing anything."""
    return {case.id: _render(case) for case in load_cases()}


def write_all(target_dir: Path = INVOICES_DIR) -> list[Path]:
    target_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for case in load_cases():
        path = target_dir / case.path.name
        path.write_bytes(_render(case))
        written.append(path)
    return written


if __name__ == "__main__":  # pragma: no cover
    for path in write_all():
        print(f"wrote {path.relative_to(path.parents[2])}")
