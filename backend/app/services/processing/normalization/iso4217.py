"""Approved ISO 4217 currency codes (Stage 4, step 6/8).

A vendored, hand-maintained allow-list. There is **no network access** - the
normalizer checks membership in this frozen set and nothing else.

Contents: the current ISO 4217 alphabetic codes for circulating national
currencies, plus the genuine multi-country codes (`EUR`, `XAF`, `XCD`, `XCG`,
`XOF`, `XPF`). Deliberately excluded, so they normalize to `unknown_currency`:

* precious-metal codes - `XAU`, `XAG`, `XPT`, `XPD`;
* the "no currency" and testing codes - `XXX`, `XTS`;
* supranational / settlement units - `XDR`, `XSU`, `XUA`, `XBA`-`XBD`;
* fund codes that are not a spendable currency - `BOV`, `CHE`, `CHW`, `CLF`,
  `COU`, `MXV`, `USN`, `UYI`, `UYW`;
* obsolete / withdrawn codes - `DEM`, `FRF`, `ITL`, `ESP`, `NLG`, `EEK`, …

Snapshot checked against the ISO 4217 Maintenance Agency publications through
1 January 2026. In particular, `ANG`, `BGN`, `SLL`, and `ZWL` are historical;
their replacements `XCG`, `EUR`, `SLE`, and `ZWG` are included.

Review this list when ISO 4217 changes (a new currency, a redenomination).
"""

from __future__ import annotations

APPROVED_CURRENCY_CODES: frozenset[str] = frozenset(
    {
        "AED", "AFN", "ALL", "AMD", "AOA", "ARS", "AUD", "AWG", "AZN",
        "BAM", "BBD", "BDT", "BHD", "BIF", "BMD", "BND", "BOB", "BRL",
        "BSD", "BTN", "BWP", "BYN", "BZD",
        "CAD", "CDF", "CHF", "CLP", "CNY", "COP", "CRC", "CUP", "CVE", "CZK",
        "DJF", "DKK", "DOP", "DZD",
        "EGP", "ERN", "ETB", "EUR",
        "FJD", "FKP",
        "GBP", "GEL", "GHS", "GIP", "GMD", "GNF", "GTQ", "GYD",
        "HKD", "HNL", "HTG", "HUF",
        "IDR", "ILS", "INR", "IQD", "IRR", "ISK",
        "JMD", "JOD", "JPY",
        "KES", "KGS", "KHR", "KMF", "KPW", "KRW", "KWD", "KYD", "KZT",
        "LAK", "LBP", "LKR", "LRD", "LSL", "LYD",
        "MAD", "MDL", "MGA", "MKD", "MMK", "MNT", "MOP", "MRU", "MUR", "MVR",
        "MWK", "MXN", "MYR", "MZN",
        "NAD", "NGN", "NIO", "NOK", "NPR", "NZD",
        "OMR",
        "PAB", "PEN", "PGK", "PHP", "PKR", "PLN", "PYG",
        "QAR",
        "RON", "RSD", "RUB", "RWF",
        "SAR", "SBD", "SCR", "SDG", "SEK", "SGD", "SHP", "SLE", "SOS",
        "SRD", "SSP", "STN", "SVC", "SYP", "SZL",
        "THB", "TJS", "TMT", "TND", "TOP", "TRY", "TTD", "TWD", "TZS",
        "UAH", "UGX", "USD", "UYU", "UZS",
        "VED", "VES", "VND", "VUV",
        "WST",
        "XAF", "XCD", "XCG", "XOF", "XPF",
        "YER",
        "ZAR", "ZMW", "ZWG",
    }
)

__all__ = ["APPROVED_CURRENCY_CODES"]
