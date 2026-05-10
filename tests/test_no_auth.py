import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("MONO_TOKEN", "stub-token-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://x:y@localhost:5432/x")
    from finance_bro.core import settings as s

    s.get_settings.cache_clear()


def test_no_auth_middleware():
    from finance_bro.main import app

    assert app.user_middleware == [], (
        f"Phase 1 must have zero auth middleware (DEP-02). Found: {app.user_middleware}"
    )


@pytest.mark.asyncio
async def test_docs_open():
    from finance_bro.main import app

    async with (
        LifespanManager(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac,
    ):
        r = await ac.get("/docs")
        assert r.status_code == 200
        assert "Swagger" in r.text or "swagger" in r.text
