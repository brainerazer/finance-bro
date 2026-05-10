import pytest


def test_no_auth_middleware():
    from finance_bro.main import app

    assert app.user_middleware == [], (
        f"Phase 1 must have zero auth middleware (DEP-02). Found: {app.user_middleware}"
    )


@pytest.mark.asyncio
async def test_docs_open(client):
    """Phase 2 (02-03): the lifespan now opens DB connections at startup
    (`recover_in_flight` + `read_state` in `src/finance_bro/main.py`), so a
    bogus DATABASE_URL no longer works for lifespan-only smoke tests. Use the
    conftest `client` fixture which wires the testcontainers Postgres before
    the lifespan fires."""
    r = await client.get("/docs")
    assert r.status_code == 200
    assert "Swagger" in r.text or "swagger" in r.text
