"""Reusable deterministic field normalizers (Stage 4, step 6).

Each public function takes one raw extracted value and returns a
:class:`NormResult` with exactly one of three outcomes:

* **normalized** - ``value`` is the canonical value, ``error`` is ``None``;
* **absent** - both ``value`` and ``error`` are ``None`` (the source was
  ``None``, empty, or whitespace-only - not a failure);
* **failed** - ``value`` is ``None`` and ``error`` is a :class:`FieldError`
  carrying a closed ``code`` and a client-safe ``message``.

These functions know nothing about which field they are normalizing; the
attempt engine (step 11) attaches ``field_path`` and the stringified
``raw_value`` to build a full
:class:`app.schemas.normalization.NormalizationError`.

Every rule here is fixed by "Normalization policies (decided - step 2)" in
``docs/stage-4-normalization.md``. **No AI, no I/O, no network.**
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

from app.schemas.normalization import NormalizationErrorCode
from app.services.processing.normalization.iso4217 import APPROVED_CURRENCY_CODES

__all__ = [
    "FieldError",
    "NormResult",
    "clean_text",
    "MAX_INVOICE_NUMBER",
    "MAX_TAX_ID",
    "MAX_PARTY_NAME",
    "MAX_LINE_ITEM_DESCRIPTION",
    "normalize_date",
    "normalize_currency",
    "normalize_money",
    "normalize_quantity",
    "normalize_text",
    "normalize_invoice_number",
    "normalize_tax_id",
]

# Text length caps (Unicode characters, after cleanup). Must match the varchar
# lengths in app/models/normalization.py and the table in
# docs/stage-4-normalization.md; a test asserts they line up.
MAX_INVOICE_NUMBER = 100
MAX_TAX_ID = 60
MAX_PARTY_NAME = 256
MAX_LINE_ITEM_DESCRIPTION = 512


@dataclass(frozen=True, slots=True)
class FieldError:
    """A field-level normalization failure: a closed code and a safe message."""

    code: NormalizationErrorCode
    message: str


@dataclass(frozen=True, slots=True)
class NormResult:
    """Outcome of normalizing one field. See the module docstring."""

    value: object | None = None
    error: FieldError | None = None

    def __post_init__(self) -> None:
        if self.value is not None and self.error is not None:
            raise ValueError("a normalization result cannot contain both value and error")

    @property
    def ok(self) -> bool:
        """True when normalization produced a value or a clean absence."""
        return self.error is None


def _ok(value: object) -> NormResult:
    return NormResult(value=value)


_ABSENT = NormResult()


def _fail(code: NormalizationErrorCode, message: str) -> NormResult:
    return NormResult(error=FieldError(code, message))


# --- shared text cleanup ------------------------------------------------

_REMOVED_FORMAT_CHARS = frozenset(chr(codepoint) for codepoint in range(0x200B, 0x200E)) | {
    "\ufeff"
}


def clean_text(raw: str) -> str:
    """The shared text cleanup for the free-text and identifier fields.

    Exactly the five steps of the text policy in
    ``docs/stage-4-normalization.md`` and nothing more:

    1. NFC normalize.
    2. Every Unicode whitespace character (tab, newline, no-break / narrow /
       figure space, line and paragraph separators, …) becomes a plain space.
    3. Remove the zero-width and BOM characters the policy names
       (``U+200B``-``U+200D``, ``U+FEFF``) and C0/C1 control characters.
    4. Collapse runs of spaces to one.
    5. Trim the ends.

    Deliberately **not** removed: other format characters the policy does not
    name - word joiner (``U+2060``), bidi marks (``U+200E``/``U+200F``,
    ``U+202A``-``U+202E``), soft hyphen (``U+00AD``). They survive so a value
    that carries them is not silently "repaired". Case, punctuation, accents,
    quotes and dashes are also left exactly as they are.
    """
    normalized = unicodedata.normalize("NFC", raw)
    out: list[str] = []
    for ch in normalized:
        if ch.isspace():
            out.append(" ")
        elif unicodedata.category(ch) == "Cc" or ch in _REMOVED_FORMAT_CHARS:
            continue
        else:
            out.append(ch)
    return re.sub(r" {2,}", " ", "".join(out)).strip()


# --- general text and identifiers -------------------------------------


def normalize_text(raw: str | None, *, max_length: int) -> NormResult:
    """Clean general free text: trim, fold whitespace, drop the named invisible
    characters, collapse repeated spaces.

    * ``None``, empty, or whitespace-only -> **absent** (value ``None``, no
      error). An absent value is not a failure.
    * Longer than ``max_length`` characters (measured *after* cleanup) ->
      ``text_too_long``. The value is **never truncated** - silently shortening
      a name or an identifier corrupts data, so bad extraction is surfaced
      instead.
    * Otherwise the cleaned string is returned. Case, punctuation, accents,
      digits and symbols are preserved verbatim; nothing is parsed or
      re-typed.

    Used for `vendor_name`, `customer_name`, line-item `description`, and (via
    the wrappers below) the identifier fields.
    """
    if raw is None:
        return _ABSENT
    if not isinstance(raw, str):
        # Stage 3 guarantees str | None. Silently stringifying a contract-
        # invalid object could persist an invented identifier (for example,
        # b"INV-1" becoming "b'INV-1'"). Let the service classify this
        # upstream contract violation as a technical failure instead.
        raise TypeError("text normalization requires a string or None")
    cleaned = clean_text(raw)
    if not cleaned:
        return _ABSENT
    if len(cleaned) > max_length:
        return _fail(
            NormalizationErrorCode.TEXT_TOO_LONG,
            f"This value is longer than the {max_length}-character maximum.",
        )
    return _ok(cleaned)


def normalize_invoice_number(raw: str | None) -> NormResult:
    """Invoice number: the shared whitespace cleanup and nothing else. Internal
    spaces and separators (`INV 2026 / 0007`) carry meaning and are preserved;
    the value stays a string - it is never parsed to a number or stripped of
    leading zeros (`007` stays `007`)."""
    return normalize_text(raw, max_length=MAX_INVOICE_NUMBER)


def normalize_tax_id(raw: str | None) -> NormResult:
    """Tax identifier: the shared whitespace cleanup and nothing else
    (`DE 123 456 789` keeps its groups). It stays a string and is never
    re-typed, re-cased, or reformatted."""
    return normalize_text(raw, max_length=MAX_TAX_ID)


# --- currency --------------------------------------------------------

# Exactly three ASCII letters. Checked on the trimmed string *before*
# upper-casing so a non-ASCII letter whose upper-case is longer (e.g. "ß" ->
# "SS") can never widen a two-character junk value into a 3-letter "code".
# The currency policy allows only trimming and upper-casing - NOT the wider
# `clean_text` cleanup - so a hidden zero-width or control character inside the
# value leaves it non-matching here and is reported, not silently repaired.
_CURRENCY_CODE_RE = re.compile(r"[A-Za-z]{3}")


def normalize_currency(raw: str | None) -> NormResult:
    """Trim, require exactly three ASCII letters, upper-case, then require
    membership in the approved ISO 4217 allow-list
    (:data:`APPROVED_CURRENCY_CODES`).

    * A missing / blank currency is **absent** - never defaulted to `EUR` or
      anything else (there is no configuration switch for that), and never
      inferred from a currency symbol: Stage 4 does no symbol interpretation
      because no unambiguous rule exists for it.
    * A value that is not three ASCII letters -> ``invalid_currency``.
    * A well-formed code that is not on the allow-list (obsolete, withdrawn,
      precious-metal, supranational, or "no currency" codes) ->
      ``unknown_currency``.
    * No currency conversion is ever performed.
    """
    if raw is None:
        return _ABSENT
    if not isinstance(raw, str):
        # The contract guarantees str | None; anything else is malformed
        # upstream. Surface it as a field error, never crash the attempt.
        return _fail(
            NormalizationErrorCode.INVALID_CURRENCY,
            "This is not a valid three-letter currency code.",
        )
    cleaned = raw.strip()
    if not cleaned:
        return _ABSENT
    if not _CURRENCY_CODE_RE.fullmatch(cleaned):
        return _fail(
            NormalizationErrorCode.INVALID_CURRENCY,
            "This is not a valid three-letter currency code.",
        )
    code = cleaned.upper()
    if code not in APPROVED_CURRENCY_CODES:
        return _fail(
            NormalizationErrorCode.UNKNOWN_CURRENCY,
            "This currency code is not a recognized ISO 4217 currency.",
        )
    return _ok(code)


# --- money and quantities ------------------------------------------
#
# In the current pipeline these arrive already parsed to ``Decimal`` by the
# Stage 3 extraction contract, so the common path just checks finiteness and
# preserves sign and scale (no rounding). The string path implements the
# documented separator policy for any future provider that hands over text.

_PLAIN_NUMBER_RE = re.compile(r"[0-9]+(\.[0-9]+)?")
_COMMA_GROUPED_RE = re.compile(r"[0-9]{1,3}(,[0-9]{3})+(\.[0-9]+)?")
_SPACE_GROUPED_RE = re.compile(r"[0-9]{1,3}( [0-9]{3})+(\.[0-9]+)?")
_DECIMAL_COMMA_RE = re.compile(
    r"(?:[0-9]+|[0-9]{1,3}(?:\.[0-9]{3})+|[0-9]{1,3}(?: [0-9]{3})+),[0-9]+"
)

# The two no-break space variants the number policy accepts as a grouping
# separator, folded to a plain ASCII space. This is the *only* character
# substitution the number path performs.
_GROUPING_SPACES = str.maketrans({" ": " ", " ": " "})


def _invalid_number(kind: str) -> NormResult:
    return _fail(NormalizationErrorCode.INVALID_NUMBER, f"This {kind} is not a valid number.")


def _strip_currency_symbols(value: str) -> str:
    """Remove Unicode currency-symbol affixes, but never symbols within digits."""
    start = 0
    end = len(value)
    while start < end and (
        value[start].isspace() or unicodedata.category(value[start]) == "Sc"
    ):
        start += 1
    while end > start and (
        value[end - 1].isspace() or unicodedata.category(value[end - 1]) == "Sc"
    ):
        end -= 1
    return value[start:end].strip()


def _parse_decimal_string(raw: str, *, kind: str) -> NormResult:
    # The number policy allows exactly two clean-ups: the no-break space
    # variants that may be used for grouping are folded to a plain space, and
    # leading / trailing whitespace is trimmed. Nothing else is stripped or
    # collapsed - a zero-width, control, or repeated whitespace character left
    # inside the value makes it malformed (``invalid_number``), never silently
    # repaired. (Do not swap this for ``clean_text``: that would quietly fix
    # hidden characters the currency policy already forbids fixing, and the
    # number policy is the same in spirit.)
    s = raw.translate(_GROUPING_SPACES).strip()
    if not s:
        return _ABSENT

    sign_forms = (
        s.startswith("(") and s.endswith(")"),
        s.endswith("-"),
        s.startswith("-"),
        s.startswith("+"),
    )
    if sum(sign_forms) > 1 or s.startswith("(") != s.endswith(")"):
        return _invalid_number(kind)
    negative = sign_forms[0] or sign_forms[1] or sign_forms[2]
    if sign_forms[0]:
        s = s[1:-1].strip()
    elif sign_forms[1]:
        s = s[:-1].strip()
    elif sign_forms[2] or sign_forms[3]:
        s = s[1:].strip()

    # strip an embedded 3-letter code and any leading/trailing currency symbol
    s = re.sub(r"^[A-Za-z]{3}\b\s*", "", s)
    s = re.sub(r"\s*\b[A-Za-z]{3}$", "", s)
    s = _strip_currency_symbols(s)

    if not s:
        return _invalid_number(kind)

    if _PLAIN_NUMBER_RE.fullmatch(s):
        digits = s
    elif _COMMA_GROUPED_RE.fullmatch(s):
        digits = s.replace(",", "")
    elif _SPACE_GROUPED_RE.fullmatch(s):
        digits = s.replace(" ", "")
    elif _DECIMAL_COMMA_RE.fullmatch(s):
        return _fail(
            NormalizationErrorCode.AMBIGUOUS_NUMBER,
            f"This {kind}'s decimal separator could be read more than one way.",
        )
    else:
        return _invalid_number(kind)

    try:
        dec = Decimal(digits)
    except InvalidOperation:
        return _invalid_number(kind)
    if not dec.is_finite():
        return _invalid_number(kind)
    # ``-dec`` applies the active Decimal context and can round a high-precision
    # value. ``copy_negate`` changes only the sign and preserves coefficient and
    # exponent exactly, as required by the no-rounding policy.
    return _ok(dec.copy_negate() if negative else dec)


def _normalize_decimal(raw: object | None, *, kind: str) -> NormResult:
    if raw is None:
        return _ABSENT
    if isinstance(raw, str):
        return _parse_decimal_string(raw, kind=kind)
    if isinstance(raw, bool):  # bool is an int subclass - reject it explicitly
        return _invalid_number(kind)
    if isinstance(raw, Decimal):
        if not raw.is_finite():
            return _invalid_number(kind)
        return _ok(raw)  # sign and scale preserved, never rounded
    return _invalid_number(kind)


def normalize_money(raw: object | None) -> NormResult:
    """A monetary amount. Any finite ``Decimal`` is accepted as-is (sign and
    scale preserved, no rounding); a string is parsed under the documented
    period-decimal / comma-or-space-grouping policy."""
    return _normalize_decimal(raw, kind="amount")


def normalize_quantity(raw: object | None) -> NormResult:
    """A line-item quantity. Same rules as :func:`normalize_money`."""
    return _normalize_decimal(raw, kind="quantity")


# --- dates ---------------------------------------------------------

_MONTHS: dict[str, int] = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}

_ORDINAL_TOKEN_RE = re.compile(
    r"(?<![0-9])([0-9]{1,2})(st|nd|rd|th)\b", re.IGNORECASE
)
_YEAR_FIRST_RE = re.compile(r"([0-9]{4})([-/.])([0-9]{1,2})\2([0-9]{1,2})")
_NUMERIC_YEAR_LAST_RE = re.compile(
    r"([0-9]{1,2})([-/.])([0-9]{1,2})\2([0-9]{4})"
)
_TWO_DIGIT_YEAR_RE = re.compile(r"[0-9]{1,2}([-/.])[0-9]{1,2}\1[0-9]{2}")
_DAY_MONTH_YEAR_RE = re.compile(
    r"([0-9]{1,2})([ -])([A-Za-z]{3,9})\2([0-9]{4})"
)
_MONTH_DAY_YEAR_RE = re.compile(
    r"([A-Za-z]{3,9})\s+([0-9]{1,2}),?\s+([0-9]{4})"
)


def _clean_date_input(raw: str) -> str:
    """Apply only the date policy's trim and whitespace-collapse rules."""
    return " ".join(raw.split())


