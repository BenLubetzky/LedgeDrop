"""Stage 5 deterministic validation service.

The pinned ⚠ policy constants live in :mod:`.policy` and the pure, deterministic
rule functions in :mod:`.rules`. Later steps add the engine (step 9), lifecycle
guards (step 10), and the repository / service orchestration (step 11). Nothing
in this package makes an AI or external-network call, and every numeric
comparison is ``Decimal`` arithmetic (spec §2.9).
"""

from app.services.processing.validation import policy
from app.services.processing.validation.rules import (
    RULE_FUNCTIONS,
    DuplicateCandidate,
    RuleContext,
    run_rules,
)

__all__ = [
    "policy",
    "RULE_FUNCTIONS",
    "DuplicateCandidate",
    "RuleContext",
    "run_rules",
]
