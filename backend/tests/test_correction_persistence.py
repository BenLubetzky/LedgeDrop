"""Pure tests for the correction persistence bridge (no database)."""

from __future__ import annotations

from dataclasses import dataclass, field

from app.schemas.correction import (
    CorrectionFieldEntry,
    CorrectionOperation,
    InvoiceCorrection,
)
from app.schemas.correction_persistence import (
    correction_field_rows,
    invoice_correction_from_row,
)


@dataclass
class _FakeAttempt:
    reviewer_name: str
    note: str | None
    fields: list = field(default_factory=list)


def _correction() -> InvoiceCorrection:
    return InvoiceCorrection(
        reviewer_name="Dana Ops",
        note="checked",
        entries=[
            CorrectionFieldEntry(
                operation=CorrectionOperation.SET_FIELD,
                field_path="total_amount",
                previous_value="120.00",
                raw_value="130.00",
                normalized_value="130.00",
                error_code=None,
                error_message=None,
            ),
            CorrectionFieldEntry(
                operation=CorrectionOperation.SET_FIELD,
                field_path="currency",
                previous_value="EUR",
                raw_value="ZZZ",
                normalized_value=None,
                error_code="unknown_currency",
                error_message="Currency ZZZ is not on the approved list.",
            ),
            CorrectionFieldEntry(
                operation=CorrectionOperation.ADD_LINE_ITEM,
                field_path="line_items.1",
                previous_value=None,
                raw_value=None,
                normalized_value=None,
                error_code=None,
                error_message=None,
            ),
        ],
    )


def test_field_rows_are_position_numbered_in_list_order() -> None:
    rows = correction_field_rows(_correction().entries)
    assert [r["position"] for r in rows] == [0, 1, 2]
    assert rows[0]["field_path"] == "total_amount"
    assert rows[1]["error_code"].value == "unknown_currency"


def test_round_trip_rebuilds_and_revalidates_the_contract() -> None:
    correction = _correction()
    rows = correction_field_rows(correction.entries)
    attempt = _FakeAttempt(
        reviewer_name=correction.reviewer_name,
        note=correction.note,
        fields=[type("Row", (), r)() for r in rows],
    )
    rebuilt = invoice_correction_from_row(attempt, attempt.fields)
    assert rebuilt.model_dump() == correction.model_dump()
