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
DDL_DIR = Path(__file__).parents[1] / "ddl"
COMPOSE = Path(__file__).parent / "docker-compose.fixture.yml"
TEST_DSN = os.environ.get("TEST_DSN", "postgresql://graph:graph@127.0.0.1:55432/graph_fixture")


def _compose(*args: str) -> None:
    subprocess.run(["docker", "compose", "-f", str(COMPOSE), *args], check=True)


def _psql(path: Path) -> None:
    subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE), "exec", "-T",
         "graph-fixture-db", "psql", "-U", "graph", "-d", "graph_fixture",
         "-v", "ON_ERROR_STOP=1"],
        stdin=path.open("rb"), check=True,
    )


@pytest.fixture(scope="session")
def fixture_db() -> str:
    """Start the fixture Postgres, load DDL + seed, yield the DSN.

    ddl/*.sql loads first -- it is the records DDL shared with the persistent
    clone (clone/docker-compose.clone.yml), so the two can never drift -- then
    fixtures/schema.sql (fixture-only indexes/schemas) and fixtures/seed.sql.
    """
    _compose("up", "-d", "--wait")
    try:
        # Lexicographic order == dependency order: the numeric prefix on each
        # ddl/ file must exceed every file it depends on. 001_records.sql
        # (tables) -> 002_indexes.sql (indexes on those tables) ->
        # 003_silver.sql -> 004_bench.sql (Tasks 2-4). The persistent clone
        # loads this same directory the same way, so the contract has to hold
        # there too, not just here.
        for sql in sorted(DDL_DIR.glob("*.sql")):
            _psql(sql)
        for name in ("schema.sql", "seed.sql"):
            sql = FIXTURE_DIR / name
            if sql.exists():
                _psql(sql)
        yield TEST_DSN
    finally:
        _compose("down", "-v")


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def fixture_pool(fixture_db: str):
    """Read-only, mirroring the adapter's own pool. Seed data is loaded via psql
    in fixture_db, not through this pool, so nothing legitimate needs to write.

    `server_settings`, not an `init=` callback running `SET` — for the same
    reason PartnerPool.create uses it, and this fixture had the identical bug:
    asyncpg's per-release `RESET ALL` wiped the session `SET`, so this
    SESSION-SCOPED pool was read-only for one acquire and writable for the rest
    of the suite. See src/occupancy_graph/source/pool.py's module docstring.
    Pinned by tests/test_pool.py::test_the_fixture_pool_is_read_only_on_every_acquire.
    """
    pool = await asyncpg.create_pool(
        fixture_db,
        min_size=1,
        max_size=4,
        server_settings={"default_transaction_read_only": "on"},
    )
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
