from __future__ import annotations

import os
import subprocess
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio

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
