import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.asyncio
async def test_second_import_is_noop(client):
    ci = json.loads((FIXTURES / "client_info_minimal.json").read_text())
    stmt = json.loads((FIXTURES / "statement_two_items.json").read_text())
    with (
        respx.mock(base_url="https://api.monobank.ua") as mock,
        patch(
            "finance_bro.importers.rate_limit.asyncio.sleep",
            new_callable=AsyncMock,
        ),
    ):
        mock.get("/personal/client-info").mock(return_value=httpx.Response(200, json=ci))
        mock.get(url__regex=r"/personal/statement/.*").mock(
            return_value=httpx.Response(200, json=stmt)
        )
        first = await client.post("/api/import")
        second = await client.post("/api/import")
    assert first.status_code == 200
    assert second.status_code == 200
    f, s = first.json(), second.json()
    # Phase 2 (02-02): insert_many uses ON CONFLICT DO UPDATE. ImportService folds
    # `(inserted, updated_in_place)` into Phase 1's single `inserted` field as
    # `inserted_total = inserted + updated`. The user-visible no-op (one row per
    # Mono id) is preserved — second import touches both rows via UPDATE rather
    # than skipping them via DO NOTHING, so `inserted` reports 2 and
    # `skipped_duplicates` is 0. The single-row invariant is the SC#3 contract.
    assert f["inserted"] == 2 and f["skipped_duplicates"] == 0
    assert s["inserted"] == 2 and s["skipped_duplicates"] == 0
    # And only 2 rows actually exist
    r = await client.get("/api/transactions")
    assert len(r.json()) == 2
