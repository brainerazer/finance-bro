"""FX-03 property tests — fx_rollup.rollup banker's rounding + no double
conversion (Pitfall 1 + Pitfall 2 / D-14).

Live (Plan 03-03): services/fx_rollup.py now exists; these assert as PASS.

Two properties:
1. ROUND_HALF_EVEN (banker's rounding) at quantize 0.01 matches the locked
   formula exactly.
2. A mono_card row and an nbu row with identical account-amount + day +
   account-currency rate produce IDENTICAL uah_amount_minor — the fx_source
   label is audit-only; the math never triangulates (no double conversion).
"""

from decimal import ROUND_HALF_EVEN, Decimal


def _expected(amount_minor: int, rate: Decimal) -> int:
    major = (Decimal(amount_minor) / 100) * rate
    return int(major.quantize(Decimal("0.01"), ROUND_HALF_EVEN) * 100)


def test_bankers_rounding_matches_locked_formula():
    from finance_bro.services import fx_rollup

    result = fx_rollup.rollup(
        amount_minor=2,
        currency="USD",
        account_currency="USD",
        fx_rate=Decimal("1.255"),
        fx_rate_date=None,
        attributed_day=None,
        op_currency_alpha=None,
    )
    # Assert against the canonical formula so the property holds for the locked
    # ROUND_HALF_EVEN math rather than a hand-computed constant.
    assert result.uah_amount_minor == _expected(2, Decimal("1.255"))


def test_mono_card_and_nbu_identical_when_same_account_amount():
    from finance_bro.services import fx_rollup

    amount_minor = -5000
    rate = Decimal("43.8033")

    nbu = fx_rollup.rollup(
        amount_minor=amount_minor,
        currency="USD",
        account_currency="USD",
        fx_rate=rate,
        fx_rate_date=None,
        attributed_day=None,
        op_currency_alpha="USD",  # op == account -> nbu
    )
    mono_card = fx_rollup.rollup(
        amount_minor=amount_minor,
        currency="USD",
        account_currency="USD",
        fx_rate=rate,
        fx_rate_date=None,
        attributed_day=None,
        op_currency_alpha="EUR",  # op != account -> mono_card label
    )

    assert nbu.fx_source == "nbu"
    assert mono_card.fx_source == "mono_card"
    # Label differs, math is identical (no double conversion, Pitfall 2).
    assert nbu.uah_amount_minor == mono_card.uah_amount_minor
    assert nbu.uah_amount_minor == _expected(amount_minor, rate)
