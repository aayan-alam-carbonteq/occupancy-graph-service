"""The address-index preflight, and the DSN redaction that runs beside it.

These guard the two ways this service can fail at startup in production: the
partner dropping an index we cannot recreate, and a credential reaching a log.
"""
from __future__ import annotations

import contextlib
import logging

import pytest

from occupancy_graph.source import preflight
from occupancy_graph.source.pool import redact_dsn


class _FakeConn:
    """Minimal asyncpg connection stand-in.

    A real fixture database would answer the catalog queries honestly, which is
    exactly what makes it useless here: the interesting cases are a MISSING and
    an INVALID index, and neither can be produced on demand -- Postgres offers
    no way to mark an index invalid, since that state only arises from an
    interrupted CREATE INDEX CONCURRENTLY.
    """

    def __init__(self, *, function_exists: bool, indexes: dict[str, bool],
                 defs: dict[str, str] | None = None):
        self._function_exists = function_exists
        self._indexes = indexes
        self._defs = defs or {}

    async def fetchval(self, sql: str, *args):
        # Both fetchval call sites return a scalar; collation is checked with
        # one too, and must not be mistaken for the function-existence probe.
        if "datcollate" in sql:
            return "C.UTF-8"
        return self._function_exists

    async def fetch(self, sql: str, names, relations=None):
        # Signature mirrors the real query's parameters ($1 names, $2 relations).
        # It is spelled out rather than *args so that a change to the production
        # query's arity fails here instead of silently passing a stale shape --
        # which is exactly how the earlier version of this fake went stale.
        return [
            {
                "index_name": name,
                "indisvalid": self._indexes[name],
                "indexdef": self._defs.get(
                    name,
                    f"CREATE INDEX {name} ON public.x USING btree "
                    f"(zip, silver.s5_street_norm(address) text_pattern_ops)",
                ),
            }
            for name in names
            if name in self._indexes
        ]


class _FakePool:
    def __init__(self, conn: _FakeConn):
        self._conn = conn

    @contextlib.asynccontextmanager
    async def acquire(self):
        yield self._conn


def _pool(*, function_exists: bool = True, missing: tuple[str, ...] = (),
          invalid: tuple[str, ...] = (),
          defs: dict[str, str] | None = None) -> _FakePool:
    indexes = {
        name: name not in invalid
        for name, _, _ in preflight.REQUIRED_INDEXES
        if name not in missing
    }
    return _FakePool(_FakeConn(function_exists=function_exists, indexes=indexes, defs=defs))


async def test_preflight_refuses_an_index_missing_its_pattern_opclass():
    """text_pattern_ops is what makes the prefix LIKE an index CONDITION. Without
    it the index still exists, still has the right name and expression, and still
    reduces the query to a scan -- the cutover's entire performance claim is
    void, silently."""
    with pytest.raises(preflight.MissingIndexError) as exc:
        await preflight.verify_address_indexes(_pool(defs={
            "idx_records_legacy_zip_normaddr":
                "CREATE INDEX idx_records_legacy_zip_normaddr ON public.records_legacy "
                "USING btree (zip, silver.s5_street_norm(address))",
        }))
    assert "text_pattern_ops" in str(exc.value)


async def test_preflight_passes_when_every_index_is_present_and_valid(caplog):
    with caplog.at_level(logging.INFO):
        await preflight.verify_address_indexes(_pool())
    assert "preflight passed" in caplog.text


async def test_preflight_refuses_when_an_index_is_missing():
    with pytest.raises(preflight.MissingIndexError) as exc:
        await preflight.verify_address_indexes(
            _pool(missing=("idx_records_legacy_zip_normaddr",))
        )
    message = str(exc.value)
    assert "idx_records_legacy_zip_normaddr" in message
    # The message must name the RELATION and the CONSEQUENCE, not just the
    # index: on-call needs to know which access path died without reading source.
    assert "public.records_legacy" in message
    assert "phase-1 address scan" in message


async def test_preflight_refuses_an_index_that_exists_but_is_invalid():
    """The silent case. An interrupted CREATE INDEX CONCURRENTLY leaves an
    index that pg_indexes reports and the planner ignores, so presence alone is
    not the check -- indisvalid is."""
    with pytest.raises(preflight.MissingIndexError) as exc:
        await preflight.verify_address_indexes(
            _pool(invalid=("idx_p20260301_property_owner_addr",))
        )
    assert "INVALID" in str(exc.value)


