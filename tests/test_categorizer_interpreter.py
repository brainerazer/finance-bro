"""Per-op truth table for the categorizer interpreter — CAT-01 (pure, no DB).

Exercises every op in the closed vocabulary (D-05) plus the canonical ATB
example. No Postgres / conftest fixtures — pure `make_row(...)`.
"""

from finance_bro.categorizer import (
    AmountRange,
    AmountSign,
    Equals,
    HoldIs,
    IContains,
    InInt,
    InStr,
    RulePredicate,
    eval_condition,
    make_row,
)


def test_icontains_is_case_insensitive_substring():
    row = make_row(description="ATB Market #14")
    assert eval_condition(IContains(field="description", value="atb"), row) is True
    assert eval_condition(IContains(field="description", value="MARKET"), row) is True
    assert eval_condition(IContains(field="description", value="silpo"), row) is False


def test_icontains_none_field_is_false_not_error():
    row = make_row(description=None)
    assert eval_condition(IContains(field="description", value="atb"), row) is False


def test_equals_is_case_sensitive_exact():
    row = make_row(description="ATB", currency="USD")
    assert eval_condition(Equals(field="description", value="ATB"), row) is True
    assert eval_condition(Equals(field="description", value="atb"), row) is False
    assert eval_condition(Equals(field="currency", value="USD"), row) is True


def test_in_int_membership():
    row = make_row(mcc=5411)
    assert eval_condition(InInt(field="mcc", values=[5411, 5499]), row) is True
    assert eval_condition(InInt(field="mcc", values=[4111, 4121]), row) is False


def test_in_int_none_field_is_false():
    row = make_row(mcc=None)
    assert eval_condition(InInt(field="mcc", values=[5411]), row) is False


def test_in_str_membership():
    row = make_row(currency="EUR")
    assert eval_condition(InStr(field="currency", values=["USD", "EUR"]), row) is True
    assert eval_condition(InStr(field="currency", values=["USD"]), row) is False


def test_amount_sign_debit_and_credit():
    debit = make_row(amount_minor=-1500)
    credit = make_row(amount_minor=5000000)
    assert eval_condition(AmountSign(sign="debit"), debit) is True
    assert eval_condition(AmountSign(sign="credit"), debit) is False
    assert eval_condition(AmountSign(sign="credit"), credit) is True
    assert eval_condition(AmountSign(sign="debit"), credit) is False


def test_amount_range_inclusive_bounds():
    row = make_row(amount_minor=-1500)
    assert eval_condition(AmountRange(min_minor=-2000, max_minor=-1000), row) is True
    assert eval_condition(AmountRange(min_minor=-1500, max_minor=-1500), row) is True
    assert eval_condition(AmountRange(min_minor=-1000), row) is False
    assert eval_condition(AmountRange(max_minor=-2000), row) is False
    # open-ended both directions == always true
    assert eval_condition(AmountRange(), row) is True


def test_hold_is():
    held = make_row(hold=True)
    settled = make_row(hold=False)
    assert eval_condition(HoldIs(value=True), held) is True
    assert eval_condition(HoldIs(value=True), settled) is False
    assert eval_condition(HoldIs(value=False), settled) is True


def test_canonical_atb_predicate_matches_atb_rejects_silpo():
    # mcc IN [5411,5499] AND amount_minor < 0 AND description ICONTAINS "ATB"
    predicate = RulePredicate(
        all=[
            InInt(field="mcc", values=[5411, 5499]),
            AmountSign(sign="debit"),
            IContains(field="description", value="ATB"),
        ]
    )
    atb = make_row(mcc=5411, amount_minor=-1500, description="ATB Market")
    silpo = make_row(mcc=5411, amount_minor=-1500, description="Silpo")

    assert all(eval_condition(c, atb) for c in predicate.all) is True
    assert all(eval_condition(c, silpo) for c in predicate.all) is False
