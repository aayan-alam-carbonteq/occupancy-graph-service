from __future__ import annotations

import pytest

from occupancy_graph.source.bundle import BundleCache, materialize
from occupancy_graph.source.pool import PartnerPool


@pytest.fixture
async def pool(fixture_db):
    pool = await PartnerPool.create(fixture_db, statement_timeout_ms=10_000)
    yield pool
    await pool.close()


async def test_materialize_projects_every_shape(pool):
    bundle = await materialize(pool, "123 Main St", "40505", address_id=1)
    assert bundle.source_counts["utility"] == 1
    assert bundle.source_counts["trace"] == 2
    assert bundle.source_counts["tax"] == 2      # 4001 + 4002; 4003 dropped
    assert bundle.dropped_counts["tax"] == 1


async def test_materialized_rows_use_contract_keys(pool):
    bundle = await materialize(pool, "123 Main St", "40505", address_id=1)
    tax = bundle.rows_by_shape["tax"][0]
    assert tax["ownername"] == "DOE, JANE ANN"
    assert tax["ownercity"] == "AURORA"     # absentee owner, another state
    assert tax["zip"] == "40505"
    utility = bundle.rows_by_shape["utility"][0]
    assert utility["first_name"] == "Pat"   # NOT firstName; raw column name


async def test_bundle_carries_the_resolved_address(pool):
    bundle = await materialize(pool, "123 Main St", "40505", address_id=1)
    assert bundle.norm_address == "123 MAIN ST"
    assert bundle.zip5 == "40505"
    assert bundle.city == "LEXINGTON"
    assert bundle.state == "KY"
    assert bundle.street_number == "123"


async def test_cache_returns_the_same_bundle_without_rescanning(pool):
    cache = BundleCache(pool)
    first = await cache.resolve("123 Main St", "40505")
    second = await cache.get(first.address_id)
    assert second is first


async def test_cache_rematerializes_after_the_hot_entry_is_evicted(pool):
    cache = BundleCache(pool)
    first = await cache.resolve("123 Main St", "40505")
    cache.evict_hot(first.address_id)
    again = await cache.get(first.address_id)
    assert again is not None
    assert again.address_id == first.address_id
    assert again.norm_address == first.norm_address


async def test_cache_returns_none_for_an_id_it_never_minted(pool):
    cache = BundleCache(pool)
    assert await cache.get(987654) is None


async def test_resolving_the_same_address_twice_reuses_the_id(pool):
    cache = BundleCache(pool)
    first = await cache.resolve("123 Main St", "40505")
    second = await cache.resolve("123 MAIN STREET", "40505")
    assert second.address_id == first.address_id