async def test_a_missing_function_is_reported_instead_of_three_index_errors():
    """s5_street_norm going away makes all three indexes impossible. Reporting
    the cause beats reporting three symptoms that share it."""
    with pytest.raises(preflight.MissingIndexError) as exc:
        await preflight.verify_address_indexes(_pool(function_exists=False, missing=(
            "idx_records_legacy_zip_normaddr",
            "idx_records_new_zip_normaddr",
            "idx_p20260301_property_owner_addr",
        )))
    message = str(exc.value)
    assert "s5_street_norm" in message
    assert "idx_records_legacy_zip_normaddr" not in message


# --- DSN redaction -----------------------------------------------------------


def test_redact_dsn_removes_the_password_and_keeps_the_target():
    got = redact_dsn("postgresql://carbonteq:hunter2@20.42.94.87:5432/all_data?sslmode=require")
    assert got == "postgresql://carbonteq:***@20.42.94.87:5432/all_data?sslmode=require"
    assert "hunter2" not in got


def test_redact_dsn_handles_a_password_containing_an_at_sign():
    """rpartition on '@', not partition: a password may legally contain one, and
    splitting on the FIRST '@' would print the tail of the password as the host
    and leave the head of it in the output."""
    got = redact_dsn("postgresql://user:p@ss@host:5432/db")
    assert got == "postgresql://user:***@host:5432/db"
    assert "p@ss" not in got


def test_redact_dsn_emits_nothing_it_cannot_parse_with_certainty():
    """If the password cannot be located, guessing risks printing it. Both of
    these must degrade to a placeholder rather than echo the input."""
    assert redact_dsn("not-a-dsn-at-all") == "<unparseable DSN>"
    assert redact_dsn("postgresql:///var/run/postgresql") == (
        "postgresql://<no credentials in DSN>"
    )


def test_redact_dsn_leaves_a_passwordless_dsn_intact():
    assert redact_dsn("postgresql://user@host:5432/db") == "postgresql://user@host:5432/db"


# --- the real SQL, against the real fixture catalog ---------------------------


async def test_preflight_runs_its_actual_queries_against_a_real_catalog(fixture_pool):
    """Every other test here fakes the connection, so `fetchval`/`fetch` never
    see the catalog statements: a typo in either would ship green while the
    production guard silently passed everything. The fixture carries all three
    indexes (ddl/005_address_indexes.sql) and s5_street_norm (ddl/003), so the
    happy path can be exercised for real."""
    await preflight.verify_address_indexes(fixture_pool)


REAL_LEGACY_INDEX = (
    "CREATE INDEX idx_records_legacy_zip_normaddr ON public.records_legacy "
    "USING btree (zip, silver.s5_street_norm(address) text_pattern_ops) "
    "WHERE zip IS NOT NULL"
)


async def test_preflight_rejects_a_correctly_named_index_on_the_wrong_expression(
    fixture_db, fixture_pool
):
    """The hole a name-only check leaves open. The partner owns these indexes and
    rebuilt their whole silver layer inside ten days; one recreated on the bare
    `address` column keeps the name and loses the property the access path rests
    on.

    The DDL runs on a SEPARATE connection, not through fixture_pool: that pool
    sets default_transaction_read_only, mirroring the adapter's own, and refuses
    DDL with ReadOnlySQLTransactionError. That refusal is the pool's guarantee
    working, so the test routes around it rather than weakening it.
    """
    import asyncpg

    ddl = await asyncpg.connect(fixture_db)
    try:
        await ddl.execute("DROP INDEX public.idx_records_legacy_zip_normaddr")
        await ddl.execute(
            "CREATE INDEX idx_records_legacy_zip_normaddr "
            "ON public.records_legacy USING btree (zip, address)"
        )
        with pytest.raises(preflight.MissingIndexError) as exc:
            await preflight.verify_address_indexes(fixture_pool)
        assert "s5_street_norm" in str(exc.value)
    finally:
        await ddl.execute("DROP INDEX IF EXISTS public.idx_records_legacy_zip_normaddr")
        await ddl.execute(REAL_LEGACY_INDEX)
        await ddl.close()
