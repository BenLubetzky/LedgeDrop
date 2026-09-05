"""Stage 6 deterministic decision engine (package 3).

``decide`` is a pure function: given a completed Stage 5 validation result
and an optional manual-review request, it returns the ``InvoiceDecision`` the
Part 2 policy implies. No AI call, no network call, and no database access -
the whole decision is a function of its two arguments and the fixed
``decision_catalogue`` table (``docs/stage-6-decision.md`` §2.6). It never
re-evaluates, re-weighs, or discards a Stage 5 finding, and it never reads
``expected`` / ``actual`` / ``context`` or any confidence value directly -
only a finding's ``rule`` (to look up its policy) and ``field_path`` (carried
through unchanged) matter.

``decide`` never mutates ``validation`` or anything it references.

**The 1:1, order-preserving correspondence.** Every finding in
``validation.findings`` becomes exactly one reason, in the same order,
followed by exactly one more reason if ``manual_review_requested`` is true.
No finding is ever skipped, merged, or reordered. Later packages rely on this
explicitly: package 4's service builds the parallel ``source_finding_ids``
sequence persistence needs
(:func:`app.schemas.decision_persistence.reason_rows`) by zipping the same
source findings (which it already has as ORM rows, each with a
``validation_finding_id``) against the reasons this module returns, in order
- :func:`reason_for_finding` and :func:`manual_review_reason` are exposed
separately for exactly that purpose, so the service never has to guess which
reason came from which finding.
"""

from __future__ import annotations

from app.schemas.decision import DecisionReason, InvoiceDecision
from app.schemas.decision_catalogue import manual_review_policy, policy_for_rule
from app.schemas.validation import InvoiceValidation, ValidationFinding

__all__ = ["decide", "reason_for_finding", "manual_review_reason"]


def reason_for_finding(finding: ValidationFinding) -> DecisionReason:
    """The one :class:`DecisionReason` the Part 2 policy assigns to ``finding``.

    A pure catalogue lookup keyed on ``finding.rule`` - ``code``,
    ``triggers_review``, and ``message`` all come from
    :func:`app.schemas.decision_catalogue.policy_for_rule`, never from the
    finding itself, so a decision reason always reads exactly like the fixed
    policy text even if a finding somehow carried a different message.
    ``field_path`` is the one piece of per-finding data that passes through
    unchanged, since it names *which* field the finding (and so the reason)
    is about.
    """
    policy = policy_for_rule(finding.rule)
    return DecisionReason(
        code=policy.code,
        triggers_review=policy.triggers_review,
        source_rule=finding.rule,
        field_path=finding.field_path,
        message=policy.message,
    )


def manual_review_reason() -> DecisionReason:
    """The fixed reason for an explicit, caller-supplied review request.

    Always ``triggers_review = true`` and never tied to a finding - see
    :func:`app.schemas.decision_catalogue.manual_review_policy`.
    """
    policy = manual_review_policy()
    return DecisionReason(
        code=policy.code,
        triggers_review=policy.triggers_review,
        source_rule=None,
        field_path=None,
        message=policy.message,
    )


def decide(
    validation: InvoiceValidation, *, manual_review_requested: bool = False
) -> InvoiceDecision:
    """Decide a completed Stage 5 validation result.

    Builds one reason per finding in ``validation.findings``, in order (via
    :func:`reason_for_finding`), then appends :func:`manual_review_reason`
    when ``manual_review_requested`` is true. The outcome is derived from the
    resulting reason list by :meth:`InvoiceDecision.from_reasons`:
    ``NEEDS_REVIEW`` exactly when at least one reason - rule-derived or
    manual - has ``triggers_review = true``; never inferred from a bare count
    or severity tally (spec §1.4, §2.1). A validation with no findings and no
    manual request decides ``ACCEPTED`` with an empty reason list; a
    validation whose only findings are non-gating (for example
    ``critical_field_confidence_unavailable`` or ``no_line_items``) also
    decides ``ACCEPTED``, but keeps those reasons - nothing Stage 5 asserted
    is ever discarded, gating or not.
    """
    # This flag can turn an otherwise accepted invoice into NEEDS_REVIEW, so
    # do not let Python truthiness reinterpret strings or integers supplied by
    # an internal caller (notably, ``"false"`` is truthy). Public request
    # schemas will validate it too, but this pure boundary must be safe on its
    # own just like DecisionReason.triggers_review is strict.
    if type(manual_review_requested) is not bool:
        raise TypeError("manual_review_requested must be a bool")

    reasons = [reason_for_finding(finding) for finding in validation.findings]
    if manual_review_requested:
        reasons.append(manual_review_reason())
    return InvoiceDecision.from_reasons(reasons)