def _ordinal_suffix(day: int) -> str:
    if 10 < day % 100 < 14:
        return "th"
    return {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")


def _strip_day_ordinal(raw: str) -> str | None:
    """Strip one valid ordinal suffix only when attached to the day token."""
    matches = list(_ORDINAL_TOKEN_RE.finditer(raw))
    if not matches:
        return raw
    if len(matches) != 1:
        return None

    ordinal = matches[0]
    day = int(ordinal[1])
    if ordinal[2].lower() != _ordinal_suffix(day):
        return None

    candidate = raw[: ordinal.start(2)] + raw[ordinal.end(2) :]
    numeric_tokens = list(re.finditer(r"[0-9]+", raw))
    ordinal_token_index = next(
        (index for index, token in enumerate(numeric_tokens) if token.start() == ordinal.start(1)),
        None,
    )

    if _YEAR_FIRST_RE.fullmatch(candidate):
        expected_day_index = 2
    elif (numeric_match := _NUMERIC_YEAR_LAST_RE.fullmatch(candidate)) is not None:
        first, second = int(numeric_match[1]), int(numeric_match[3])
        expected_day_index = 1 if second > 12 and first <= 12 else 0
    elif _DAY_MONTH_YEAR_RE.fullmatch(candidate) or _MONTH_DAY_YEAR_RE.fullmatch(candidate):
        expected_day_index = 0
    else:
        return None

    return candidate if ordinal_token_index == expected_day_index else None


def _bad_date() -> NormResult:
    return _fail(
        NormalizationErrorCode.INVALID_DATE,
        "This date could not be recognized as a valid calendar date.",
    )


def _to_iso(year: int, month: int, day: int) -> NormResult:
    try:
        return _ok(date(year, month, day).isoformat())
    except ValueError:
        return _bad_date()


def normalize_date(raw: str | None) -> NormResult:
    """Parse a raw date string into canonical ``YYYY-MM-DD``.

    Accepts year-first numeric (`2026/1/5`), English month-name forms
    (`15 Jan 2026`, `January 15, 2026`), and all-numeric year-last dates. Every
    parse is checked against the real calendar with :class:`datetime.date`, so
    ``31/02/2026``, ``Feb 30, 2026`` and ``29/02/2026`` (non-leap) all fail.

    Order resolution for all-numeric year-last dates:

    * if exactly one of the first two components is > 12 it is the day;
    * otherwise the order is undetermined and the date is read **day-first**
      (`DD/MM/YYYY`). This is a fixed policy default applied identically to
      every source - it is *not* locale inference and records no error.

    Two-digit years, unrecognized formats, weekday/quarter notation, and
    impossible calendar dates all fail as ``invalid_date``. A ``None`` or
    blank input is absent (no value, no error). The Stage 3 raw string is
    never modified.
    """
    if raw is None:
        return _ABSENT
    if not isinstance(raw, str):
        # The contract guarantees str | None; anything else is malformed
        # upstream. Surface it as a field error, never crash the attempt.
        return _bad_date()
    s = _clean_date_input(raw)
    if not s:
        return _ABSENT

    if s.endswith("."):
        s = s[:-1].strip()
    stripped = _strip_day_ordinal(s)
    if stripped is None:
        return _bad_date()
    s = stripped

    m = _YEAR_FIRST_RE.fullmatch(s)
    if m:
        return _to_iso(int(m[1]), int(m[3]), int(m[4]))

    m = _NUMERIC_YEAR_LAST_RE.fullmatch(s)
    if m:
        a, b, year = int(m[1]), int(m[3]), int(m[4])
        if a > 12 and b <= 12:
            day, month = a, b
        elif b > 12 and a <= 12:
            day, month = b, a
        elif a <= 12 and b <= 12:
            day, month = a, b  # day/month order undetermined -> day-first
        else:
            return _bad_date()
        return _to_iso(year, month, day)

    if _TWO_DIGIT_YEAR_RE.fullmatch(s):
        return _bad_date()

    m = _DAY_MONTH_YEAR_RE.fullmatch(s)
    if m and m[3].lower() in _MONTHS:
        return _to_iso(int(m[4]), _MONTHS[m[3].lower()], int(m[1]))

    m = _MONTH_DAY_YEAR_RE.fullmatch(s)
    if m:
        name = m[1].lower()
        if name in _MONTHS:
            return _to_iso(int(m[3]), _MONTHS[name], int(m[2]))

    return _bad_date()
