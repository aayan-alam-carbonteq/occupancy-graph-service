"""PartnerPool: read-only enforcement, statement timeout, and env wiring.

We are a read-only guest on someone else's production database, so these tests
go beyond the happy path in the plan: every write statement type must be
rejected (not just CREATE), a timed-out statement must not poison the
connection for the next query, and the read-only/timeout settings must land on
every pooled connection, not just the first one created.
"""
from __future__ import annotations

import asyncio

import asyncpg
import pytest
import pytest_asyncio

from occupancy_graph.source.pool import PartnerPool

SCRATCH_TABLE = "pool_test_scratch"
SCRATCH_TABLE_EXTRA = "pool_test_scratch_extra"


@pytest_asyncio.fixture(loop_scope="session")
async def scratch_table(fixture_db: str):
    """A throwaway table via a direct (non-pool) connection, so the write-
    statement tests below have a real target without touching the shared
    fixture schema other test modules depend on."""
    conn = await asyncpg.connect(fixture_db)
    try:
        await conn.execute(f"DROP TABLE IF EXISTS {SCRATCH_TABLE_EXTRA}")
        await conn.execute(f"DROP TABLE IF EXISTS {SCRATCH_TABLE}")
        await conn.execute(f"CREATE TABLE {SCRATCH_TABLE} (id int PRIMARY KEY, val text)")
        await conn.execute(f"INSERT INTO {SCRATCH_TABLE} (id, val) VALUES (1, 'x')")
        yield SCRATCH_TABLE
    finally:
        await conn.execute(f"DROP TABLE IF EXISTS {SCRATCH_TABLE_EXTRA}")
        await conn.execute(f"DROP TABLE IF EXISTS {SCRATCH_TABLE}")
        await conn.close()


# --- Base tests from the plan ---


async def test_sessions_are_read_only(fixture_db):
    pool = await PartnerPool.create(fixture_db, statement_timeout_ms=5000)
    try:
        async with pool.acquire() as conn:
            with pytest.raises(asyncpg.PostgresError):
                await conn.execute("CREATE TABLE should_not_exist (x int)")
    finally:
        await pool.close()


async def test_statement_timeout_is_applied(fixture_db):
    pool = await PartnerPool.create(fixture_db, statement_timeout_ms=250)
    try:
        async with pool.acquire() as conn:
            with pytest.raises(asyncpg.QueryCanceledError):
                await conn.fetch("SELECT pg_sleep(2)")
    finally:
        await pool.close()


async def test_reads_still_work(fixture_db):
    pool = await PartnerPool.create(fixture_db, statement_timeout_ms=5000)
    try:
        async with pool.acquire() as conn:
            value = await conn.fetchval("SELECT 1")
    finally:
        await pool.close()
    assert value == 1


# --- Extra verification 1: read-only enforcement for every statement type ---


async def test_select_works_against_a_real_table(fixture_db, scratch_table):
    pool = await PartnerPool.create(fixture_db, statement_timeout_ms=5000)
    try:
        async with pool.acquire() as conn:
            value = await conn.fetchval(f"SELECT val FROM {scratch_table} WHERE id = 1")
    finally:
        await pool.close()
    assert value == "x"


@pytest.mark.parametrize(
    "sql",
    [
        pytest.param(f"INSERT INTO {SCRATCH_TABLE} (id, val) VALUES (2, 'y')", id="insert"),
        pytest.param(f"UPDATE {SCRATCH_TABLE} SET val = 'z' WHERE id = 1", id="update"),
        pytest.param(f"DELETE FROM {SCRATCH_TABLE} WHERE id = 1", id="delete"),
        pytest.param(f"TRUNCATE {SCRATCH_TABLE}", id="truncate"),
        pytest.param(f"CREATE TABLE {SCRATCH_TABLE_EXTRA} (x int)", id="create_table"),
        pytest.param(f"DROP TABLE {SCRATCH_TABLE}", id="drop_table"),
    ],
)
async def test_write_statements_are_rejected(fixture_db, scratch_table, sql):
    pool = await PartnerPool.create(fixture_db, statement_timeout_ms=5000)
    try:
        async with pool.acquire() as conn:
            with pytest.raises(asyncpg.PostgresError):
                await conn.execute(sql)
    finally:
        await pool.close()


# --- Extra verification 2: timeout applies, and the pool survives it ---


async def test_pool_survives_a_timed_out_statement(fixture_db):
    """A cancelled statement must not poison the connection: the very next
    query on the same acquired connection has to succeed."""
    pool = await PartnerPool.create(fixture_db, statement_timeout_ms=250)
    try:
        async with pool.acquire() as conn:
            with pytest.raises(asyncpg.QueryCanceledError):
                await conn.fetch("SELECT pg_sleep(2)")
            value = await conn.fetchval("SELECT 1")
    finally:
        await pool.close()
    assert value == 1


# --- Extra verification 3: settings are per-connection, not per-first-connection ---


async def test_settings_apply_to_every_pooled_connection(fixture_db):
    pool = await PartnerPool.create(
        fixture_db, statement_timeout_ms=1234, min_size=1, max_size=4
    )

    async def _check() -> tuple[str, str]:
        async with pool.acquire() as conn:
            read_only = await conn.fetchval("SHOW default_transaction_read_only")
            timeout = await conn.fetchval("SHOW statement_timeout")
            # Hold the connection briefly so 4 concurrent acquires force the
            # pool to actually open 4 distinct connections, rather than one
            # connection being reused sequentially before the others ask.
            await asyncio.sleep(0.05)
            return read_only, timeout

    try:
        results = await asyncio.gather(*(_check() for _ in range(4)))
    finally:
        await pool.close()

    assert len(results) == 4
    for read_only, timeout in results:
        assert read_only == "on"
        assert timeout == "1234ms"


# --- Extra verification 4: from_env ---


async def test_from_env_raises_when_partner_dsn_unset(monkeypatch):
    monkeypatch.delenv("PARTNER_DSN", raising=False)
    with pytest.raises(RuntimeError, match="PARTNER_DSN"):
        await PartnerPool.from_env()


async def test_from_env_reads_overrides_from_environment(fixture_db, monkeypatch):
    monkeypatch.setenv("PARTNER_DSN", fixture_db)
    monkeypatch.setenv("PARTNER_STATEMENT_TIMEOUT_MS", "1234")
    monkeypatch.setenv("PARTNER_POOL_MIN", "1")
    monkeypatch.setenv("PARTNER_POOL_MAX", "3")

    pool = await PartnerPool.from_env()
    try:
        async with pool.acquire() as conn:
            timeout = await conn.fetchval(
                "SELECT setting FROM pg_settings WHERE name = 'statement_timeout'"
            )
        assert timeout == "1234"
        assert pool.pool.get_min_size() == 1
        assert pool.pool.get_max_size() == 3
    finally:
        await pool.close()
