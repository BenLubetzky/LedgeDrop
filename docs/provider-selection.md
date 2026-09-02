# Stage 3 - Step 11: extraction-provider evaluation

## Decision status

**Azure AI Document Intelligence `prebuilt-invoice` is the provisional adapter
target, not yet a validated production selection.** AWS Textract
`AnalyzeExpense` is the comparison candidate. A real provider has not yet been
run against LedgerDrop's evaluation set, so this project must not claim measured
accuracy, latency, cost, or confidence calibration yet.

Vendor documentation establishes feature support, not performance on
LedgerDrop's invoice population. Microsoft explicitly recommends a
representative pilot because accuracy and useful confidence thresholds vary by
scenario.

## Desk-research comparison

| Criterion | Azure Document Intelligence | AWS Textract | General vision LLM |
|---|---|---|---|
| Invoice model | `prebuilt-invoice` | `AnalyzeExpense` | Prompt/schema defined by us |
| Digital and scanned PDFs | Supported | Supported | Model dependent; vision required |
| Structured invoice fields | Native invoice schema | Summary and line-item fields | Structured output can constrain shape |
| Line items | Supported | Supported | Model dependent |
| Provider confidence | `0..1` estimate; some fields can omit it | `0..100` detection confidence | No provider-calibrated per-field score |
| Contract fit | Close; adapter mapping required | Close; adapter mapping required | Direct schema fit, but prompt dependent |
| Latency and cost | Measure in deployment region/tier | Measure in deployment region/tier | Model/tier dependent |
| Privacy and retention | Approve selected region/account configuration | Approve selected region/account configuration | Vendor/account dependent |
| Offline automated tests | Fake provider only | Fake provider only | Fake provider only |

Pricing numbers are deliberately not frozen here. They change by region, tier,
and date and must be captured with a date and billing assumptions during the
live comparison.

## Why Azure is the provisional target

1. Its prebuilt invoice model supports PDFs, OCR, structured invoice fields,
   line items, and field confidence.
2. Its schema is close to `InvoiceExtraction`, limiting adapter-specific logic.
3. It can consume the original PDF, including the image-only evaluation case.
4. It stays isolated behind `ExtractionProvider`; the first adapter does not
   lock the application to Azure.

These are implementation-fit reasons, not empirical proof that Azure is more
accurate than AWS for LedgerDrop.

## Required live bake-off

Run Azure and AWS over the same frozen evaluation-set version. Record:

- provider, model/API version, region, timestamp, and dataset revision;
- every case result, including failures and malformed responses;
- overall, critical-field, and line-item field accuracy;
- exact line-item-count rate and per-category results;
- end-to-end latency per document (median and p95 with enough samples);
- pages/documents billed and resulting observed cost;
- confidence coverage and Brier score;
- privacy/retention configuration enabled for the tested resource.

The current synthetic set is useful for adapter correctness but too small and
clean to justify a production vendor decision. Before final selection, add a
legally usable validation set representative of intended customers, vendors,
scans, languages, layouts, and image quality. Do not tune against the same cases
used for the final comparison.

## Confidence decision

**Until the live bake-off demonstrates useful calibration, application-level
confidence remains `null`.** Provider confidence may be retained only in the
internal raw response for analysis. It must not enter the application contract
or drive escalation merely because it lies between zero and one.

`evaluation.scoring` reports confidence coverage and Brier score (squared error
between confidence and observed correctness). Those metrics are diagnostic. A
useful decision also needs reliability checks across confidence bands and enough
incorrect predictions to reveal overconfidence. If later evidence shows the
scores track correctness, the adapter can persist genuine provider scores;
absent scores stay `null`. Vision-LLM self-reported probabilities also stay
`null` unless separately calibrated.

## Proposed Azure field mapping

| LedgerDrop | Azure invoice field |
|---|---|
| `invoice_number` | `InvoiceId` |
| `invoice_date` | `InvoiceDate` content/raw text |
| `due_date` | `DueDate` content/raw text |
| `vendor_name` | `VendorName` |
| `vendor_tax_id` | `VendorTaxId` |
| `customer_name` | `CustomerName` |
| `currency` | currency code associated with `InvoiceTotal`, when present |
| `subtotal` | `SubTotal` amount |
| `tax_amount` | `TotalTax` amount |
| `total_amount` | `InvoiceTotal` amount |
| line-item fields | corresponding `Items[]` fields |

Dates remain raw strings for the later normalization stage. Missing values and
confidence are `null`; currency is never defaulted. Every mapped payload must
pass `InvoiceExtraction` validation before persistence.

## Configuration boundary

The placeholders in `backend/.env.example` remain inert until the adapter step.
Credentials stay in environment variables or managed identity. Normal automated
tests never call Azure or AWS; live evaluation requires an explicit command and
opt-in credentials.

## Primary sources

- Microsoft: Document Intelligence prebuilt invoice model
  <https://learn.microsoft.com/en-us/azure/ai-services/document-intelligence/prebuilt/invoice>
- Microsoft: accuracy and confidence guidance
  <https://learn.microsoft.com/en-us/azure/ai-services/document-intelligence/concept/accuracy-confidence>
- Microsoft: responsible evaluation guidance
  <https://learn.microsoft.com/en-us/azure/foundry/responsible-ai/document-intelligence/transparency-note>
- Microsoft: service limits
  <https://learn.microsoft.com/en-us/azure/ai-services/document-intelligence/service-limits>
- Microsoft: current pricing
  <https://azure.microsoft.com/en-us/pricing/details/ai-document-intelligence/>
- AWS: AnalyzeExpense API
  <https://docs.aws.amazon.com/textract/latest/APIReference/API_AnalyzeExpense.html>
- AWS: invoice and receipt analysis
  <https://docs.aws.amazon.com/textract/latest/dg/invoices-receipts.html>
- AWS: current pricing
  <https://aws.amazon.com/textract/pricing/>
