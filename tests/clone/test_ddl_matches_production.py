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
