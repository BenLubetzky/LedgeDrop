# LedgerDrop example invoices

This folder contains 10 PDFs for manual testing. Files 01-03 and 07-10 are
synthetic and clearly marked as test documents. Files 04-06 are unchanged
public examples downloaded from the internet so the extractor sees layouts
and document styles that were not generated specifically for LedgerDrop.

## Important runtime note

LedgerDrop's default `EXTRACTION_PROVIDER=fake` does not read invoice content.
It returns the same deterministic valid payload for every uploaded PDF. To make
the expected outcomes below meaningful, configure the backend with
`EXTRACTION_PROVIDER=openai` and a valid `OPENAI_API_KEY`, restart it, then run
the PDFs through the pipeline. AI extraction can vary, so "expected" is a test
hypothesis rather than a guaranteed assertion.

The current decision engine has only `ACCEPTED` and `NEEDS_REVIEW`. It does not
automatically reject a structurally valid PDF. The `not acceptable` examples
should be routed to `NEEDS_REVIEW`, where a human reviewer can reject them.

## Test set

| # | File | Origin | Intended result | Why |
|---|---|---|---|---|
| 01 | `01-accepted-simple-consulting.pdf` | Synthetic | `ACCEPTED` | One clear line item, all five critical fields, current dates, and reconciled totals. |
| 02 | `02-accepted-standard-products.pdf` | Synthetic | `ACCEPTED` | Three line items with consistent quantity, price, subtotal, tax, and total. |
| 03 | `03-accepted-complex-multipage.pdf` | Synthetic | `ACCEPTED` | Fourteen reconciled line items plus a second supporting-detail page. |
| 04 | `04-internet-simple-sample.pdf` | Internet | Likely `NEEDS_REVIEW` | Very sparse layout; it lacks an invoice number and named vendor, testing missing critical fields. |
| 05 | `05-internet-payex-complex.pdf` | Internet | `NEEDS_REVIEW` | Dense, image-heavy/watermarked Malaysian sample; its date is older than 10 years at the current test date and its total is above the default high-value threshold. |
| 06 | `06-internet-dc-health-template.pdf` | Internet | `NEEDS_REVIEW`, then reject | An unfilled government invoice template with placeholders instead of actual invoice data. |
| 07 | `07-review-high-value.pdf` | Synthetic | `NEEDS_REVIEW` | Reconciles correctly, but EUR 15,375 exceeds the EUR 10,000 high-value threshold. |
| 08 | `08-review-inconsistent-totals.pdf` | Synthetic | `NEEDS_REVIEW` | Subtotal + tax is GBP 2,400, while the displayed total is GBP 2,525. |
| 09 | `09-not-acceptable-missing-critical-fields.pdf` | Synthetic | `NEEDS_REVIEW`, then reject | Service note missing invoice number, invoice date, currency, and total. |
| 10 | `10-not-acceptable-future-date-and-bad-math.pdf` | Synthetic | `NEEDS_REVIEW`, then reject | Future date, due date before invoice date, line-item mismatch, and totals mismatch. |

## Internet sources

- 04: Freelancer CDN, `https://cdn6.f-cdn.com/files/download/259265429/original_invoice_sample.pdf`
- 05: Payex sample invoice, `https://payex.io/wp-content/uploads/2024/01/sample-invoice.pdf`
- 06: District of Columbia Health invoice template, `https://dchealth.dc.gov/sites/default/files/dc/sites/doh/publication/attachments/FY10_Invoice_Template.pdf`

Downloaded on 2026-09-07. The external files are retained unchanged and are
included only as public test fixtures; consult the source sites for their terms.

## Suggested run order

Upload 01, 02, and 03 first to establish clean accepted cases. Then upload
07-10 to exercise review reasons. Upload 04-06 last because their real-world
layouts are intentionally less predictable. Do not upload the same accepted
invoice twice unless you want to test the probable-duplicate rule.
