"""The check that would have caught `records_partitioned`.

A fixture that models a topology production does not have cannot fail on the
difference -- that is how five of seven shapes came to name a nonexistent
relation while 548 tests passed. This asserts our DDL against a recorded
catalog of the real thing.
"""
from __future__ import annotations

import json
from pathlib import Path

CATALOG = Path(__file__).parents[2] / "clone" / "profiles" / "records_catalog.json"


async def test_records_legacy_matches_the_production_catalog(fixture_pool):
    expected = [(name, typ) for name, typ in json.loads(CATALOG.read_text())]
    async with fixture_pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT a.attname, format_type(a.atttypid, a.atttypmod) AS typ
            FROM pg_attribute a WHERE a.attrelid='public.records_legacy'::regclass
              AND a.attnum>0 AND NOT a.attisdropped ORDER BY a.attnum""")
    actual = [(r["attname"], r["typ"]) for r in rows]
    assert len(actual) == 144
    assert actual == expected


async def test_records_new_has_all_five_production_partitions(fixture_pool):
    async with fixture_pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT c.relname, pg_get_expr(c.relpartbound, c.oid) AS bound
            FROM pg_class c JOIN pg_inherits i ON i.inhrelid=c.oid
            WHERE i.inhparent='public.records_new'::regclass ORDER BY c.relname""")
    got = {r["relname"]: r["bound"] for r in rows}
    assert set(got) == {
        "records_partitioned_p20251201", "records_partitioned_p20260101",
        "records_partitioned_p20260201", "records_partitioned_p20260301",
        "records_partitioned_default",
    }
    assert "2025-12-01" in got["records_partitioned_p20251201"]
    assert "2026-01-01" in got["records_partitioned_p20260101"]
    assert got["records_partitioned_default"] == "DEFAULT"


# Every index production carries on records_legacy. Access-path behaviour is
# the whole point of the clone: a missing index silently changes the plan the
# experiments are meant to observe.
#
# `idx_records_legacy_zip_normaddr` added 2026-08-11, when the partner built it
# at our ask. Verified against the live catalog that day: present, indisvalid,
# 61 GB. It is the index the entire address-first access path depends on -- see
# ddl/005_address_indexes.sql and source/preflight.py, which refuses to serve
# without it.
EXPECTED_LEGACY_INDEXES = {
    "records_pkey", "idx_records_zip", "idx_records_lastname_zip_house",
    "idx_records_legacy_zip_house", "idx_records_legacy_state_city",
    "idx_records_first_last", "idx_records_last_name_trgm", "idx_records_dob",
    "idx_records_email", "idx_records_email2", "idx_records_mobile",
    "idx_records_phone", "idx_records_ssn", "idx_records_ssn2",
    "idx_records_legacy_zip_normaddr",
}


async def test_records_legacy_carries_the_production_index_set(fixture_pool):
    async with fixture_pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT indexname FROM pg_indexes
            WHERE schemaname='public' AND tablename='records_legacy'""")
    assert {r["indexname"] for r in rows} == EXPECTED_LEGACY_INDEXES


async def test_the_address_indexes_are_expression_indexes_on_s5_street_norm(fixture_pool):
    """The clone must reproduce the EXPRESSION, not merely an index of the same
    name. resolve.py's predicate is written to match `s5_street_norm(address)`
    exactly; an index on the bare column would be silently unusable by it while
    every name-based assertion above still passed."""
    async with fixture_pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT indexname, indexdef FROM pg_indexes
            WHERE schemaname='public' AND indexdef LIKE '%s5_street_norm%'""")
    defs = {r["indexname"]: r["indexdef"] for r in rows}
    assert "idx_records_legacy_zip_normaddr" in defs
    assert "idx_p20260301_property_owner_addr" in defs
    # text_pattern_ops is what makes a prefix LIKE an index CONDITION rather
    # than a filter; without it the cutover's whole performance claim is void.
    assert "text_pattern_ops" in defs["idx_records_legacy_zip_normaddr"]
    # The assessor index is PARTIAL, and the predicate must match the literal
    # feeds.py emits or the planner cannot prove the index applies.
    assert "property_owner%" in defs["idx_p20260301_property_owner_addr"]


async def test_every_partition_carries_a_record_id_index(fixture_pool):
    """record_id IS indexed on every relation in production. The cost there is
    heap I/O, not the index -- see source/search.py::rows_for_links."""
    async with fixture_pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT tablename FROM pg_indexes
            WHERE schemaname='public' AND tablename LIKE 'records_partitioned_%'
              AND indexdef ~ '\\(record_id'""")
    assert len({r["tablename"] for r in rows}) == 5


async def test_the_fixture_collation_matches_production(fixture_pool):
    """C.UTF-8, because a range scan's meaning IS its collation.

    search.py resolves a name to hal_ids with `key_value >= 'LAST|FIRST|' AND
    key_value < 'LAST|FIRST}'`, which equals a prefix match only under bytewise
    comparison. This fixture defaulted to the postgres image's en_US.utf8, where
    linguistic rules reorder punctuation and the upper bound stops bounding:
    `'DOE|JANE|1980-04-01' < 'DOE|JANE}'` is TRUE in production and FALSE there.
    Name search returned zero rows locally for data that resolves live -- no
    error, just silence.

    Pinned here rather than left to the compose file because the failure mode is
    invisible: nothing else in the suite compares strings across a separator, so
    a drifted collation would surface as one confusing test, not as this.
    """
    async with fixture_pool.acquire() as conn:
        collate = await conn.fetchval(
            "SELECT datcollate FROM pg_database WHERE datname = current_database()"
        )
        prefix_range_holds = await conn.fetchval(
            "SELECT 'DOE|JANE|1980-04-01' < 'DOE|JANE}'"
        )
    assert collate == "C.UTF-8", (
        f"fixture collation is {collate!r}, production is 'C.UTF-8'. Recreate the "
        f"fixture volume: docker compose -f tests/docker-compose.fixture.yml down -v"
    )
    assert prefix_range_holds, "the separator range bound does not hold under this collation"
