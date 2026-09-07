from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    PageBreak,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)


ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "example invoices"
OUT.mkdir(parents=True, exist_ok=True)

NAVY = colors.HexColor("#152238")
BLUE = colors.HexColor("#2F6FED")
PALE = colors.HexColor("#EEF3FF")
INK = colors.HexColor("#263241")
MUTED = colors.HexColor("#667085")
RED = colors.HexColor("#B42318")

styles = getSampleStyleSheet()
styles.add(ParagraphStyle(name="InvoiceTitle", parent=styles["Title"], fontName="Helvetica-Bold", fontSize=25, leading=29, textColor=NAVY, alignment=TA_RIGHT))
styles.add(ParagraphStyle(name="Small", parent=styles["Normal"], fontSize=8.5, leading=11, textColor=MUTED))
styles.add(ParagraphStyle(name="Body2", parent=styles["Normal"], fontSize=9.5, leading=13, textColor=INK))
styles.add(ParagraphStyle(name="Right", parent=styles["Body2"], alignment=TA_RIGHT))
styles.add(ParagraphStyle(name="Section", parent=styles["Heading2"], fontName="Helvetica-Bold", fontSize=9, leading=12, textColor=BLUE, spaceAfter=4))


def money(value: Decimal, currency: str) -> str:
    return f"{currency} {value:,.2f}"


def footer(canvas, doc):
    canvas.saveState()
    canvas.setStrokeColor(colors.HexColor("#D0D5DD"))
    canvas.line(18 * mm, 14 * mm, 192 * mm, 14 * mm)
    canvas.setFont("Helvetica", 7.5)
    canvas.setFillColor(MUTED)
    canvas.drawString(18 * mm, 9 * mm, "Synthetic LedgerDrop test document - not a demand for payment")
    canvas.drawRightString(192 * mm, 9 * mm, f"Page {doc.page}")
    canvas.restoreState()


