from __future__ import annotations

import os
import subprocess
from pathlib import Path

import asyncpg
import httpx
import pytest
import pytest_asyncio

from occupancy_graph.source.bundle import BundleCache
from occupancy_graph.source.pool import PartnerPool

FIXTURE_DIR = Path(__file__).parent / "fixtures"
COMPOSE = Path(__file__).parent / "docker-compose.fixture.yml"
TEST_DSN = os.environ.get("TEST_DSN", "postgresql://graph:graph@127.0.0.1:55432/graph_fixture")


def _compose(*args: str) -> None:
    subprocess.run(["docker", "compose", "-f", str(COMPOSE), *args], check=True)


@pytest.fixture(scope="session")
def fixture_db() -> str:
    """Start the fixture Postgres, load DDL + seed, yield the DSN."""
    _compose("up", "-d", "--wait")
    try:
        for path in ("schema.sql", "seed.sql"):
            sql = FIXTURE_DIR / path
            if sql.exists():
                subprocess.run(
                    ["docker", "compose", "-f", str(COMPOSE), "exec", "-T",
                     "graph-fixture-db", "psql", "-U", "graph", "-d", "graph_fixture",
                     "-v", "ON_ERROR_STOP=1"],
                    stdin=sql.open("rb"), check=True,
                )
        yield TEST_DSN
    finally:
        _compose("down", "-v")


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def fixture_pool(fixture_db: str):
    """Read-only, mirroring the adapter's own pool. Seed data is loaded via psql
    in fixture_db, not through this pool, so nothing legitimate needs to write."""
    async def _setup(conn):
        await conn.execute("SET default_transaction_read_only = on")

    pool = await asyncpg.create_pool(fixture_db, min_size=1, max_size=4, init=_setup)
    yield pool
    await pool.close()


@pytest_asyncio.fixture(loop_scope="session")
async def service_pool(fixture_db: str):
    """A PartnerPool over the fixture, shaped exactly like the production one."""
    pool = await PartnerPool.create(fixture_db, statement_timeout_ms=10_000)
    yield pool
    await pool.close()


@pytest_asyncio.fixture(loop_scope="session")
async def service_cache(service_pool) -> BundleCache:
    """The BundleCache the `client` app is built on.

    Its own fixture rather than an inline argument to create_app, so a test that
    needs to drive the cache's tiers (evict_hot, and the hot-miss/cold-hit
    re-materialization behind it) can request it directly instead of reaching
    through httpx's private transport for the object the app was handed."""
    return BundleCache(service_pool)


@pytest_asyncio.fixture(loop_scope="session")
async def client(service_pool, service_cache):
    """The ASGI app driven in-process. httpx.ASGITransport does NOT run the
    lifespan, which is exactly what we want: the pool and cache are injected,
    so no test needs PARTNER_DSN."""
    from occupancy_graph.service.app import create_app

    app = create_app(pool=service_pool, cache=service_cache)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://graph.test") as http:
        yield http
