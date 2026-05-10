import pytest


@pytest.mark.asyncio
async def test_health_db_ok(client):
    r = await client.get("/api/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "db": "ok"}


@pytest.mark.asyncio
async def test_health_no_auth(client):
    r = await client.get("/api/health")
    assert r.status_code == 200
    # no Authorization required
