# Stage 3 - Step 11/12: extraction-provider evaluation and selection

## Decision status

**OpenAI GPT-5-mini is the selected real provider for step 12.** This is a
deliberate, explicit change from the provisional Azure/AWS plan below, made
without running the live Azure-vs-AWS bake-off that plan called for: the
project chose to integrate a real provider now and revisit the choice later
rather than block on that comparison. Azure AI Document Intelligence
`prebuilt-invoice` remains the candidate for a later migration once the
Document Intelligence / Textract bake-off is actually run; AWS Textract
`AnalyzeExpense` remains a comparison candidate at that point too. Nothing
about `ExtractionProvider` ties the application to GPT-5-mini - swapping in
Azure later means writing a new adapter, not changing callers.

Because this selection was not validated by a live bake-off or a
representative evaluation set, the same caution applies as before: this
project must not claim measured accuracy, latency, cost, or confidence
calibration for GPT-5-mini on LedgerDrop's real invoice population.

Vendor documentation establishes feature support, not performance on
LedgerDrop's invoice population. Microsoft explicitly recommends a
representative pilot because accuracy and useful confidence thresholds vary by
scenario; the same caution applies to any vendor, GPT-5-mini included.

## Why confidence is null for the GPT-5-mini adapter

GPT-5-mini has no calibrated per-field confidence the way Document
Intelligence's `prebuilt-invoice` or Textract's `AnalyzeExpense` do. The
adapter does not even ask the model for a self-reported confidence number:
per the existing guidance below, "an LLM's self-reported probability" is not
a meaningful substitute for a calibrated score, so asking for one and
discarding it would add prompt complexity for no benefit. Every field's
`confidence` is `None` unconditionally. This can change only if a later
provider (or a separately calibrated scheme for GPT-5-mini) demonstrates that
its scores actually track correctness.

## How the GPT-5-mini adapter works

`app/services/processing/extraction/openai_provider.py` (`OpenAIExtractionProvider`):

- Sends the stored PDF's original bytes to OpenAI's Responses API as a
  base64-encoded `input_file` - no local rendering or re-encoding; OpenAI
  extracts text and page images from the PDF itself.
- Requires a strict `json_schema` response format naming every
  `InvoiceExtraction` field, so the API itself rejects any shape other than
  the one requested.
- Maps the decoded JSON into the same `{value, confidence}` shape the fake
  provider uses, with `confidence` always `None`, and returns it as a
  *payload* - `ExtractionService` still validates it against
  `InvoiceExtraction` before anything is persisted, exactly like every other
  provider. A response that fails to parse as JSON is mapped to a payload
  that fails that validation, never raised as a provider failure.
- Returns the full raw API response alongside the payload for internal audit
  (`ExtractionAttempt.raw_response`) - never returned by a public endpoint.
- Configured via `EXTRACTION_PROVIDER=openai`, `OPENAI_API_KEY`,
  `OPENAI_MODEL` (default `gpt-5-mini`), `EXTRACTION_PROVIDER_TIMEOUT_SECONDS`.
  `EXTRACTION_PROVIDER` stays `fake` until a real key is supplied.

## Original Azure/AWS desk research (kept for the future migration)

This section predates the GPT-5-mini decision above and was written when
Azure was still the provisional target. It stays here because it is the
starting point for the eventual Azure/AWS bake-off, not because Azure is the
current selection.

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

## Why Azure was the provisional target (for the future migration)

1. Its prebuilt invoice model supports PDFs, OCR, structured invoice fields,
   line items, and field confidence.
2. Its schema is close to `InvoiceExtraction`, limiting adapter-specific logic.
3. It can consume the original PDF, including the image-only evaluation case.
4. It stays isolated behind `ExtractionProvider`; the first adapter does not
   lock the application to Azure.

These are implementation-fit reasons, not empirical proof that Azure is more
accurate than AWS for LedgerDrop.

## Required live bake-off (for a later Azure/AWS migration decision)

Not run yet; GPT-5-mini was integrated without it (see "Decision status"
above). Run Azure and AWS over the same frozen evaluation-set version. Record:

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

## Confidence decision (general policy; see also "Why confidence is null for the GPT-5-mini adapter" above)

**Until a live bake-off demonstrates useful calibration for a given provider,
application-level confidence remains `null`.** Provider confidence may be retained only in the
internal raw response for analysis. It must not enter the application contract
or drive escalation merely because it lies between zero and one.

`evaluation.scoring` reports confidence coverage and Brier score (squared error
between confidence and observed correctness). Those metrics are diagnostic. A
useful decision also needs reliability checks across confidence bands and enough
incorrect predictions to reveal overconfidence. If later evidence shows the
scores track correctness, the adapter can persist genuine provider scores;
absent scores stay `null`. Vision-LLM self-reported probabilities also stay
`null` unless separately calibrated.

## Proposed Azure field mapping (for the future migration)

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

`EXTRACTION_PROVIDER`, `OPENAI_API_KEY`, `OPENAI_MODEL`, and
`EXTRACTION_PROVIDER_TIMEOUT_SECONDS` in `backend/.env.example` are wired to
the GPT-5-mini adapter (`app/api/deps.py`); the app still runs on the
deterministic offline fake while `EXTRACTION_PROVIDER=fake`, its default.
Credentials stay in environment variables, never committed. The Azure/AWS
placeholders described elsewhere in this document remain inert until that
migration is actually built. Normal automated tests never call OpenAI, Azure,
or AWS; a live comparison requires an explicit command and opt-in credentials.

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
