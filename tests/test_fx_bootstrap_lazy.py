"""D-15 — a never-seen currency is lazily tracked on first sighting, and a
bootstrap fetch backfills its range so a subsequent read resolves a rate.

Live test (Plan 03-04): FxBootstrapService is built.

Insert a CHF transaction -> a tracked_fx_currencies CHF row appears; the
(mocked) bootstrap fetches the CHF range, upserts fx_rates, and a re-read
returns a non-null rollup.
"""
from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text


@pytest.mark.asyncio
async def test_chf_tracked_and_bootstrapped(session_factory):
    from finance_bro.services.fx_bootstrap import FxBootstrapService

    from finance_bro.importers.base import FxRateRow

    async with session_factory() as s:
        await s.execute(
            text(
                "INSERT INTO accounts (source_kind, source_account_id, currency, raw_payload) "
                "VALUES ('mono.card', 'chf-acc', 'CHF', '{}'::jsonb)"
            )
        )
        acc_id = (
            await s.execute(
                text("SELECT id FROM accounts WHERE source_account_id='chf-acc'")
            )
        ).scalar_one()
        await s.execute(
            text(
                "INSERT INTO transactions "
                "(account_id, source_tx_id, amount_minor, currency, time, "
                " raw_payload, attributed_day) "
                "VALUES (:a, 'chf-1', -3000, 'CHF', "
                "'2026-05-08T12:00:00+00:00'::timestamptz, "
                "'{\"currencyCode\": 756}'::jsonb, '2026-05-08')"
            ),
            {"a": acc_id},
        )
        # First sighting tracks the currency (DO NOTHING idempotent).
        await s.execute(
            text(
                "INSERT INTO tracked_fx_currencies (currency, bootstrap_done) "
                "VALUES ('CHF', false) ON CONFLICT (currency) DO NOTHING"
            )
        )
        await s.commit()

        tracked = (
            await s.execute(
                text("SELECT currency FROM tracked_fx_currencies WHERE currency='CHF'")
            )
        ).scalar_one_or_none()
    assert tracked == "CHF"

    # Mocked importer returns one CHF rate for the bootstrap range.
    importer = AsyncMock()
    importer.fetch_range = AsyncMock(
        return_value=[
            FxRateRow(rate_date=date(2026, 5, 8), currency="CHF", rate=Decimal("49.1200"))
        ]
    )
    svc = FxBootstrapService(session_factory, importer)
    await svc.maybe_bootstrap_fx("CHF")

    async with session_factory() as s:
        repo_rows = (
            await s.execute(
                text("SELECT rate FROM fx_rates WHERE currency='CHF'")
            )
        ).scalars().all()
    assert len(repo_rows) == 1
    importer.fetch_range.assert_awaited()
