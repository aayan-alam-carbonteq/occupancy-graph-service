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


async def test_seed_covers_every_shape_and_known_defect(fixture_pool):
    async with fixture_pool.acquire() as conn:
        legacy = await conn.fetch(
            "SELECT DISTINCT split_part(source_file, '/', 1) AS feed FROM public.records_legacy"
        )
        new = await conn.fetch(
            "SELECT DISTINCT split_part(source_file, '/', 1) AS feed FROM public.records_partitioned"
        )
        shifted = await conn.fetchval(
            "SELECT count(*) FROM public.records_partitioned "
            "WHERE raw_data->>'streetNumber' IN ('True', 'False')"
        )
        variants = await conn.fetchval(
            "SELECT count(DISTINCT own_rent) FROM public.records_partitioned "
            "WHERE own_rent IS NOT NULL"
        )
    feeds = {r["feed"] for r in legacy} | {r["feed"] for r in new}
    assert "Export Utility Stripped Down" in feeds
    assert "Trace Skipping Oct 2025" in feeds
    assert "2026.1-USCRM" in feeds
    assert any(f.startswith("Payday_Big") for f in feeds)
    assert any(f.startswith("AvengerAuto") for f in feeds)
    assert any(f.startswith("property_owner") for f in feeds)
    assert shifted >= 1, "need a column-shifted property_owner row"
    assert variants >= 7, "need every observed own_rent casing"
