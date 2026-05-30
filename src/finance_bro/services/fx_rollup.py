"""UAH rollup math (FX-03/FX-04) — pure Decimal, no I/O, no session.

Computes the UAH-equivalent minor-units for a transaction given the joined NBU
rate, plus the fx_source label (D-11) and fx_stale flag (D-13). Lives in
services/ because it is domain logic the route composes per row; it never
touches the DB (the LATERAL join already fetched the rate).

The math is IDENTICAL for `mono_card` and `nbu` rows: `amount_minor x NBU_rate`
where the rate is the NBU rate for the ACCOUNT currency. The `mono_card` label
is audit-only — there is NEVER any triangulation via the operation amount
(FX-04 / Pitfall 2 / Pitfall 8). `account_currency` is accepted for call-site
clarity and parity with the read-row shape; the conversion always uses
`amount_minor` (already in the account currency) x the joined account-currency
rate, so it is informational, not a second conversion leg.

`fx_rate` is transported as a Decimal-as-string (e.g. "43.80330000") — the one
deliberate exception to the "money is always int minor units" rule (CLAUDE.md
§Money). It is never a float on the money path.
"""

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal, getcontext
from typing import Literal

getcontext().prec = 28


@dataclass(frozen=True)
class FxFields:
    uah_amount_minor: int | None
    fx_rate: str | None
    fx_rate_date: date | None
    fx_source: Literal["native_uah", "mono_card", "nbu"]
    fx_stale: bool


def rollup(
    amount_minor: int,
    currency: str,
    account_currency: str | None = None,
    fx_rate: Decimal | None = None,
    fx_rate_date: date | None = None,
    attributed_day: date | None = None,
    op_currency_alpha: str | None = None,
) -> FxFields:
    """Compute the UAH rollup + classification for a single transaction row.

    - native UAH (D-11): UAH rows roll up 1:1, fx_rate "1.00000000", never stale.
    - source label (D-11): "mono_card" when the operation currency differs from
      the account currency (FX-on-card), else "nbu". Audit-only — the math is
      identical either way.
    - no rate (D-12): all FX value fields None, fx_stale True.
    - otherwise (D-14): `major = (Decimal(amount_minor) / 100) * fx_rate`;
      `uah_minor = int(major.quantize(0.01, ROUND_HALF_EVEN) * 100)` (banker's
      rounding to the kopeck); stale (D-13) iff the rate date precedes the
      attributed day.
    """
    if currency == "UAH":
        return FxFields(
            uah_amount_minor=amount_minor,
            fx_rate="1.00000000",
            fx_rate_date=attributed_day,
            fx_source="native_uah",
            fx_stale=False,
        )

    source: Literal["mono_card", "nbu"] = (
        "mono_card" if (op_currency_alpha and op_currency_alpha != currency) else "nbu"
    )

    if fx_rate is None:
        return FxFields(
            uah_amount_minor=None,
            fx_rate=None,
            fx_rate_date=None,
            fx_source=source,
            fx_stale=True,
        )

    # FX-04: account-amount x NBU rate — the rate is the account-currency rate;
    # never triangulate via the operation amount.
    major = (Decimal(amount_minor) / 100) * fx_rate
    uah_minor = int(major.quantize(Decimal("0.01"), ROUND_HALF_EVEN) * 100)
    # D-13: stale iff the rate predates the transaction day. When either date is
    # absent (pure-function callers), treat as fresh.
    stale = (
        fx_rate_date < attributed_day
        if (fx_rate_date is not None and attributed_day is not None)
        else False
    )
    return FxFields(
        uah_amount_minor=uah_minor,
        fx_rate=f"{fx_rate:.8f}",
        fx_rate_date=fx_rate_date,
        fx_source=source,
        fx_stale=stale,
    )
