"""Offline accuracy scoring for the extraction evaluation dataset (Stage 3, step 11).

Compares a provider's :class:`InvoiceExtraction` output against the manually
recorded ground truth in :mod:`evaluation.dataset` and produces field-level and
line-item-level accuracy. No provider is called here - step 12 runs the real
adapter over ``load_cases()`` and hands the predictions to :func:`score_run`.

Value accuracy and confidence calibration are reported separately. Matching is
lenient in the ways extraction is allowed to vary and strict everywhere else:

* text (vendor / customer / description) - case-insensitive, whitespace-collapsed
* ids (invoice number, tax id) - exact after trimming
* dates - exact raw string after trimming (extraction never reformats)
* currency - exact after trimming and upper-casing
* amounts / quantities - numeric ``Decimal`` equality (``"100"`` == ``"100.00"``)

``None`` matches ``None``; a value against ``None`` (either direction) is wrong.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from app.schemas.extraction import InvoiceExtraction
from evaluation.dataset import (
    CRITICAL_FIELDS,
    LINE_ITEM_FIELDS,
    SCALAR_FIELDS,
    EvalCase,
    ExpectedInvoice,
    ExpectedLineItem,
    load_cases,
)

_TEXT_FIELDS = frozenset({"vendor_name", "customer_name", "description"})
_ID_FIELDS = frozenset({"invoice_number", "vendor_tax_id"})
_DATE_FIELDS = frozenset({"invoice_date", "due_date"})
_CURRENCY_FIELDS = frozenset({"currency"})
_AMOUNT_FIELDS = frozenset(
    {"subtotal", "tax_amount", "total_amount", "quantity", "unit_price", "line_total"}
)


def _kind(name: str) -> str:
    if name in _TEXT_FIELDS:
        return "text"
    if name in _ID_FIELDS:
        return "id"
    if name in _DATE_FIELDS:
        return "date"
    if name in _CURRENCY_FIELDS:
        return "currency"
    if name in _AMOUNT_FIELDS:
        return "amount"
    raise KeyError(name)


def _values_match(name: str, expected: str | None, predicted: str | None) -> bool:
    if expected is None or predicted is None:
        return expected is None and predicted is None

    kind = _kind(name)
    if kind == "text":
        return " ".join(expected.split()).casefold() == " ".join(predicted.split()).casefold()
    if kind == "id" or kind == "date":
        return expected.strip() == predicted.strip()
    if kind == "currency":
        return expected.strip().upper() == predicted.strip().upper()
    try:
        return Decimal(expected) == Decimal(predicted)
    except (InvalidOperation, ValueError):
        return False


@dataclass(frozen=True)
class FieldOutcome:
    name: str
    kind: str
    expected: str | None
    predicted: str | None
    correct: bool
    confidence: Decimal | None = None


def _predicted_scalar(predicted: InvoiceExtraction | None, name: str) -> str | None:
    if predicted is None:
        return None
    value = getattr(predicted, name).value
    return None if value is None else str(value)


def _predicted_line_item(predicted: InvoiceExtraction | None, index: int):
    if predicted is None or index >= len(predicted.line_items):
        return None
    return predicted.line_items[index]


@dataclass
class CaseResult:
    case_id: str
    category: str
    scored: bool
    skip_reason: str | None = None
    scalars: dict[str, FieldOutcome] = field(default_factory=dict)
    line_items: list[dict[str, FieldOutcome]] = field(default_factory=list)
    expected_item_count: int = 0
    predicted_item_count: int = 0

    # --- per-case tallies ------------------------------------------------
    @property
    def scalar_correct(self) -> int:
        return sum(1 for o in self.scalars.values() if o.correct)

    @property
    def scalar_total(self) -> int:
        return len(self.scalars)

    @property
    def critical_correct(self) -> int:
        return sum(1 for n in CRITICAL_FIELDS if self.scalars[n].correct)

    @property
    def critical_total(self) -> int:
        return len(CRITICAL_FIELDS) if self.scalars else 0

    @property
    def line_item_field_correct(self) -> int:
        return sum(1 for row in self.line_items for o in row.values() if o.correct)

    @property
    def line_item_field_total(self) -> int:
        return sum(len(row) for row in self.line_items)

    @property
    def line_item_count_exact(self) -> bool:
        return self.expected_item_count == self.predicted_item_count


def score_case(
    expected: ExpectedInvoice,
    predicted: InvoiceExtraction | None,
    *,
    case_id: str,
    category: str,
) -> CaseResult:
    result = CaseResult(case_id=case_id, category=category, scored=True)

    prediction_missing = predicted is None
    for name in SCALAR_FIELDS:
        exp = getattr(expected, name)
        pred = _predicted_scalar(predicted, name)
        confidence = None if predicted is None else getattr(predicted, name).confidence
        result.scalars[name] = FieldOutcome(
            name=name,
            kind=_kind(name),
            expected=exp,
            predicted=pred,
            correct=not prediction_missing and _values_match(name, exp, pred),
            confidence=confidence,
        )

    result.expected_item_count = len(expected.line_items)
    result.predicted_item_count = 0 if predicted is None else len(predicted.line_items)
    item_count = max(result.expected_item_count, result.predicted_item_count)
    for index in range(item_count):
        exp_item = expected.line_items[index] if index < result.expected_item_count else None
        pred_item = _predicted_line_item(predicted, index)
        row: dict[str, FieldOutcome] = {}
        for name in LINE_ITEM_FIELDS:
            exp_value = None if exp_item is None else getattr(exp_item, name)
            pred_value = None if pred_item is None else _stringify(getattr(pred_item, name).value)
            confidence = None if pred_item is None else getattr(pred_item, name).confidence
            row[name] = FieldOutcome(
                name=name,
                kind=_kind(name),
                expected=exp_value,
                predicted=pred_value,
                correct=(
                    exp_item is not None
                    and pred_item is not None
                    and _values_match(name, exp_value, pred_value)
                ),
                confidence=confidence,
            )
        result.line_items.append(row)
    return result


def _stringify(value: object | None) -> str | None:
    return None if value is None else str(value)


@dataclass
class RunReport:
    results: list[CaseResult]

    @property
    def scored(self) -> list[CaseResult]:
        return [r for r in self.results if r.scored]

    def _ratio(self, correct: int, total: int) -> float:
        return 1.0 if total == 0 else correct / total

    @property
    def field_accuracy(self) -> float:
        return self._ratio(
            sum(r.scalar_correct for r in self.scored),
            sum(r.scalar_total for r in self.scored),
        )

    @property
    def critical_field_accuracy(self) -> float:
        return self._ratio(
            sum(r.critical_correct for r in self.scored),
            sum(r.critical_total for r in self.scored),
        )

    @property
    def line_item_field_accuracy(self) -> float:
        return self._ratio(
            sum(r.line_item_field_correct for r in self.scored),
            sum(r.line_item_field_total for r in self.scored),
        )

    @property
    def line_item_count_exact_rate(self) -> float:
        scored = self.scored
        return self._ratio(sum(1 for r in scored if r.line_item_count_exact), len(scored))

    @property
    def field_outcomes(self) -> list[FieldOutcome]:
        return [
            *[outcome for result in self.scored for outcome in result.scalars.values()],
            *[
                outcome
                for result in self.scored
                for row in result.line_items
                for outcome in row.values()
            ],
        ]

    @property
    def confidence_coverage(self) -> float:
        """Share of scored fields for which the provider supplied confidence."""
        outcomes = self.field_outcomes
        return self._ratio(sum(o.confidence is not None for o in outcomes), len(outcomes))

    @property
    def confidence_brier_score(self) -> float | None:
        """Mean squared error of supplied confidence; lower is better.

        ``None`` means the provider supplied no scores. This is diagnostic, not
        proof of calibration; the dataset must be representative and large
        enough before confidence is allowed into business decisions.
        """
        scored = [o for o in self.field_outcomes if o.confidence is not None]
        if not scored:
            return None
        return sum((float(o.confidence) - float(o.correct)) ** 2 for o in scored) / len(scored)

    @property
    def by_category(self) -> dict[str, dict[str, float]]:
        out: dict[str, dict[str, float]] = {}
        for category in sorted({r.category for r in self.scored}):
            rows = [r for r in self.scored if r.category == category]
            out[category] = {
                "field_accuracy": self._ratio(
                    sum(r.scalar_correct for r in rows), sum(r.scalar_total for r in rows)
                ),
                "critical_field_accuracy": self._ratio(
                    sum(r.critical_correct for r in rows), sum(r.critical_total for r in rows)
                ),
                "line_item_field_accuracy": self._ratio(
                    sum(r.line_item_field_correct for r in rows),
                    sum(r.line_item_field_total for r in rows),
                ),
            }
        return out

    def format_summary(self) -> str:
        lines = [
            f"scored cases:            {len(self.scored)} / {len(self.results)}",
            f"field accuracy:          {self.field_accuracy:.1%}",
            f"critical-field accuracy: {self.critical_field_accuracy:.1%}",
            f"line-item field accuracy:{self.line_item_field_accuracy:.1%}",
            f"line-item count exact:   {self.line_item_count_exact_rate:.1%}",
            f"confidence coverage:     {self.confidence_coverage:.1%}",
            "confidence Brier score:  "
            + (
                "n/a"
                if self.confidence_brier_score is None
                else f"{self.confidence_brier_score:.4f}"
            ),
            "",
            "by category:",
        ]
        for category, stats in self.by_category.items():
            lines.append(
                f"  {category:18} fields {stats['field_accuracy']:.0%}  "
                f"critical {stats['critical_field_accuracy']:.0%}  "
                f"line-items {stats['line_item_field_accuracy']:.0%}"
            )
        return "\n".join(lines)


def score_run(
    predictions: Mapping[str, InvoiceExtraction | None],
    *,
    cases: list[EvalCase] | None = None,
) -> RunReport:
    """Score ``{case_id: predicted invoice or None}`` against the dataset.

    Cases whose ground truth is ``None`` (unusable / not-an-invoice) are recorded
    but not scored. A missing or ``None`` prediction for a scored case counts as
    every field wrong - a provider failure is not a free pass.
    """
    cases = cases if cases is not None else load_cases()
    results: list[CaseResult] = []
    for case in cases:
        if case.expected is None:
            results.append(
                CaseResult(
                    case_id=case.id,
                    category=case.category,
                    scored=False,
                    skip_reason="no ground truth (not an invoice / unusable)",
                )
            )
            continue
        results.append(
            score_case(
                case.expected,
                predictions.get(case.id),
                case_id=case.id,
                category=case.category,
            )
        )
    return RunReport(results=results)


__all__ = [
    "FieldOutcome",
    "CaseResult",
    "RunReport",
    "score_case",
    "score_run",
]
