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
EXPECTED_LEGACY_INDEXES = {
    "records_pkey", "idx_records_zip", "idx_records_lastname_zip_house",
    "idx_records_legacy_zip_house", "idx_records_legacy_state_city",
    "idx_records_first_last", "idx_records_last_name_trgm", "idx_records_dob",
    "idx_records_email", "idx_records_email2", "idx_records_mobile",
    "idx_records_phone", "idx_records_ssn", "idx_records_ssn2",
}


async def test_records_legacy_carries_the_production_index_set(fixture_pool):
    async with fixture_pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT indexname FROM pg_indexes
            WHERE schemaname='public' AND tablename='records_legacy'""")
    assert {r["indexname"] for r in rows} == EXPECTED_LEGACY_INDEXES


async def test_every_partition_carries_a_record_id_index(fixture_pool):
    """record_id IS indexed on every relation in production. The cost there is
    heap I/O, not the index -- see source/search.py::rows_for_links."""
    async with fixture_pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT tablename FROM pg_indexes
            WHERE schemaname='public' AND tablename LIKE 'records_partitioned_%'
              AND indexdef ~ '\\(record_id'""")
    assert len({r["tablename"] for r in rows}) == 5
