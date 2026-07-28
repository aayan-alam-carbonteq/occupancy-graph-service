"""The fixture database must reproduce the partner's structure closely enough
that query plans in tests match production. Missing indexes would let tests pass
on plans that do not exist against the real corpus."""
from __future__ import annotations


async def test_fixture_has_partition_structure(fixture_pool):
    async with fixture_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"
        )
    names = {row["tablename"] for row in rows}
    assert "records_legacy" in names
    assert "records_partitioned_p20260201" in names
    assert "records_partitioned_p20260301" in names


async def test_fixture_has_the_real_index_set(fixture_pool):
    async with fixture_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT indexdef FROM pg_indexes WHERE schemaname = 'public'"
        )
    defs = " ".join(row["indexdef"] for row in rows)
    assert "(zip)" in defs
    assert "(last_name, zip, house_number)" in defs
    assert "(upper(state), upper(city))" in defs


async def test_fixture_has_silver_entity_tables(fixture_pool):
    async with fixture_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'silver'"
        )
    names = {row["tablename"] for row in rows}
    assert {"entity_master", "entity_links"} <= names
