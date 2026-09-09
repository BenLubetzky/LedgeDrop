from app.models.decision import (
    DecisionAttempt,
    DecisionOutcome,
    DecisionReasonCode,
    DecisionReasonRow,
    DecisionStatus,
)
from app.models.document import Document, DocumentStatus
from app.models.extraction import ExtractionAttempt, ExtractionLineItem, ExtractionStatus
from app.models.normalization import (
    NormalizationAttempt,
    NormalizationErrorCode,
    NormalizationFieldError,
    NormalizationLineItem,
    NormalizationStatus,
)
from app.models.review import ReviewAction, ReviewRecord
from app.models.validation import (
    FindingSeverity,
    ValidationAttempt,
    ValidationFindingRow,
    ValidationRule,
    ValidationStatus,
)

# Imported last on purpose: invoice_corrections / invoice_correction_fields
# reuse the document_status, decision_outcome and normalization_error_code
# native enums that earlier tables own (create_type=False on those columns),
# and invoice_normalizations closes an FK cycle with invoice_corrections
# (use_alter). Registering the correction tables after every table they lean
# on keeps Base.metadata.create_all ordering correct in the test harness.
from app.models.correction import (  # noqa: E402
    CorrectionAttempt,
    CorrectionFieldRow,
    CorrectionOperation,
    CorrectionStatus,
)

__all__ = [
    "Document",
    "DocumentStatus",
    "CorrectionAttempt",
    "CorrectionFieldRow",
    "CorrectionOperation",
    "CorrectionStatus",
    "ExtractionAttempt",
    "ExtractionLineItem",
    "ExtractionStatus",
    "NormalizationAttempt",
    "NormalizationErrorCode",
    "NormalizationFieldError",
    "NormalizationLineItem",
    "NormalizationStatus",
    "FindingSeverity",
    "ValidationAttempt",
    "ValidationFindingRow",
    "ValidationRule",
    "ValidationStatus",
    "DecisionAttempt",
    "DecisionOutcome",
    "DecisionReasonCode",
    "DecisionReasonRow",
    "DecisionStatus",
    "ReviewAction",
    "ReviewRecord",
]
