"""Formal Stage 6 decision-reason catalogue (package 1).

Turns the Part 2 policy table in ``docs/stage-6-decision.md`` into structured
data: for every :class:`~app.schemas.validation.ValidationRule`, whether a
finding for that rule requires human review, and the fixed client-safe
reason text to use. This is **the decision policy** - which validation
findings require review, and which are recorded for context only.

This module is pure data. It calls no AI provider, makes no network call, and
touches no database. It reuses each rule's existing client-safe sentence from
:mod:`app.schemas.validation_catalogue` rather than inventing new text, so a
:class:`~app.schemas.decision.DecisionReason` reads exactly like the Stage 5
finding it explains.

The catalogue is closed and exhaustive: :data:`REASON_POLICIES` has exactly
one :class:`ReasonPolicy` per :class:`~app.schemas.validation.ValidationRule`
member, plus one for the Stage-6-only ``manual_review_requested`` reason -
checked at import time so the mapping cannot silently drift from either enum.

Building an actual :class:`~app.schemas.decision.DecisionReason` list from a
completed validation attempt is the package 3 engine's job; this module only
supplies the policy it applies.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from app.schemas.decision import DecisionReasonCode
from app.schemas.validation import ValidationRule
from app.schemas.validation_catalogue import RULE_MESSAGES

__all__ = [
    "ReasonPolicy",
    "REASON_POLICIES",
    "REASON_POLICY_BY_RULE",
    "REASON_POLICY_BY_CODE",
    "MANUAL_REVIEW_MESSAGE",
    "POLICY_VERSION",
    "policy_for_rule",
    "manual_review_policy",
]

MANUAL_REVIEW_MESSAGE: Final[str] = "A manual review of this invoice was requested."

# Identifies this exact revision of REASON_POLICIES. Stamped onto every
# decision attempt at creation (``invoice_decisions.policy_version``, package
# 2) so a historical decision's reasons stay explicable even after this table
# is retuned - bump it whenever a triggers_review value, the reason set, or a
# message changes, so a stored decision can always be traced to the policy
# text that produced it.
POLICY_VERSION: Final[str] = "1"


@dataclass(frozen=True, slots=True)
class ReasonPolicy:
    """The Part 2 policy entry for one decision reason.

    ``source_rule`` is ``None`` exactly for ``manual_review_requested``, the
    one reason with no Stage 5 finding behind it. ``triggers_review`` is the
    policy call: whether this reason, on its own, requires ``NEEDS_REVIEW``.
    """

    code: DecisionReasonCode
    source_rule: ValidationRule | None
    triggers_review: bool
    message: str


# --- the catalogue, in ValidationRule / spec §2.2 order, manual reason last --

REASON_POLICIES: Final[tuple[ReasonPolicy, ...]] = (
    # Gating: an error or a genuine data/consistency problem a reviewer
    # should see before the invoice is treated as accepted.
    ReasonPolicy(
        code=DecisionReasonCode.MISSING_REQUIRED_FIELD,
        source_rule=ValidationRule.MISSING_REQUIRED_FIELD,
        triggers_review=True,
        message=RULE_MESSAGES[ValidationRule.MISSING_REQUIRED_FIELD],
    ),
    ReasonPolicy(
        code=DecisionReasonCode.NORMALIZATION_ERROR,
        source_rule=ValidationRule.NORMALIZATION_ERROR,
        triggers_review=True,
        message=RULE_MESSAGES[ValidationRule.NORMALIZATION_ERROR],
    ),
    ReasonPolicy(
        code=DecisionReasonCode.DUE_DATE_BEFORE_INVOICE_DATE,
        source_rule=ValidationRule.DUE_DATE_BEFORE_INVOICE_DATE,
        triggers_review=True,
        message=RULE_MESSAGES[ValidationRule.DUE_DATE_BEFORE_INVOICE_DATE],
    ),
    ReasonPolicy(
        code=DecisionReasonCode.DUE_DATE_FAR_AFTER_INVOICE_DATE,
        source_rule=ValidationRule.DUE_DATE_FAR_AFTER_INVOICE_DATE,
        triggers_review=True,
        message=RULE_MESSAGES[ValidationRule.DUE_DATE_FAR_AFTER_INVOICE_DATE],
    ),
    ReasonPolicy(
        code=DecisionReasonCode.INVOICE_DATE_IN_FUTURE,
        source_rule=ValidationRule.INVOICE_DATE_IN_FUTURE,
        triggers_review=True,
        message=RULE_MESSAGES[ValidationRule.INVOICE_DATE_IN_FUTURE],
    ),
    ReasonPolicy(
        code=DecisionReasonCode.INVOICE_DATE_IMPLAUSIBLY_OLD,
        source_rule=ValidationRule.INVOICE_DATE_IMPLAUSIBLY_OLD,
        triggers_review=True,
        message=RULE_MESSAGES[ValidationRule.INVOICE_DATE_IMPLAUSIBLY_OLD],
    ),
    ReasonPolicy(
        code=DecisionReasonCode.TOTALS_DO_NOT_RECONCILE,
        source_rule=ValidationRule.TOTALS_DO_NOT_RECONCILE,
        triggers_review=True,
        message=RULE_MESSAGES[ValidationRule.TOTALS_DO_NOT_RECONCILE],
    ),
    ReasonPolicy(
        code=DecisionReasonCode.LINE_ITEM_AMOUNT_MISMATCH,
        source_rule=ValidationRule.LINE_ITEM_AMOUNT_MISMATCH,
        triggers_review=True,
        message=RULE_MESSAGES[ValidationRule.LINE_ITEM_AMOUNT_MISMATCH],
    ),
    ReasonPolicy(
        code=DecisionReasonCode.LINE_ITEMS_DO_NOT_SUM,
        source_rule=ValidationRule.LINE_ITEMS_DO_NOT_SUM,
        triggers_review=True,
        message=RULE_MESSAGES[ValidationRule.LINE_ITEMS_DO_NOT_SUM],
    ),
    # Non-gating: recorded for context; not itself a reason to hold the
    # invoice for a human.
    ReasonPolicy(
        code=DecisionReasonCode.LINE_ITEM_SUM_NOT_CHECKED,
        source_rule=ValidationRule.LINE_ITEM_SUM_NOT_CHECKED,
        triggers_review=False,
        message=RULE_MESSAGES[ValidationRule.LINE_ITEM_SUM_NOT_CHECKED],
    ),
    ReasonPolicy(
        code=DecisionReasonCode.LOW_CONFIDENCE_CRITICAL_FIELD,
        source_rule=ValidationRule.LOW_CONFIDENCE_CRITICAL_FIELD,
        triggers_review=True,
        message=RULE_MESSAGES[ValidationRule.LOW_CONFIDENCE_CRITICAL_FIELD],
    ),
    ReasonPolicy(
        code=DecisionReasonCode.CRITICAL_FIELD_CONFIDENCE_UNAVAILABLE,
        source_rule=ValidationRule.CRITICAL_FIELD_CONFIDENCE_UNAVAILABLE,
        triggers_review=False,
        message=RULE_MESSAGES[ValidationRule.CRITICAL_FIELD_CONFIDENCE_UNAVAILABLE],
    ),
    ReasonPolicy(
        code=DecisionReasonCode.PROBABLE_DUPLICATE_INVOICE,
        source_rule=ValidationRule.PROBABLE_DUPLICATE_INVOICE,
        triggers_review=True,
        message=RULE_MESSAGES[ValidationRule.PROBABLE_DUPLICATE_INVOICE],
    ),
    ReasonPolicy(
        code=DecisionReasonCode.HIGH_VALUE_INVOICE,
        source_rule=ValidationRule.HIGH_VALUE_INVOICE,
        triggers_review=True,
        message=RULE_MESSAGES[ValidationRule.HIGH_VALUE_INVOICE],
    ),
    ReasonPolicy(
        code=DecisionReasonCode.NO_LINE_ITEMS,
        source_rule=ValidationRule.NO_LINE_ITEMS,
        triggers_review=False,
        message=RULE_MESSAGES[ValidationRule.NO_LINE_ITEMS],
    ),
    # Stage 6's own reason: no Stage 5 finding behind it, always gates.
    ReasonPolicy(
        code=DecisionReasonCode.MANUAL_REVIEW_REQUESTED,
        source_rule=None,
        triggers_review=True,
        message=MANUAL_REVIEW_MESSAGE,
    ),
)


# --- derived views and lookup ----------------------------------------

REASON_POLICY_BY_RULE: Final[Mapping[ValidationRule, ReasonPolicy]] = MappingProxyType(
    {policy.source_rule: policy for policy in REASON_POLICIES if policy.source_rule is not None}
)

REASON_POLICY_BY_CODE: Final[Mapping[DecisionReasonCode, ReasonPolicy]] = MappingProxyType(
    {policy.code: policy for policy in REASON_POLICIES}
)


def policy_for_rule(rule: ValidationRule) -> ReasonPolicy:
    """Return the :class:`ReasonPolicy` for the Stage 5 rule ``rule`` (always present)."""
    return REASON_POLICY_BY_RULE[rule]


def manual_review_policy() -> ReasonPolicy:
    """Return the fixed policy for a caller-supplied manual-review request."""
    return REASON_POLICY_BY_CODE[DecisionReasonCode.MANUAL_REVIEW_REQUESTED]


# --- import-time coverage guard ---------------------------------------


def _validate_catalogue() -> None:
    """Fail the import if the policy table drifts from either enum."""
    if len(REASON_POLICIES) != len(DecisionReasonCode):
        raise RuntimeError(
            "REASON_POLICIES must have exactly one entry per DecisionReasonCode member"
        )
    if set(REASON_POLICY_BY_CODE) != set(DecisionReasonCode):
        raise RuntimeError("REASON_POLICIES must cover every DecisionReasonCode exactly once")
    if set(REASON_POLICY_BY_RULE) != set(ValidationRule):
        raise RuntimeError("REASON_POLICIES must cover every ValidationRule exactly once")
    for policy in REASON_POLICIES:
        where = f"reason {policy.code.value!r}"
        is_manual = policy.code is DecisionReasonCode.MANUAL_REVIEW_REQUESTED
        if is_manual != (policy.source_rule is None):
            raise RuntimeError(
                f"{where}: source_rule must be null exactly for manual_review_requested"
            )
        if not is_manual and policy.source_rule.value != policy.code.value:
            raise RuntimeError(f"{where}: code must match source_rule's value")
        if not policy.message.strip() or "\n" in policy.message:
            raise RuntimeError(f"{where}: message must be a non-blank single line")


_validate_catalogue()
