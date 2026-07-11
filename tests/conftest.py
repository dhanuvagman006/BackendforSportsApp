"""Test fixtures.

Environment is pinned to the throwaway `sportyqo_test` database BEFORE any
app import so the engine binds to it. Schema is created once per run;
tables are truncated between tests.
"""
import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://sportyqo:sportyqo@localhost:5432/sportyqo_test")
os.environ.setdefault("STORAGE_DIR", "/tmp/sportyqo_test_storage")
os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("ADMIN_API_KEY", "test-admin-key")
os.environ.setdefault("ENVIRONMENT", "test")

import httpx  # noqa: E402
import pytest  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.db.base import Base, SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.services.scoring import seed_tiers  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
async def _schema():
    async with engine.begin() as conn:
        await conn.execute(text("DROP SCHEMA public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS citext"))
        await conn.run_sync(Base.metadata.create_all)
    yield
    await engine.dispose()


@pytest.fixture(autouse=True)
async def _clean_db(_schema):
    tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    async with SessionLocal() as db:
        await seed_tiers(db)
        await db.commit()
    yield


@pytest.fixture
async def client():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# --------------------------------------------------------------------------- helpers
async def register(client: httpx.AsyncClient, role: str = "player", email: str | None = None,
                   name: str = "Aarav Mehta") -> dict:
    email = email or f"{role}_{os.urandom(4).hex()}@example.com"
    r = await client.post("/v1/auth/register", json={
        "full_name": name, "email": email, "password": "Passw0rd1", "role": role,
    })
    assert r.status_code == 201, r.text
    data = r.json()
    data["email"] = email
    data["headers"] = {"Authorization": f"Bearer {data['access_token']}"}
    return data


async def onboard_player(client: httpx.AsyncClient, name: str = "Aarav Mehta",
                         dob: str = "2010-05-16") -> dict:
    acct = await register(client, "player", name=name)
    r = await client.patch("/v1/users/me", data={"dob": dob, "role_position": "Batter",
                                                 "location": "Mumbai, India"},
                           headers=acct["headers"])
    assert r.status_code == 200, r.text
    r = await client.post("/v1/users/me/sport", json={"sport": "Cricket", "sub_role": "Batsman"},
                          headers=acct["headers"])
    assert r.status_code == 201, r.text
    acct["player_id"] = r.json()["player_id"]
    return acct


async def onboard_coach(client: httpx.AsyncClient, name: str = "Coach Suneeth") -> dict:
    acct = await register(client, "coach", name=name)
    r = await client.patch("/v1/coaches/me", data={"role_title": "Head Coach",
                                                   "academy": "Falcons Cricket Academy",
                                                   "experience_years": "6", "sport": "Cricket"},
                           headers=acct["headers"])
    assert r.status_code == 200, r.text
    return acct


async def create_league(client: httpx.AsyncClient, coach: dict, teams: list[str] | None = None) -> dict:
    teams = teams or ["Falcons FC", "Thunder Strikers"]
    r = await client.post("/v1/leagues", data={
        "name": "Falcons U16 Premier League",
        "cricket_type": "Professional Cricket",
        "location": "Bangalore, Karnataka",
        "gender": "Men's",
        "teams_count": str(len(teams)),
        "team_names": ",".join(teams),
    }, headers=coach["headers"])
    assert r.status_code == 201, r.text
    return r.json()
