// Response shapes for the Stage 3-7 endpoints the reviewer screens read.
// Only the client-safe fields the UI actually renders are modelled here;
// internal diagnostics (raw provider payloads, finding `context`, policy
// versions, attempt ids beyond what a URL needs) are intentionally omitted.

export type ExtractedField = { value: string | null; confidence: string | null };

export type ExtractionData = {
  invoice_number: ExtractedField;
  invoice_date: ExtractedField;
  due_date: ExtractedField;
  vendor_name: ExtractedField;
  vendor_tax_id: ExtractedField;
  customer_name: ExtractedField;
  currency: ExtractedField;
  subtotal: ExtractedField;
  tax_amount: ExtractedField;
  total_amount: ExtractedField;
  line_items: Array<{
    description: ExtractedField;
    quantity: ExtractedField;
    unit_price: ExtractedField;
    line_total: ExtractedField;
  }>;
};

export type ExtractionResult = {
  extraction_id: string;
  status: "PROCESSING" | "COMPLETED" | "FAILED";
  data: ExtractionData;
};

export type NormalizationError = {
  field_path: string;
  raw_value: string | null;
  code: string;
  message: string;
};

export type NormalizedData = {
  invoice_number: string | null;
  invoice_date: string | null;
  due_date: string | null;
  vendor_name: string | null;
  vendor_tax_id: string | null;
  customer_name: string | null;
  currency: string | null;
  subtotal: string | null;
  tax_amount: string | null;
  total_amount: string | null;
  line_items: Array<{
    description: string | null;
    quantity: string | null;
    unit_price: string | null;
    line_total: string | null;
  }>;
  errors: NormalizationError[];
};

export type NormalizationResult = {
  normalization_id: string;
  status: "PROCESSING" | "COMPLETED" | "FAILED";
  data: NormalizedData;
};

export type FindingSeverity = "error" | "warning" | "info";

export type ValidationFinding = {
  rule: string;
  severity: FindingSeverity;
  field_path: string | null;
  expected: string | null;
  actual: string | null;
  message: string;
};

export type ValidationResult = {
  validation_id: string;
  status: "PROCESSING" | "COMPLETED" | "FAILED";
  data: {
    findings: ValidationFinding[];
    summary: { total: number; error: number; warning: number; info: number };
  };
};

export type DecisionReason = {
  code: string;
  triggers_review: boolean;
  source_rule: string | null;
  field_path: string | null;
  message: string;
};

export type DecisionResult = {
  decision_id: string;
  status: "PROCESSING" | "COMPLETED" | "FAILED";
  outcome: "ACCEPTED" | "NEEDS_REVIEW" | null;
  data: { outcome: "ACCEPTED" | "NEEDS_REVIEW"; reasons: DecisionReason[] } | null;
};

export type QueueEntry = {
  document_id: string;
  original_filename: string;
  uploaded_at: string;
  extraction_id: string;
  normalization_id: string;
  validation_id: string;
  decision_id: string;
  decided_at: string;
  decision: { outcome: "ACCEPTED" | "NEEDS_REVIEW"; reasons: DecisionReason[] };
};

export type ReviewResult = {
  review_id: string;
  decision_id: string;
  document_id: string;
  action: "APPROVE" | "REJECT";
  reviewer_name: string;
  note: string | null;
  reviewed_at: string;
};

export type DocumentRecord = {
  document_id: string;
  original_filename: string;
  status:
    | "UPLOADED"
    | "PROCESSING"
    | "COMPLETED"
    | "NEEDS_REVIEW"
    | "FAILED"
    | "APPROVED"
    | "REJECTED";
  uploaded_at: string;
};

// Field order + human labels shared by the queue and detail screens.
export const SCALAR_FIELDS: Array<
  [keyof Omit<NormalizedData, "line_items" | "errors">, string]
> = [
  ["invoice_number", "Invoice number"],
  ["invoice_date", "Invoice date"],
  ["due_date", "Due date"],
  ["vendor_name", "Vendor"],
  ["vendor_tax_id", "Vendor tax ID"],
  ["customer_name", "Customer"],
  ["currency", "Currency"],
  ["subtotal", "Subtotal"],
  ["tax_amount", "Tax amount"],
  ["total_amount", "Total amount"],
];

/** "line_items.3.unit_price" -> "Line 4 · Unit price"; scalar paths -> their label. */
export function fieldPathLabel(path: string | null): string | null {
  if (!path) return null;
  const scalar = SCALAR_FIELDS.find(([key]) => key === path);
  if (scalar) return scalar[1];
  const match = /^line_items\.(\d+)\.(.+)$/.exec(path);
  if (match) {
    const line = Number(match[1]) + 1;
    const leaf = match[2].replace(/_/g, " ");
    return `Line ${line} · ${leaf.charAt(0).toUpperCase()}${leaf.slice(1)}`;
  }
  return path;
}
