from __future__ import annotations

import asyncio

import pytest

from occupancy_graph.source import bundle as bundle_module
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


def _utility_row(record_id: int, first_name: str) -> dict:
    return {
        "record_id": record_id, "first_name": first_name, "last_name": "Tenant",
        "address": "123 MAIN ST", "city": "LEXINGTON", "county": "FAYETTE",
        "state": "KY", "zip": "40505",
    }


def _tax_row(record_id: int | None, owner: str) -> dict:
    return {
        "record_id": record_id, "address": "123 MAIN ST", "city": "LEXINGTON",
        "state": "KY", "raw_data": {"ownerName": owner},
    }


def _patch_scans(monkeypatch, utility_rows: list[dict], tax_rows: list[dict]) -> None:
    """Drive materialize with scan results handed back in a chosen order.

    The real scans have no ORDER BY, so on the fixture they happen to emit heap
    order, which already equals record_id order -- a test against the real scan
    would pass with the sort deleted. Feeding a deliberately scrambled order is
    what makes the assertion actually load-bearing.
    """
    from occupancy_graph.source.resolve import TaxScanResult, ZipScanResult

    async def fake_zip_scan(_pool, _query):
        empty = {shape: [] for shape in ("trace", "base", "loan", "drive", "auto")}
        return ZipScanResult(
            rows_by_shape={"utility": list(utility_rows), **empty},
            city="LEXINGTON", state="KY",
        )

    async def fake_tax_scan(_pool, _query, *, city, state):
        return TaxScanResult(
            rows=list(tax_rows), dropped=0, queried_city=city, queried_state=state
        )

    monkeypatch.setattr(bundle_module, "scan_zip_sources", fake_zip_scan)
    monkeypatch.setattr(bundle_module, "scan_tax_source", fake_tax_scan)


async def test_materialize_orders_rows_by_record_id_whatever_order_the_scan_returned(
    pool, monkeypatch
):
    """POST /v1/resolve shows the engine the FIRST N rows per shape, so the
    order the plan happens to emit must not leak into the response."""
    _patch_scans(
        monkeypatch,
        utility_rows=[
            _utility_row(1003, "Third"), _utility_row(1001, "First"), _utility_row(1002, "Second")
        ],
        tax_rows=[_tax_row(4002, "SECOND OWNER"), _tax_row(4001, "FIRST OWNER")],
    )
    bundle = await materialize(pool, "123 Main St", "40505", address_id=1)

    assert [row["first_name"] for row in bundle.rows_by_shape["utility"]] == [
        "First", "Second", "Third"
    ]
    assert [row["ownername"] for row in bundle.rows_by_shape["tax"]] == [
        "FIRST OWNER", "SECOND OWNER"
    ]


async def test_materialize_sorts_a_null_record_id_last_rather_than_raising(pool, monkeypatch):
    """record_id has no NOT NULL constraint. A None mixed with ints would raise
    TypeError on comparison and take the whole investigation down."""
    _patch_scans(
        monkeypatch,
        utility_rows=[_utility_row(None, "NoId"), _utility_row(1001, "First")],
        tax_rows=[_tax_row(None, "NO ID"), _tax_row(None, "ALSO NO ID")],
    )
    bundle = await materialize(pool, "123 Main St", "40505", address_id=1)

    assert [row["first_name"] for row in bundle.rows_by_shape["utility"]] == ["First", "NoId"]
    assert len(bundle.rows_by_shape["tax"]) == 2


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


# --- Review fix 1: concurrent callers for the same address must not each ---
# --- run their own scan; they must await the one materialize() in flight ---


async def test_concurrent_resolve_of_a_new_address_materializes_only_once(pool, monkeypatch):
    calls = 0
    orig_materialize = bundle_module.materialize

    async def counting_materialize(*args, **kwargs):
        nonlocal calls
        calls += 1
        return await orig_materialize(*args, **kwargs)

    monkeypatch.setattr(bundle_module, "materialize", counting_materialize)

    cache = BundleCache(pool)
    bundles = await asyncio.gather(
        *(cache.resolve("456 Concurrent Ave", "40505") for _ in range(5))
    )
    assert calls == 1
    assert all(b is bundles[0] for b in bundles)


async def test_concurrent_get_after_eviction_materializes_only_once(pool, monkeypatch):
    cache = BundleCache(pool)
    first = await cache.resolve("123 Main St", "40505")
    cache.evict_hot(first.address_id)

    calls = 0
    orig_materialize = bundle_module.materialize

    async def counting_materialize(*args, **kwargs):
        nonlocal calls
        calls += 1
        return await orig_materialize(*args, **kwargs)

    monkeypatch.setattr(bundle_module, "materialize", counting_materialize)

    bundles = await asyncio.gather(*(cache.get(first.address_id) for _ in range(5)))
    assert calls == 1
    assert all(b is bundles[0] for b in bundles)
    assert all(b.address_id == first.address_id for b in bundles)


async def test_materialize_failure_propagates_to_every_waiter_then_allows_retry(
    pool, monkeypatch
):
    calls = 0

    async def failing_materialize(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise RuntimeError("boom")

    monkeypatch.setattr(bundle_module, "materialize", failing_materialize)

    cache = BundleCache(pool)
    results = await asyncio.gather(
        *(cache.resolve("789 Failure Blvd", "40505") for _ in range(3)),
        return_exceptions=True,
    )
    assert calls == 1  # only the owner ran materialize; waiters shared its Task
    assert len(results) == 3
    assert all(isinstance(r, RuntimeError) for r in results)
    # The dead Task's entry must be gone -- not left behind for the next
    # caller to await forever.
    assert cache._inflight == {}

    calls = 0
    with pytest.raises(RuntimeError):
        await cache.resolve("789 Failure Blvd", "40505")
    assert calls == 1  # a fresh Task was started, not a stale/dead one reused


# --- Review fix 2: the hot tier is bounded by an LRU cap ---


async def test_hot_cache_evicts_the_least_recently_used_beyond_the_cap(pool, monkeypatch):
    monkeypatch.setattr(bundle_module, "MAX_HOT_BUNDLES", 2)

    cache = BundleCache(pool)
    first = await cache.resolve("123 Main St", "40505")
    second = await cache.resolve("999 Nowhere Rd", "40505")
    third = await cache.resolve("Esther St", "02920")

    assert len(cache._hot) == 2
    assert first.address_id not in cache._hot
    assert second.address_id in cache._hot
    assert third.address_id in cache._hot

    # The cold tier is unbounded, so the evicted address still recovers.
    recovered = await cache.get(first.address_id)
    assert recovered is not None
    assert recovered.address_id == first.address_id
    assert recovered.norm_address == first.norm_address
