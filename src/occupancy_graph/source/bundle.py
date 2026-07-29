"""The materialized address bundle and its cache.

The engine does one preflight and then 10-30 tool calls against the same address.
Querying per resolver would re-run a 173 ms - 32 s scan every time, so the scan
happens once and every later resolver reads this bundle.

The cache is two-tier so eviction is recoverable rather than fatal:
  hot  — address_id -> AddressBundle          (~30 min; an investigation is minutes)
  cold — address_id -> (address, zip, ...)    (~24 h; tiny)
A hot miss with a cold hit re-materializes. Both missing returns None, which
`address(id: Int!)` already permits.
"""
from __future__ import annotations

import asyncio
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field

from occupancy_graph.normalize import normalize_address, zip5
from occupancy_graph.source.manifest import SHAPES
from occupancy_graph.source.pool import PartnerPool
from occupancy_graph.source.project import project_row
from occupancy_graph.source.resolve import AddressQuery, scan_tax_source, scan_zip_sources

HOT_TTL_SECONDS = 30 * 60
COLD_TTL_SECONDS = 24 * 60 * 60

# Hot-tier size ceiling. TTL alone is lazy -- an expired entry is only purged
# when that exact id is looked up again, so an address resolved once and never
# revisited would stay forever. A realistic bundle is ~4.8 KB; the worst case
# (7 shapes x MAX_ROWS_PER_SHAPE=200 rows each) is ~281 KB, so this caps
# hot-tier memory at a few tens of MB even under sustained load. `_cold` stays
# unbounded: entries are ~100-200 bytes and it is the recovery tier.
MAX_HOT_BUNDLES = 256


@dataclass
class AddressBundle:
    address_id: int
    norm_address: str
    zip5: str
    street_number: str | None
    street_name: str | None
    unit: str | None
    city: str | None
    state: str | None
    county: str | None
    rows_by_shape: dict[str, list[dict]] = field(default_factory=dict)
    source_counts: dict[str, int] = field(default_factory=dict)
    dropped_counts: dict[str, int] = field(default_factory=dict)
    tax_timed_out: bool = False

    @property
    def relation_count(self) -> int:
        return sum(self.source_counts.values())


def _split_address(norm_address: str) -> tuple[str | None, str | None, str | None]:
    match = re.match(r"^\s*(\d+)\s+(.*?)(?:\s+(?:APT|STE|UNIT|#)\s*(\S+))?\s*$", norm_address)
    if not match:
        return None, norm_address or None, None
    return match.group(1), (match.group(2) or None), match.group(3)


async def materialize(
    pool: PartnerPool, address: str, zip_code: str | None, *, address_id: int
) -> AddressBundle:
    query = AddressQuery.build(address, zip_code)
    zip_scan = await scan_zip_sources(pool, query)
    tax_scan = await scan_tax_source(pool, query, city=zip_scan.city, state=zip_scan.state)

    rows_by_shape: dict[str, list[dict]] = {}
    dropped: dict[str, int] = {}
    for shape, rows in zip_scan.rows_by_shape.items():
        rows_by_shape[shape] = [project_row(SHAPES[shape], row) for row in rows]
        dropped[shape] = 0
    rows_by_shape["tax"] = [project_row(SHAPES["tax"], row) for row in tax_scan.rows]
    dropped["tax"] = tax_scan.dropped

    street_number, street_name, unit = _split_address(query.norm_address)
    county = next(
        (row.get("county") for rows in rows_by_shape.values() for row in rows if row.get("county")),
        None,
    )
    return AddressBundle(
        address_id=address_id,
        norm_address=query.norm_address,
        zip5=query.zip5,
        street_number=street_number,
        street_name=street_name,
        unit=unit,
        city=zip_scan.city,
        state=zip_scan.state,
        county=county,
        rows_by_shape=rows_by_shape,
        source_counts={shape: len(rows) for shape, rows in rows_by_shape.items()},
        dropped_counts=dropped,
        tax_timed_out=tax_scan.timed_out,
    )


@dataclass
class _ColdEntry:
    address: str
    zip_code: str
    expires_at: float