def build_invoice(filename: str, *, vendor: str, vendor_address: str, tax_id: str,
                  customer: str, invoice_no: str | None, invoice_date: str | None,
                  due_date: str | None, currency: str | None, items: list[tuple[str, str, str, str]],
                  subtotal: str | None, tax: str | None, total: str | None,
                  title: str = "INVOICE", note: str | None = None,
                  extra_page: list[str] | None = None):
    path = OUT / filename
    doc = BaseDocTemplate(str(path), pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm,
                          topMargin=16 * mm, bottomMargin=20 * mm,
                          title=title, author=vendor, subject="LedgerDrop example invoice")
    frame = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height, id="main")
    doc.addPageTemplates(PageTemplate(id="invoice", frames=[frame], onPage=footer))
    story = []
    header = Table([
        [Paragraph(f"<b>{vendor}</b><br/><font size='8' color='#667085'>{vendor_address}<br/>Tax ID: {tax_id or 'Not supplied'}</font>", styles["Body2"]),
         Paragraph(title, styles["InvoiceTitle"])],
    ], colWidths=[105 * mm, 69 * mm])
    header.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LINEBELOW", (0, 0), (-1, -1), 1.2, BLUE), ("BOTTOMPADDING", (0, 0), (-1, -1), 10)]))
    story += [header, Spacer(1, 9 * mm)]

    meta_left = f"<b>BILL TO</b><br/>{customer}"
    meta_right = (
        f"<b>Invoice number:</b> {invoice_no or 'Not provided'}<br/>"
        f"<b>Invoice date:</b> {invoice_date or 'Not provided'}<br/>"
        f"<b>Due date:</b> {due_date or 'Not provided'}<br/>"
        f"<b>Currency:</b> {currency or 'Not provided'}"
    )
    meta = Table([[Paragraph(meta_left, styles["Body2"]), Paragraph(meta_right, styles["Right"])]], colWidths=[95 * mm, 79 * mm])
    meta.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("BACKGROUND", (0, 0), (-1, -1), PALE), ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#D6E2FF")), ("LEFTPADDING", (0, 0), (-1, -1), 9), ("RIGHTPADDING", (0, 0), (-1, -1), 9), ("TOPPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 8)]))
    story += [meta, Spacer(1, 8 * mm)]

    data = [["Description", "Qty", "Unit price", "Line total"]] + [list(row) for row in items]
    table = Table(data, colWidths=[94 * mm, 18 * mm, 30 * mm, 32 * mm], repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), NAVY), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"), ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
        ("FONTSIZE", (0, 0), (-1, -1), 8.5), ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "TOP"), ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F8FAFC")]),
        ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#D0D5DD")),
        ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    story += [table, Spacer(1, 6 * mm)]

    totals = []
    if subtotal is not None:
        totals.append(["Subtotal", subtotal])
    if tax is not None:
        totals.append(["Tax", tax])
    if total is not None:
        totals.append(["TOTAL", total])
    if totals:
        total_table = Table(totals, colWidths=[32 * mm, 38 * mm], hAlign="RIGHT")
        total_table.setStyle(TableStyle([("ALIGN", (0, 0), (-1, -1), "RIGHT"), ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"), ("TEXTCOLOR", (0, -1), (-1, -1), NAVY), ("LINEABOVE", (0, -1), (-1, -1), 1, BLUE), ("FONTSIZE", (0, 0), (-1, -1), 9.5), ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]))
        story += [total_table]
    if note:
        story += [Spacer(1, 8 * mm), Paragraph("NOTES", styles["Section"]), Paragraph(note, styles["Body2"])]
    if extra_page:
        story += [PageBreak(), Paragraph("SUPPORTING DETAIL", styles["InvoiceTitle"]), Spacer(1, 8 * mm)]
        for text in extra_page:
            story += [Paragraph(text, styles["Body2"]), Spacer(1, 4 * mm)]
    doc.build(story)


build_invoice(
    "01-accepted-simple-consulting.pdf", vendor="Brightline Consulting Ltd", vendor_address="18 Market Lane, Lisbon 1200-109, Portugal",
    tax_id="PT509123456", customer="LedgerDrop Demo Customer, 5 Innovation Way, Lisbon",
    invoice_no="BL-2026-091", invoice_date="2026-09-01", due_date="2026-09-30", currency="EUR",
    items=[("Process mapping workshop", "1", "800.00", "800.00")], subtotal="EUR 800.00", tax="EUR 184.00", total="EUR 984.00",
    note="Payment due by bank transfer within 30 days. Reference BL-2026-091.")

build_invoice(
    "02-accepted-standard-products.pdf", vendor="Northstar Office Supply GmbH", vendor_address="42 Hafenstrasse, Hamburg 20457, Germany",
    tax_id="DE294817365", customer="LedgerDrop Demo Customer, 5 Innovation Way, Lisbon",
    invoice_no="NS-88421", invoice_date="2026-08-28", due_date="2026-09-27", currency="EUR",
    items=[("Ergonomic keyboard", "4", "79.50", "318.00"), ("USB-C docking station", "2", "145.00", "290.00"), ("Monitor arm", "3", "68.00", "204.00")],
    subtotal="EUR 812.00", tax="EUR 186.76", total="EUR 998.76", note="All amounts are in EUR. VAT rate: 23%.")

complex_items = [(f"Cloud migration work package {i:02d}", str((i % 4) + 1), f"{Decimal('120') + i * 7:.2f}", f"{((i % 4) + 1) * (Decimal('120') + i * 7):.2f}") for i in range(1, 15)]
complex_subtotal = sum(Decimal(row[3]) for row in complex_items)
complex_tax = (complex_subtotal * Decimal("0.23")).quantize(Decimal("0.01"))
build_invoice(
    "03-accepted-complex-multipage.pdf", vendor="Atlas Systems Integration SA", vendor_address="220 Avenida Central, Porto 4000-114, Portugal",
    tax_id="PT503987654", customer="LedgerDrop Demo Customer, 5 Innovation Way, Lisbon",
    invoice_no="ASI-2026-4472", invoice_date="2026-08-15", due_date="2026-10-14", currency="EUR",
    items=complex_items, subtotal=money(complex_subtotal, "EUR"), tax=money(complex_tax, "EUR"), total=money(complex_subtotal + complex_tax, "EUR"),
    note="Milestone invoice under statement of work SOW-2026-14. Fourteen work packages are itemized.",
    extra_page=["Project: Regional cloud migration and data reconciliation.", "Acceptance reference: CR-2026-0815-A.", "This second page intentionally tests multi-page extraction while all critical invoice values remain on page one."])

build_invoice(
    "07-review-high-value.pdf", vendor="Summit Infrastructure BV", vendor_address="91 Delta Park, Rotterdam 3011 AA, Netherlands",
    tax_id="NL861234567B01", customer="LedgerDrop Demo Customer, 5 Innovation Way, Lisbon",
    invoice_no="SI-2026-778", invoice_date="2026-08-20", due_date="2026-09-19", currency="EUR",
    items=[("Network modernization milestone", "1", "12500.00", "12500.00")], subtotal="EUR 12,500.00", tax="EUR 2,875.00", total="EUR 15,375.00",
    note="Expected NEEDS_REVIEW: total exceeds LedgerDrop's EUR 10,000 high-value threshold.")

build_invoice(
    "08-review-inconsistent-totals.pdf", vendor="Harbor Creative Studio Ltd", vendor_address="7 Dock Street, Liverpool L1 8JQ, United Kingdom",
    tax_id="GB218765432", customer="LedgerDrop Demo Customer, 5 Innovation Way, Lisbon",
    invoice_no="HCS-260812", invoice_date="2026-08-12", due_date="2026-09-11", currency="GBP",
    items=[("Brand strategy", "1", "1200.00", "1200.00"), ("Design production", "2", "400.00", "800.00")],
    subtotal="GBP 2,000.00", tax="GBP 400.00", total="GBP 2,525.00",
    note="Expected NEEDS_REVIEW: subtotal plus tax is 2,400.00, but the stated total is 2,525.00.")

build_invoice(
    "09-not-acceptable-missing-critical-fields.pdf", vendor="Walk-In Repair Desk", vendor_address="Address not supplied",
    tax_id="", customer="LedgerDrop Demo Customer",
    invoice_no=None, invoice_date=None, due_date=None, currency=None,
    items=[("Repair service", "1", "250.00", "250.00")], subtotal=None, tax=None, total=None,
    title="SERVICE NOTE", note="Intentionally incomplete: invoice number, invoice date, currency, total and vendor tax details are absent.")

build_invoice(
    "10-not-acceptable-future-date-and-bad-math.pdf", vendor="Tomorrow Logistics LLC", vendor_address="900 Horizon Road, Austin, TX 78701, USA",
    tax_id="US-74-9918821", customer="LedgerDrop Demo Customer, 5 Innovation Way, Lisbon",
    invoice_no="TL-2099-001", invoice_date="2099-12-20", due_date="2099-01-10", currency="USD",
    items=[("Freight service", "3", "300.00", "700.00")], subtotal="USD 700.00", tax="USD 70.00", total="USD 1,000.00",
    note="Intentionally invalid: future invoice date, due date before invoice date, line-item multiplication mismatch, and totals mismatch.")

print(f"Generated 7 PDFs in {OUT}")
