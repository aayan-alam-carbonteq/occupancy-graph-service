"""PartnerPool: read-only enforcement, statement timeout, and env wiring.

We are a read-only guest on someone else's production database, so these tests
go beyond the happy path in the plan: every write statement type must be
rejected (not just CREATE), a timed-out statement must not poison the
connection for the next query, and the read-only/timeout settings must land on
every pooled connection, not just the first one created.
"""
from __future__ import annotations

import asyncio
import logging

import asyncpg
import pytest
import pytest_asyncio

from occupancy_graph.source import pool as pool_module
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


# --- Review fix 1: reject non-positive statement_timeout_ms (0 == UNLIMITED) ---


async def test_create_rejects_zero_statement_timeout(fixture_db):
    with pytest.raises(ValueError, match="UNLIMITED"):
        await PartnerPool.create(fixture_db, statement_timeout_ms=0)


async def test_create_rejects_negative_statement_timeout(fixture_db):
    with pytest.raises(ValueError, match="UNLIMITED"):
        await PartnerPool.create(fixture_db, statement_timeout_ms=-1)


async def test_from_env_rejects_zero_statement_timeout_before_connecting(monkeypatch):
    # DSN is deliberately meaningless: if the validation didn't run before
    # asyncpg.create_pool, this would fail on connection instead, masking the
    # real bug. Patching create_pool to blow up makes that failure mode loud.
    monkeypatch.setenv("PARTNER_DSN", "postgresql://irrelevant/irrelevant")
    monkeypatch.setenv("PARTNER_STATEMENT_TIMEOUT_MS", "0")

    async def _must_not_be_called(*args, **kwargs):
        raise AssertionError("asyncpg.create_pool must not be called for statement_timeout_ms=0")

    monkeypatch.setattr(pool_module.asyncpg, "create_pool", _must_not_be_called)

    with pytest.raises(ValueError, match="UNLIMITED"):
        await PartnerPool.from_env()


async def test_from_env_rejects_malformed_pool_max(fixture_db, monkeypatch):
    monkeypatch.setenv("PARTNER_DSN", fixture_db)
    monkeypatch.setenv("PARTNER_POOL_MAX", "abc")

    with pytest.raises(ValueError, match="PARTNER_POOL_MAX"):
        await PartnerPool.from_env()


# --- Review fix 2: pin the scope of the read-only guarantee ---


async def test_read_only_is_a_session_default_not_a_boundary(fixture_db, scratch_table):
    """Known limitation, pinned so it can't silently regress into a false
    sense of safety. `default_transaction_read_only` is a session DEFAULT,
    not an enforced boundary: raw `BEGIN READ WRITE` overrides it and the
    write succeeds. If this ever stops being true, we want to notice --
    idiomatic asyncpg usage (e.g. conn.transaction(readonly=False)) does NOT
    defeat the default, only raw SQL text like this does."""
    pool = await PartnerPool.create(fixture_db, statement_timeout_ms=5000)
    try:
        async with pool.acquire() as conn:
            await conn.execute("BEGIN READ WRITE")
            try:
                await conn.execute(
                    f"INSERT INTO {scratch_table} (id, val) VALUES (99, 'defeats-default')"
                )
            except Exception:
                await conn.execute("ROLLBACK")
                raise
            await conn.execute("COMMIT")
            value = await conn.fetchval(f"SELECT val FROM {scratch_table} WHERE id = 99")
    finally:
        await pool.close()
    assert value == "defeats-default"


# --- Review fix 3: close() must be bounded -----------------------------------
#
# asyncpg.Pool.close() waits indefinitely for connections to release and has no
# timeout parameter. A connection left mid-cancellation by a statement timeout
# never releases, so the bare `await self.pool.close()` this replaced could stall
# forever: a test session that produces NO output at all (observed twice, ~1 run
# in 10, 10 and 15 minutes before being killed), and in production a container
# that never exits and has to be SIGKILLed by its orchestrator.
#
# The fixture below is a pool whose close() genuinely never completes, which is
# the only way to exercise the fallback deterministically -- forcing a real
# asyncpg connection to wedge is exactly the race we cannot reproduce on demand.


class _WedgedPool:
    """An asyncpg.Pool stand-in whose close() never returns.

    Deliberately NOT a Mock: the point is a close() that really never completes,
    so that deleting the timeout in PartnerPool.close makes the test hang or
    fail rather than pass.
    """

    def __init__(self) -> None:
        self.close_entered = False
        self.close_cancelled = False
        self.terminate_calls = 0

    async def close(self) -> None:
        self.close_entered = True
        try:
            # Never set by anyone. Not asyncio.sleep(large): a sleep would
            # eventually return and could mask a too-generous timeout.
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.close_cancelled = True
            raise

    def terminate(self) -> None:
        self.terminate_calls += 1


class _DrainingPool:
    """The healthy counterpart: close() completes promptly."""

    def __init__(self) -> None:
        self.close_calls = 0
        self.terminate_calls = 0

    async def close(self) -> None:
        self.close_calls += 1

    def terminate(self) -> None:
        self.terminate_calls += 1


async def test_close_falls_back_to_terminate_when_the_drain_never_finishes(caplog):
    wedged = _WedgedPool()
    partner = PartnerPool(pool=wedged, close_timeout_seconds=0.05)

    with caplog.at_level(logging.WARNING, logger="occupancy_graph.source.pool"):
        # The outer bound is the mutation detector: with the timeout removed
        # from PartnerPool.close this raises TimeoutError in 5s instead of
        # hanging out the whole session, so the regression fails loudly.
        await asyncio.wait_for(partner.close(), timeout=5)

    assert wedged.close_entered is True, "the graceful drain must be attempted first"
    assert wedged.close_cancelled is True, "the stalled drain must be cancelled, not left running"
    assert wedged.terminate_calls >= 1, "terminate() is the escape hatch and must be called"
    assert any(
        record.levelname == "WARNING" and "terminating" in record.getMessage()
        for record in caplog.records
    ), "a silent terminate() teaches us nothing; the fallback must be logged"


async def test_close_does_not_terminate_when_the_drain_succeeds(caplog):
    """The fallback is a fallback. A healthy pool must close gracefully and
    log nothing -- otherwise every clean shutdown cries wolf."""
    healthy = _DrainingPool()
    partner = PartnerPool(pool=healthy, close_timeout_seconds=0.05)

    with caplog.at_level(logging.WARNING, logger="occupancy_graph.source.pool"):
        await partner.close()

    assert healthy.close_calls == 1
    assert healthy.terminate_calls == 0
    assert caplog.records == []


def test_the_close_window_is_derived_from_the_statement_timeout():
    """Long enough that an in-flight query (bounded by statement_timeout) can
    finish, capped so a large statement timeout cannot push shutdown past the
    orchestrator's grace period."""
    assert pool_module.close_timeout_for(20_000) == 25.0
    assert pool_module.close_timeout_for(1_000) == 6.0
    # Raising the query budget must widen the window...
    assert pool_module.close_timeout_for(10_000) > pool_module.close_timeout_for(5_000)
    # ...but never past the cap.
    assert pool_module.close_timeout_for(600_000) == pool_module.MAX_CLOSE_TIMEOUT_SECONDS


async def test_a_real_pool_carries_the_derived_close_window(fixture_db):
    pool = await PartnerPool.create(fixture_db, statement_timeout_ms=1_000)
    try:
        assert pool.close_timeout_seconds == pool_module.close_timeout_for(1_000)
    finally:
        await pool.close()
