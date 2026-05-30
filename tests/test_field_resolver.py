"""Field resolver — column vs raw_payload + absent-key safety (D-08 / Pitfall 5).

Pure tests (no DB). The headline case: a card transaction whose raw_payload lacks
`counterIban` resolves `counter_iban` to None and a condition over it cleanly
DOES NOT MATCH (never KeyError) — T-4-payload.
"""

from finance_bro.categorizer import IContains, InInt, eval_condition, make_row
from finance_bro.categorizer.fields import make_row as make_row_fields


def test_column_backed_fields_resolve_from_columns():
    row = make_row(description="ATB", mcc=5411, account_id=42, currency="UAH")
    assert row.text("description") == "ATB"
    assert row.int_field("mcc") == 5411
    assert row.int_field("account_id") == 42
    assert row.str_field("currency") == "UAH"


def test_raw_payload_text_fields_resolve_via_get():
    row = make_row(
        raw_payload={
            "comment": "rent",
            "counterIban": "UA12345",
            "counterEdrpou": "99887766",
        }
    )
    assert row.text("comment") == "rent"
    assert row.text("counter_iban") == "UA12345"
    assert row.text("counter_edrpou") == "99887766"


def test_original_mcc_resolves_and_coerces_from_raw_payload():
    row = make_row(mcc=5411, raw_payload={"originalMcc": 4829})
    assert row.int_field("original_mcc") == 4829
    # string-typed payload value is coerced
    row2 = make_row(raw_payload={"originalMcc": "4829"})
    assert row2.int_field("original_mcc") == 4829


def test_absent_counter_iban_resolves_none_and_condition_does_not_match():
    # card transaction — no counterparty fields in raw_payload
    card = make_row(description="Coffee", mcc=5814, raw_payload={})
    assert card.text("counter_iban") is None
    # a rule referencing counter_iban must cleanly NOT match (never KeyError)
    cond = IContains(field="counter_iban", value="UA")
    assert eval_condition(cond, card) is False


def test_absent_original_mcc_is_none():
    row = make_row(raw_payload={})
    assert row.int_field("original_mcc") is None
    assert eval_condition(InInt(field="original_mcc", values=[4829]), row) is False


def test_make_row_exposed_from_fields_module():
    # the helper is importable both from the package and from fields directly
    row = make_row_fields(amount_minor=-100)
    assert row.amount_minor == -100
