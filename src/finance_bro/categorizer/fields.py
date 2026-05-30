"""Field resolver — column vs raw_payload (D-08 / Pitfall 5).

`RowView` adapts a transaction (its first-class columns + the Mono `raw_payload`
JSONB) into the small read surface the interpreter needs. Column-backed fields
(`description`, `mcc`, `account_id`, `currency`, `hold`, `amount_minor`) read
attributes directly; raw_payload-backed fields (`comment`, `counter_iban`,
`counter_edrpou`, `original_mcc`) read via `raw_payload.get(...)` and return
None on absence — mirroring the never-raise `.get()` discipline of
`transaction_repo._op_currency_alpha`. A condition over a None value evaluates
False (no match), never KeyError (T-4-payload / Pitfall 5).

This module is PURE — no SQLAlchemy, no DB connection imports.
"""

from dataclasses import dataclass, field
from typing import Any

# Mono raw_payload key names for the text fields (verified against
# tests/fixtures/statement_two_items.json + Mono docs). counterIban /
# counterEdrpou are FOP-only and frequently absent on card rows.
_RAW_TEXT: dict[str, str] = {
    "comment": "comment",
    "counter_iban": "counterIban",
    "counter_edrpou": "counterEdrpou",
}


@dataclass(frozen=True)
class RowView:
    """A pure, read-only adapter over a transaction's categorization inputs."""

    id: int
    amount_minor: int
    hold: bool
    is_user_locked: bool
    category_id: int | None = None
    # column-backed optional fields
    description: str | None = None
    mcc: int | None = None
    account_id: int | None = None
    currency: str | None = None
    # the Mono statementItem payload
    raw_payload: dict[str, Any] = field(default_factory=dict)

    def text(self, field_name: str) -> str | None:
        """Resolve a TextField. description is a column; the rest are raw_payload
        keys read with `.get()` (None on absence — Pitfall 5)."""
        if field_name == "description":
            return self.description
        return self.raw_payload.get(_RAW_TEXT[field_name])

    def int_field(self, field_name: str) -> int | None:
        """Resolve an IntSetField. mcc/account_id are columns; original_mcc is a
        raw_payload key, coerced to int (None on absence or non-coercible)."""
        if field_name == "mcc":
            return self.mcc
        if field_name == "account_id":
            return self.account_id
        # original_mcc
        v = self.raw_payload.get("originalMcc")
        if v is None:
            return None
        try:
            return int(v)
        except (ValueError, TypeError):
            return None

    def str_field(self, field_name: str) -> str | None:
        """Resolve a StrSetField. currency is a column."""
        if field_name == "currency":
            return self.currency
        return None


def make_row(
    *,
    id: int = 1,
    amount_minor: int = 0,
    hold: bool = False,
    is_user_locked: bool = False,
    category_id: int | None = None,
    description: str | None = None,
    mcc: int | None = None,
    account_id: int | None = None,
    currency: str | None = None,
    raw_payload: dict[str, Any] | None = None,
) -> RowView:
    """Construct a RowView for pure unit tests (and any pure caller)."""
    return RowView(
        id=id,
        amount_minor=amount_minor,
        hold=hold,
        is_user_locked=is_user_locked,
        category_id=category_id,
        description=description,
        mcc=mcc,
        account_id=account_id,
        currency=currency,
        raw_payload=raw_payload if raw_payload is not None else {},
    )
