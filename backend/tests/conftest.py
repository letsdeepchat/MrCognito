"""Shared fixtures: in-memory DB, controllable clock, real Stockfish when present."""
from __future__ import annotations

import os
import shutil
from datetime import datetime, timedelta, timezone

os.environ.setdefault("COGNITO_ANALYSIS_DEPTH", "8")
os.environ.setdefault("COGNITO_LLM_PROVIDER", "rule")
os.environ.setdefault("COGNITO_STOCKFISH_POOL_SIZE", "2")

import httpx  # noqa: E402
import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402

from app import clock, db  # noqa: E402
from app.api import routes  # noqa: E402
from app.engines import registry  # noqa: E402
from app.engines.material import MaterialEngine  # noqa: E402
from app.engines.stockfish import StockfishPool  # noqa: E402
from app.main import create_app  # noqa: E402


class FakeClock:
    def __init__(self) -> None:
        self.t = datetime(2026, 11, 1, 8, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.t

    def advance(self, **kw) -> None:
        self.t += timedelta(**kw)


def _engine():
    path = registry._resolve_stockfish("stockfish")
    if path and not os.environ.get("COGNITO_FORCE_MATERIAL_ENGINE"):
        return StockfishPool(path, size=2, default_depth=8)
    return MaterialEngine(depth=2)


@pytest_asyncio.fixture
async def engine():
    e = _engine()
    registry.set_engines(e, None)
    yield e
    await e.close()
    registry.set_engines(None, None)


@pytest.fixture
def fake_clock():
    fc = FakeClock()
    clock.set_clock(fc)
    yield fc
    clock.reset_clock()


@pytest_asyncio.fixture
async def client(engine, fake_clock):
    db.init_engine("sqlite+aiosqlite:///:memory:")
    await db.create_all()
    routes.set_coach(None)
    app = create_app(use_lifespan=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=120) as c:
        yield c
    await db.dispose()


async def new_user(client, budget: int = 10, ack: bool = True) -> dict:
    tok = (await client.post("/api/users", json={"display_name": "t"})).json()["token"]
    h = {"Authorization": f"Bearer {tok}"}
    if budget != 20:
        r = await client.put("/api/me/settings", json={"daily_budget_minutes": budget}, headers=h)
        assert r.status_code == 200, r.text
    if ack:
        await client.post("/api/me/wellness-ack", headers=h)
    return h


async def start(client, h) -> dict:
    r = await client.post("/api/sessions", headers=h)
    assert r.status_code == 200, r.text
    return r.json()


async def act(client, h, sid, action, expect=200):
    r = await client.post(f"/api/sessions/{sid}/action", headers=h, json={"action": action})
    assert r.status_code == expect, r.text
    return r.json()


def has_stockfish() -> bool:
    return shutil.which("stockfish") is not None or os.path.exists("/usr/games/stockfish")
