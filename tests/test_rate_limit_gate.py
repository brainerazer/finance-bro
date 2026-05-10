import asyncio
from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_within_window(session_factory):
    from finance_bro.importers.rate_limit import RateLimitGate

    gate = RateLimitGate(session_factory)
    with patch(
        "finance_bro.importers.rate_limit.asyncio.sleep",
        new_callable=AsyncMock,
    ) as mock_sleep:
        await gate.acquire("tok-aaa")
        await gate.acquire("tok-aaa")
    assert mock_sleep.await_count == 1
    assert mock_sleep.await_args.args[0] >= 60


@pytest.mark.asyncio
async def test_persists_across_restart(session_factory):
    from finance_bro.importers.rate_limit import RateLimitGate

    gate_a = RateLimitGate(session_factory)
    with patch(
        "finance_bro.importers.rate_limit.asyncio.sleep",
        new_callable=AsyncMock,
    ):
        await gate_a.acquire("tok-restart")
    del gate_a
    gate_b = RateLimitGate(session_factory)
    with patch(
        "finance_bro.importers.rate_limit.asyncio.sleep",
        new_callable=AsyncMock,
    ) as mock_sleep:
        await gate_b.acquire("tok-restart")
    assert mock_sleep.await_count == 1
    assert mock_sleep.await_args.args[0] >= 60


@pytest.mark.asyncio
async def test_concurrent_serialize(session_factory):
    from finance_bro.importers.rate_limit import RateLimitGate

    gate = RateLimitGate(session_factory)
    with patch(
        "finance_bro.importers.rate_limit.asyncio.sleep",
        new_callable=AsyncMock,
    ) as mock_sleep:
        await asyncio.gather(
            gate.acquire("tok-concurrent"),
            gate.acquire("tok-concurrent"),
        )
    assert mock_sleep.await_count == 1, f"Expected exactly one sleep, got {mock_sleep.await_count}"
    assert mock_sleep.await_args.args[0] >= 60


@pytest.mark.asyncio
async def test_different_tokens_independent(session_factory):
    from finance_bro.importers.rate_limit import RateLimitGate

    gate = RateLimitGate(session_factory)
    with patch(
        "finance_bro.importers.rate_limit.asyncio.sleep",
        new_callable=AsyncMock,
    ) as mock_sleep:
        await gate.acquire("token-A-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
        await gate.acquire("token-B-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb")
    assert mock_sleep.await_count == 0
