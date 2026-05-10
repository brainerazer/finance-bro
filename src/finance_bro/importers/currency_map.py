"""ISO-4217 numeric -> alpha mapping for the importer boundary.

Mono returns numeric `currencyCode` (e.g. 980 for UAH); the rest of the codebase
works in alpha codes (UAH/USD/EUR) per the schema's `CHAR(3) currency` column.
This module is the single source of truth for that conversion.
"""

_NUM_TO_ALPHA: dict[int, str] = {
    980: "UAH",
    840: "USD",
    978: "EUR",
}


def numeric_to_alpha(code: int) -> str:
    try:
        return _NUM_TO_ALPHA[code]
    except KeyError as e:
        raise ValueError(f"Unknown ISO 4217 numeric code: {code}") from e