class BundleCache:
    """Mints address ids and serves bundles for them."""

    def __init__(self, pool: PartnerPool) -> None:
        self._pool = pool
        self._next_id = 1
        self._by_key: dict[tuple[str, str], int] = {}
        # OrderedDict so `_store_hot`/`_hot_get` can track recency for the LRU
        # cap: `move_to_end` on every touch, `popitem(last=False)` evicts the
        # least-recently-used entry once len() exceeds MAX_HOT_BUNDLES.
        self._hot: OrderedDict[int, tuple[AddressBundle, float]] = OrderedDict()
        self._cold: dict[int, _ColdEntry] = {}
        # address_id -> in-progress materialize() Task. resolve()/get() both
        # await materialize() between the hot-miss check and the hot-store;
        # without this, every concurrent caller for the same address starts
        # its own scan (measured: 5 concurrent callers -> 5 scans). Concurrent
        # callers for the SAME address_id instead await the one Task already
        # in flight. See `_materialize_once`.
        self._inflight: dict[int, asyncio.Task[AddressBundle]] = {}

    async def resolve(self, address: str, zip_code: str | None) -> AddressBundle:
        norm = normalize_address(address)
        key = (norm, zip5(zip_code))
        address_id = self._by_key.get(key)
        if address_id is None:
            address_id = self._next_id
            self._next_id += 1
            self._by_key[key] = address_id
        self._cold[address_id] = _ColdEntry(
            address=address, zip_code=zip_code or "", expires_at=time.monotonic() + COLD_TTL_SECONDS
        )
        cached = self._hot_get(address_id)
        if cached is not None:
            return cached
        return await self._materialize_once(address_id, address, zip_code)

    async def get(self, address_id: int) -> AddressBundle | None:
        cached = self._hot_get(address_id)
        if cached is not None:
            return cached
        cold = self._cold.get(address_id)
        if cold is None or cold.expires_at < time.monotonic():
            self._cold.pop(address_id, None)
            return None
        return await self._materialize_once(address_id, cold.address, cold.zip_code)

    async def _materialize_once(
        self, address_id: int, address: str, zip_code: str | None
    ) -> AddressBundle:
        """Ensure only ONE materialization per address is ever in flight.

        Without this, `resolve()`/`get()` each `await materialize(...)` between
        the hot-miss check and the hot-store, so N concurrent callers for the
        same address_id all pass the miss check before any of them stores,
        and all N run their own scan -- exactly the cost this cache exists to
        eliminate (measured: 5 concurrent callers -> 5 scans, last writer wins
        the hot slot).

        The first caller in ("the owner") creates the Task and registers it in
        `_inflight`; every later concurrent caller for the same address_id
        finds it already there and awaits that SAME Task instead of starting
        its own. Only the owner ever removes the `_inflight` entry -- a waiter
        must not, or it could delete a different, newer owner's in-flight
        entry for the same id. The removal happens in a `finally` so it runs
        whether `materialize` succeeds or raises: `asyncio.Task` caches an
        exception and re-raises it to every awaiter (owner and waiters alike),
        so a failure propagates everywhere, and because the entry is already
        gone by the time that happens, the next call for this address_id
        starts a fresh Task rather than awaiting the dead one.
        """
        task = self._inflight.get(address_id)
        if task is None:
            task = asyncio.create_task(
                materialize(self._pool, address, zip_code, address_id=address_id)
            )
            self._inflight[address_id] = task
            try:
                bundle = await task
            finally:
                self._inflight.pop(address_id, None)
        else:
            bundle = await task
        self._store_hot(address_id, bundle)
        return bundle

    def evict_hot(self, address_id: int) -> None:
        self._hot.pop(address_id, None)

    def _hot_get(self, address_id: int) -> AddressBundle | None:
        entry = self._hot.get(address_id)
        if entry is None:
            return None
        bundle, expires_at = entry
        if expires_at < time.monotonic():
            del self._hot[address_id]
            return None
        self._hot.move_to_end(address_id)
        return bundle

    def _store_hot(self, address_id: int, bundle: AddressBundle) -> None:
        self._hot[address_id] = (bundle, time.monotonic() + HOT_TTL_SECONDS)
        self._hot.move_to_end(address_id)
        while len(self._hot) > MAX_HOT_BUNDLES:
            self._hot.popitem(last=False)
