"""Decision persistence models (Stage 6, package 2).

Two tables, mirroring ``app/models/validation.py``:

* ``invoice_decisions`` - one row per **decision attempt** over a source
  validation attempt. A validation attempt may have several decision
  attempts (the initial run plus explicit retries of a *technical* failure),
  so the natural key is ``(validation_id, attempt_number)``. The partial
  unique index ``uq_invoice_decisions_one_active_per_validation`` allows at
  most one ``PROCESSING`` attempt per source validation. Unlike Stage 5's
  ``summary`` (re-derived on read and never stored), ``outcome`` *is* stored:
  it is Stage 6's central business artifact and needs to be directly
  queryable (for example, listing every document that needs review) without
  joining every reason row. It stays internally consistent with the stored
  reasons for free - :class:`app.schemas.decision.InvoiceDecision` cannot be
  constructed with a disagreeing outcome (``app/schemas/decision.py``), so
  nothing here re-derives or re-checks it against the reasons at write time.
* ``invoice_decision_reasons`` - the ordered reasons for one attempt,
  mirroring ``invoice_validation_findings``. Each reason mirrors
  :class:`app.schemas.decision.DecisionReason`: a closed ``code``, whether it
  ``triggers_review``, the originating Stage 5 ``source_rule`` (``NULL`` only
  for ``manual_review_requested``), a Stage 4 ``field_path`` (or ``NULL``),
  and a fixed ``message``. ``source_finding_id`` additionally references the
  exact ``invoice_validation_findings`` row a rule-derived reason came from -
  the "finding reference" the Stage 6 handoff calls for - so a reason's full
  detail (``expected`` / ``actual`` / ``context``) is always reachable
  without duplicating it here (``NULL`` only for ``manual_review_requested``,
  which has no finding behind it).

``invoice_decisions.policy_version`` records which named revision of the
Part 2 decision policy (``app.schemas.decision_catalogue.POLICY_VERSION``)
was in effect when the attempt ran, stamped once at attempt creation and
never changed - so a historical decision's reasons stay explicable even
after the policy table is retuned later (spec §2.7).

``source_rule`` is stored as plain, CHECK-constrained text rather than a
second native enum reusing Stage 5's ``validation_rule`` type: the closed set
of valid values is already guaranteed application-side by
:class:`app.schemas.decision.DecisionReason`, and the combined
``source_rule_matches_code`` CHECK below ties a non-null ``source_rule`` to a
matching ``code`` for free, so no separate list-membership CHECK is needed -
without introducing a native type shared, and therefore version-coupled,
across two migrations.

These rows never modify the Stage 2-5 records they derive from. The ORM
classes are ``DecisionAttempt`` and ``DecisionReasonRow`` to avoid confusion
with the Pydantic contract types ``InvoiceDecision`` / ``DecisionReason``; the
tables are ``invoice_decisions`` and ``invoice_decision_reasons``.

The full boundary and the pinned decision policy live in
``docs/stage-6-decision.md``. The column types here enforce only structural
parts; building the actual reasons from a validation attempt is the package 3
engine, and starting/retrying an attempt is the package 4 service.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base
from app.schemas.decision import DecisionOutcome, DecisionReasonCode, DecisionStatus
from app.schemas.normalization import (
    NORMALIZED_LINE_ITEM_FIELD_NAMES,
    NORMALIZED_SCALAR_FIELD_NAMES,
)

if TYPE_CHECKING:
    from app.models.validation import ValidationAttempt

__all__ = [
    "DecisionStatus",
    "DecisionOutcome",
    "DecisionReasonCode",
    "DecisionAttempt",
    "DecisionReasonRow",
]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# field_path shape: identical construction to app/models/validation.py so the
# two CHECKs cannot drift apart (both are built from the same Stage 4 name
# tuples, never hand-copied).
_SCALAR_IN_LIST = ", ".join(f"'{name}'" for name in NORMALIZED_SCALAR_FIELD_NAMES)
_LINE_ITEM_FIELD_ALT = "|".join(NORMALIZED_LINE_ITEM_FIELD_NAMES)
_FIELD_PATH_CHECK = (
    f"field_path IS NULL OR field_path IN ({_SCALAR_IN_LIST}) OR "
    rf"field_path ~ '^line_items\.(0|[1-9][0-9]*)\.({_LINE_ITEM_FIELD_ALT})$'"
)

# Mirrors invoice_validations' status/failure CHECK, plus outcome: PROCESSING
# carries neither completion, failure detail, nor outcome; COMPLETED has a
# completion time and an outcome, and no failure detail; FAILED has a
# completion time and failure detail, and no outcome. Every branch uses
# IS [NOT] NULL explicitly (not bare equality) so an unexpected NULL cannot
# make the constraint vacuously pass.
_STATUS_FIELDS_CONSISTENT = (
    "(status = 'PROCESSING' AND completed_at IS NULL "
    "AND failure_code IS NULL AND failure_message IS NULL "
    "AND outcome IS NULL) OR "
    "(status = 'COMPLETED' AND completed_at IS NOT NULL "
    "AND failure_code IS NULL AND failure_message IS NULL "
    "AND outcome IS NOT NULL) OR "
    "(status = 'FAILED' AND completed_at IS NOT NULL "
    "AND failure_code IS NOT NULL AND failure_message IS NOT NULL "
    "AND outcome IS NULL)"
)

# A reason's source_rule/source_finding_id are null exactly for
# manual_review_requested, and otherwise source_rule must name the same rule
# as code (the two enums share their string values by construction -
# app/schemas/decision.py's DecisionReasonCode - so this comparison also
# proves source_rule only ever holds one of the fifteen valid rule codes,
# with no separate list-membership CHECK needed). Every branch tests
# IS [NOT] NULL explicitly before comparing, so a NULL source_rule on a
# rule-derived reason cannot make the constraint vacuously pass (NULL = x is
# NULL, not FALSE, and a NULL CHECK result is treated as satisfied).
_SOURCE_RULE_MATCHES_CODE = (
    "(code = 'manual_review_requested' AND source_rule IS NULL "
    "AND source_finding_id IS NULL) OR "
    "(code <> 'manual_review_requested' AND source_rule IS NOT NULL "
    "AND source_rule = code::text AND source_finding_id IS NOT NULL)"
)


class DecisionAttempt(Base):
    __tablename__ = "invoice_decisions"

    __table_args__ = (
        UniqueConstraint(
            "validation_id",
            "attempt_number",
            name="uq_invoice_decisions_validation_id_attempt_number",
        ),
        # At most one active (PROCESSING) attempt per source validation.
        Index(
            "uq_invoice_decisions_one_active_per_validation",
            "validation_id",
            unique=True,
            postgresql_where=text("status = 'PROCESSING'"),
        ),
        CheckConstraint("attempt_number >= 1", name="attempt_number_positive"),
        CheckConstraint(_STATUS_FIELDS_CONSISTENT, name="status_fields_consistent"),
    )

    decision_id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    # Source Stage 5 attempt. CASCADE so deleting a document (which cascades
    # through extraction -> normalization -> validation) also clears the
    # derived decisions; decision code never writes back to this row.
    validation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("invoice_validations.validation_id", ondelete="CASCADE"),
        nullable=False,
    )

    # 1-based; retry N produces attempt_number N. Unique per source validation.
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)

    status: Mapped[DecisionStatus] = mapped_column(
        Enum(
            DecisionStatus,
            name="decision_status",
            native_enum=True,
            validate_strings=True,
        ),
        nullable=False,
    )

    # Business outcome. NULL until COMPLETED (see _STATUS_FIELDS_CONSISTENT).
    outcome: Mapped[DecisionOutcome | None] = mapped_column(
        Enum(
            DecisionOutcome,
            name="decision_outcome",
            native_enum=True,
            validate_strings=True,
        ),
        nullable=True,
    )

    # The named REASON_POLICIES revision (decision_catalogue.POLICY_VERSION)
    # in effect when this attempt ran. Stamped once at creation; never
    # changed, so a later policy retune never rewrites a past explanation.
    policy_version: Mapped[str] = mapped_column(String(32), nullable=False)

    # --- timing --------------------------------------------------------- ---
    started_at: Mapped[datetime] = mapped_column(nullable=False, default=_utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(nullable=True)

    # --- technical failure (client-safe; set only when status is FAILED) -----
    failure_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    failure_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        nullable=False, default=_utcnow, onupdate=_utcnow
    )

    # --- relationships ---------------------------------------------------- --
    source_validation: Mapped["ValidationAttempt"] = relationship(
        back_populates="decisions"
    )
    reasons: Mapped[list["DecisionReasonRow"]] = relationship(
        back_populates="decision",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="DecisionReasonRow.position",
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<DecisionAttempt {self.decision_id} "
            f"validation={self.validation_id} #{self.attempt_number} "
            f"{self.status}>"
        )


class DecisionReasonRow(Base):
    __tablename__ = "invoice_decision_reasons"

    __table_args__ = (
        UniqueConstraint(
            "decision_id",
            "position",
            name="uq_invoice_decision_reasons_decision_id_position",
        ),
        CheckConstraint("position >= 0", name="position_non_negative"),
        CheckConstraint(_FIELD_PATH_CHECK, name="field_path_shape"),
        CheckConstraint(_SOURCE_RULE_MATCHES_CODE, name="source_rule_matches_code"),
    )

    decision_reason_id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, default=uuid.uuid4
    )
    decision_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("invoice_decisions.decision_id", ondelete="CASCADE"),
        nullable=False,
    )

    # 0-based order within the attempt; Stage 5 catalogue order, then the
    # manual-review reason (if any) last.
    position: Mapped[int] = mapped_column(Integer, nullable=False)

    code: Mapped[DecisionReasonCode] = mapped_column(
        Enum(
            DecisionReasonCode,
            name="decision_reason_code",
            native_enum=True,
            validate_strings=True,
            values_callable=lambda enum_type: [member.value for member in enum_type],
        ),
        nullable=False,
    )
    triggers_review: Mapped[bool] = mapped_column(Boolean, nullable=False)

    # The Stage 5 ValidationRule this reason came from, as plain text (see
    # module docstring for why this is not a second native enum). NULL only
    # for manual_review_requested.
    source_rule: Mapped[str | None] = mapped_column(Text, nullable=True)

    # The exact invoice_validation_findings row this reason explains, so the
    # finding's full expected/actual/context stays reachable without being
    # duplicated here. NULL only for manual_review_requested. The default
    # NO ACTION delete behavior protects the audit trail from an independent
    # finding deletion; deleting the owning validation/document still removes
    # the complete decision branch through its own CASCADE.
    source_finding_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("invoice_validation_findings.validation_finding_id"),
        nullable=True,
    )

    # A Stage 4 field path (scalar name or "line_items.<index>.<field>") or
    # NULL for an invoice-level reason. Shape enforced by a CHECK above.
    field_path: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Fixed, generic, client-safe sentence - the originating Stage 5 message
    # verbatim, or the one Stage-6-only manual-review sentence.
    message: Mapped[str] = mapped_column(Text, nullable=False)

    created_at: Mapped[datetime] = mapped_column(nullable=False, default=_utcnow)

    decision: Mapped[DecisionAttempt] = relationship(back_populates="reasons")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<DecisionReasonRow {self.decision_reason_id} "
            f"decision={self.decision_id} pos={self.position} {self.code}>"
        )
