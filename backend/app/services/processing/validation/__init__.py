"""Stage 5 deterministic validation service.

The pinned ⚠ policy constants live in :mod:`.policy`, the pure rule functions in
:mod:`.rules`, the read-only :mod:`.engine` loads the inputs and assembles an
``InvoiceValidation``, and :mod:`.lifecycle` guards attempt transitions.
:mod:`.repository` is the sole reader/writer of the validation tables, and
:mod:`.service` orchestrates ``start`` / ``retry``. Nothing in this package
makes an AI or external-network call, and every numeric comparison is
``Decimal`` arithmetic (spec §2.9).
"""

from app.services.processing.validation import policy
from app.services.processing.validation.engine import evaluate
from app.services.processing.validation.repository import ValidationRepository
from app.services.processing.validation.rules import (
    RULE_FUNCTIONS,
    DuplicateCandidate,
    RuleContext,
    run_rules,
)
from app.services.processing.validation.service import ValidationService

__all__ = [
    "policy",
    "evaluate",
    "ValidationRepository",
    "ValidationService",
    "RULE_FUNCTIONS",
    "DuplicateCandidate",
    "RuleContext",
    "run_rules",
]
