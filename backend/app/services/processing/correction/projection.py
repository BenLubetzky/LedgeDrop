"""The ``base ⊕ corrections`` merge projection (Stage 8, package 2).

Pure, deterministic, no session, no AI, no network. The single place a
corrected canonical invoice is computed: it applies the **same Stage 4 field
normalizers** the normalization engine uses to the reviewer's submitted text,
overlays the results onto the base normalization attempt's canonical values,
applies line-item add/remove by list length, and returns

* a re-validated :class:`~app.schemas.normalization.NormalizedInvoice` (the
  ``YYYY-MM-DD`` date shape, the ISO-4217 currency shape, decimal typing, and
  the "a field with an error has a null value" rule all hold), and
* the ordered before/after diff as
  :class:`~app.schemas.correction.CorrectionFieldEntry` list - one entry per
  field the reviewer actually changed, plus ``ADD_LINE_ITEM`` /
  ``REMOVE_LINE_ITEM`` markers.

The Stage 4 extraction/normalization records are never read or written here -
only the already-rebuilt ``base`` contract and the submitted text.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app.schemas.correction import CorrectionFieldEntry, CorrectionOperation
from app.schemas.normalization import (
    NORMALIZED_LINE_ITEM_FIELD_NAMES,
    NORMALIZED_SCALAR_FIELD_NAMES,
    NormalizationError,
    NormalizedInvoice,
)
from app.services.processing.normalization.engine import (
    _LINE_ITEM_NORMALIZERS,
    _SCALAR_NORMALIZERS,
)

__all__ = ["SubmittedInvoice", "merge_correction"]


@dataclass(frozen=True, slots=True)
class SubmittedInvoice:
    """The raw corrected invoice a reviewer submitted, before normalization.

    ``scalars`` maps every Stage 4 scalar name to the reviewer's text (or
    ``None``); ``line_items`` is the full desired list, each a mapping of the
    four leaf names to text (or ``None``). Its length sets the merged list
    length (Part 3.4).
    """

    scalars: dict[str, str | None] = field(default_factory=dict)
    line_items: list[dict[str, str | None]] = field(default_factory=list)


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _values_equal(left: Any, right: Any) -> bool:
    if isinstance(left, Decimal) and isinstance(right, Decimal):
        return left == right
    return _str_or_none(left) == _str_or_none(right)


def _base_error_for(base: NormalizedInvoice, path: str) -> NormalizationError | None:
    for err in base.errors:
        if err.field_path == path:
            return err
    return None


def _normalize_one(
    normalizer: Any, raw: str | None, path: str
) -> tuple[Any, NormalizationError | None]:
    """Run a Stage 4 normalizer; return ``(canonical_value_or_None, error_or_None)``."""
    result = normalizer(raw)
    if result.error is not None:
        return None, NormalizationError(
            field_path=path,
            raw_value=raw,
            code=result.error.code,
            message=result.error.message,
        )
    return result.value, None


def _entry(
    operation: CorrectionOperation,
    path: str,
    *,
    previous: Any,
    raw: str | None,
    value: Any,
    error: NormalizationError | None,
) -> CorrectionFieldEntry:
    return CorrectionFieldEntry(
        operation=operation,
        field_path=path,
        previous_value=_str_or_none(previous),
        raw_value=raw,
        normalized_value=_str_or_none(value),
        error_code=error.code if error is not None else None,
        error_message=error.message if error is not None else None,
    )


def merge_correction(
    base: NormalizedInvoice, submitted: SubmittedInvoice
) -> tuple[NormalizedInvoice, list[CorrectionFieldEntry]]:
    """Merge ``submitted`` onto ``base``; return ``(merged, diff_entries)``."""
    merged_scalars: dict[str, Any] = {}
    merged_errors: list[NormalizationError] = []
    scalar_entries: list[CorrectionFieldEntry] = []

    for name in NORMALIZED_SCALAR_FIELD_NAMES:
        raw = submitted.scalars.get(name)
        value, error = _normalize_one(_SCALAR_NORMALIZERS[name], raw, name)
        merged_scalars[name] = value

        base_value = getattr(base, name)
        base_error = _base_error_for(base, name)
        changed = not (
            _values_equal(base_value, value)
            and (base_error.code if base_error else None)
            == (error.code if error else None)
        )
        if changed:
            scalar_entries.append(
                _entry(
                    CorrectionOperation.SET_FIELD,
                    name,
                    previous=base_value if base_error is None else None,
                    raw=raw,
                    value=value,
                    error=error,
                )
            )
        final_error = error if error is not None else (base_error if not changed else None)
        if final_error is not None:
            merged_errors.append(
                final_error
                if final_error.field_path == name
                else final_error.model_copy(update={"field_path": name})
            )

    b = base.line_items
    s = submitted.line_items
    overlap = min(len(b), len(s))

    merged_items: list[dict[str, Any]] = []
    overlap_entries: list[CorrectionFieldEntry] = []
    add_entries: list[CorrectionFieldEntry] = []
    remove_entries: list[CorrectionFieldEntry] = []

    for index in range(len(s)):
        raw_item = s[index]
        leaves: dict[str, Any] = {}
        is_new = index >= len(b)
        if is_new:
            add_entries.append(
                CorrectionFieldEntry(
                    operation=CorrectionOperation.ADD_LINE_ITEM,
                    field_path=f"line_items.{index}",
                    previous_value=None,
                    raw_value=None,
                    normalized_value=None,
                    error_code=None,
                    error_message=None,
                )
            )
        for leaf in NORMALIZED_LINE_ITEM_FIELD_NAMES:
            path = f"line_items.{index}.{leaf}"
            raw = raw_item.get(leaf)
            value, error = _normalize_one(_LINE_ITEM_NORMALIZERS[leaf], raw, path)
            leaves[leaf] = value

            base_value = None if is_new else getattr(b[index], leaf)
            base_error = None if is_new else _base_error_for(base, path)
            changed = is_new or not (
                _values_equal(base_value, value)
                and (base_error.code if base_error else None)
                == (error.code if error else None)
            )
            if changed and (value is not None or error is not None or not is_new):
                target = add_entries if is_new else overlap_entries
                target.append(
                    _entry(
                        CorrectionOperation.SET_FIELD,
                        path,
                        previous=base_value if base_error is None else None,
                        raw=raw,
                        value=value,
                        error=error,
                    )
                )
            final_error = (
                error if error is not None else (base_error if not changed else None)
            )
            if final_error is not None:
                merged_errors.append(
                    final_error
                    if final_error.field_path == path
                    else final_error.model_copy(update={"field_path": path})
                )
        merged_items.append(leaves)

    for index in range(overlap, len(b)):
        dropped = b[index]
        summary = " | ".join(
            _str_or_none(getattr(dropped, leaf)) or ""
            for leaf in NORMALIZED_LINE_ITEM_FIELD_NAMES
        )
        remove_entries.append(
            CorrectionFieldEntry(
                operation=CorrectionOperation.REMOVE_LINE_ITEM,
                field_path=f"line_items.{index}",
                previous_value=summary,
                raw_value=None,
                normalized_value=None,
                error_code=None,
                error_message=None,
            )
        )

    merged = NormalizedInvoice.model_validate(
        {**merged_scalars, "line_items": merged_items, "errors": merged_errors}
    )
    entries = scalar_entries + overlap_entries + add_entries + remove_entries
    return merged, entries
