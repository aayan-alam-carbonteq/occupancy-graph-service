"""Measure the resident hop's coverage loss on records_legacy -- a number
production can never give us.

    .venv/bin/python -m clone.coverage_experiment

RUN AS A MODULE, NEVER AS A SCRIPT PATH -- see clone/README.md and
clone/load.py's identical warning; `clone` is not an installed package.

THIS SCRIPT DOES NOT ASSERT. It is a measurement instrument, not a test.

WHY THIS CANNOT BE MEASURED AGAINST PRODUCTION. source/resolve.py::
scan_zip_sources routes records_legacy through `_scan_legacy_via_residents`
(the "resident hop") instead of the collapsed zip+address scan every other
table gets, because that scan was observed server-side ACTIVE at 14+ minutes
without completing against the live corpus -- see that function's docstring.
There is therefore no live measurement of what the hop MISSES: the only way
to know is to run the full scan too and diff them, and the full scan is
exactly the thing that does not finish live. Locally, both paths run in
milliseconds (2.36M rows fit in RAM -- see clone/README.md's "What this
CANNOT test"), so this is the one place the diff is even computable.

WHAT "RECALL" MEANS HERE. `full` is every distinct record_id the collapsed
zip+address scan finds for this address (capped at MAX_ROWS_PER_SHAPE per
shape group, same as production); `hop` is every record_id the resident hop
finds. Recall = |hop & full| / |full| -- the fraction of the rows a normal
scan would show the engine that the hop actually recovers. A LOW recall
alone is ambiguous: it could mean the hop's mechanism (anchor -> name ->
address-filtered rows) is lossy, or it could mean there was never an anchor
to hop from in the first place. Splitting by anchor availability (below)
is what tells those two apart -- conflating them, as the task brief notes,
produces a meaningless average.

THIS IS A LOWER BOUND ON PRODUCTION RECALL, not an estimate of it. This
clone's loader reproduces exactly 1 of production's 4 house_number-bearing
anchor feeds (USCRM, loaded as the `base` shape's records_legacy plan -- see
clone/loader/feedplan.py); production also has SSNxDOB, the 2014 phonebook,
and Historic Data. Every one of those is a feed of rows the live hop can
anchor from that this clone cannot. More anchor feeds means more residents
get named per address, which can only raise recall, never lower it -- so
whatever this script measures, production's hop has strictly more to work
with.
"""
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass

from occupancy_graph.source.feeds import pattern_groups
from occupancy_graph.source.pool import PartnerPool
from occupancy_graph.source.resolve import (
    ANCHOR_LIMIT,
    ZIP_SHAPES,
    AddressQuery,
    _scan_legacy_via_residents,
    _scan_table,
)

DEFAULT_CLONE_DSN = "postgresql://clone:clone@127.0.0.1:55433/partner_clone"

# (address, zip) -- the same 12 mini.csv gold-label addresses
# clone/compare_to_live.py diffs against the recorded live baseline. Reused
# here rather than re-typed so the two scripts can never silently drift onto
# different address sets.
ADDRESSES: tuple[tuple[str, str], ...] = (
    ("1104 SPRING RUN RD", "40514"),
    ("1552 SAMARA GLEN WAY", "40515"),
    ("548 RHODORA RDG", "40517"),
    ("2812 RED LEAF DR", "40509"),
    ("849 W MAXWELL ST", "40508"),
    ("535 LONE OAK DR", "40503"),
    ("1000 TURNBERRY LN", "40515"),
    ("1004 SPRING RUN RD", "40514"),
    ("1057 SPRING RUN RD", "40514"),
    ("1101 WELDON CT", "40515"),
    ("115 WABASH DR", "40503"),
    ("1332 OX HILL DR", "40517"),
)


@dataclass
class AddressResult:
    address: str
    zip_code: str
    full: set
    hop: set
    anchors_total: int
    anchors_surviving: int

    @property
    def missed(self) -> set:
        return self.full - self.hop

    @property
    def hit(self) -> set:
        return self.hop & self.full

    @property
    def recall(self) -> float | None:
        """None, not 0.0, when `full` is empty -- there is nothing to recall,
        which is a different fact from recalling nothing out of something."""
        return len(self.hit) / len(self.full) if self.full else None

    @property
    def has_anchor(self) -> bool:
        return self.anchors_surviving >= 1


async def _anchor_availability(pool: PartnerPool, query: AddressQuery) -> tuple[int, int]:
    """(total anchors at zip+house_number, anchors surviving the address
    prefix filter) -- i.e. how many rows `_scan_legacy_via_residents` would
    see before/after the `query.matches_prefix` check its own `at_address`
    applies (source/resolve.py lines ~317-318).

    Deliberately UNCAPPED (no ANCHOR_LIMIT here) so a total above ANCHOR_LIMIT
    is visible instead of silently matching the hop's own cap -- if the hop
    itself is ever the bottleneck rather than anchor scarcity, this is where
    that would show up.
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT address FROM public.records_legacy WHERE zip = $1 AND house_number = $2",
            query.zip5, query.house_number,
        )
    total = len(rows)
    surviving = sum(1 for row in rows if query.matches_prefix(row["address"]))
    return total, surviving


def _fmt_recall(recall: float | None) -> str:
    return f"{recall:.1%}" if recall is not None else "n/a"


def _group_recall(results: list[AddressResult]) -> tuple[int, int, float | None]:
    full_sum = sum(len(r.full) for r in results)
    hit_sum = sum(len(r.hit) for r in results)
    return full_sum, hit_sum, (hit_sum / full_sum if full_sum else None)


async def _measure(pool: PartnerPool) -> list[AddressResult]:
    groups = pattern_groups(ZIP_SHAPES, "records_legacy")
    results = []
    for address, zip_code in ADDRESSES:
        query = AddressQuery.build(address, zip_code)
        full = {r["record_id"] for r in await _scan_table(pool, "records_legacy", groups, query)}
        hop = {r["record_id"] for r in await _scan_legacy_via_residents(pool, query)}
        anchors_total, anchors_surviving = await _anchor_availability(pool, query)
        results.append(AddressResult(address, zip_code, full, hop, anchors_total, anchors_surviving))
    return results


async def main() -> None:
    clone_dsn = os.environ.get("CLONE_DSN", DEFAULT_CLONE_DSN)
    pool = await PartnerPool.create(clone_dsn)
    try:
        results = await _measure(pool)
    finally:
        await pool.close()

    # --- main table: full vs hop, with the anchor columns alongside --------
    header = (
        f"{'address':<22} {'zip':<5} {'full':>5} {'hop':>5} {'missed':>7} "
        f"{'recall':>7}   {'anchors':>7} {'survive':>8}"
    )
    print(header)
    print("-" * len(header))
    for r in results:
        print(
            f"{r.address:<22} {r.zip_code:<5} {len(r.full):>5} {len(r.hop):>5} "
            f"{len(r.missed):>7} {_fmt_recall(r.recall):>7}   "
            f"{r.anchors_total:>7} {r.anchors_surviving:>8}"
        )
    print("-" * len(header))
    overall_full, overall_hit, overall_recall = _group_recall(results)
    overall_missed = overall_full - overall_hit
    print(
        f"{'OVERALL':<22} {'':<5} {overall_full:>5} {'':>5} {overall_missed:>7} "
        f"{_fmt_recall(overall_recall):>7}"
    )
    print(
        "\n(`full`/`hop`/`missed` are per-address record_id set sizes -- OVERALL sums "
        "them rather than re-unioning, since record_ids never repeat across these 12 "
        "addresses. `anchors` = rows at zip+house_number with no prefix filter; "
        "`survive` = the subset matching the address prefix, i.e. what "
        "`_scan_legacy_via_residents` actually seeds its name hops from. "
        f"ANCHOR_LIMIT is {ANCHOR_LIMIT}; an `anchors` value above it would mean the "
        "hop's own cap, not scarcity, is the bottleneck -- not observed below.)"
    )

    # --- (a)+(b): split recall by anchor availability -----------------------
    # Conflating "the hop ran and missed rows" with "the hop had nothing to
    # anchor from" produces a meaningless average -- an address with 0
    # surviving anchors CANNOT produce a hop result no matter how good the
    # name-hop mechanism is, so its 0% recall is not a statement about the
    # mechanism at all.
    with_anchor = [r for r in results if r.has_anchor]
    without_anchor = [r for r in results if not r.has_anchor]

    def _print_split(label: str, rows: list[AddressResult]) -> None:
        print(f"\n{label} ({len(rows)}/{len(results)} addresses):")
        if not rows:
            print("  (none)")
            return
        for r in rows:
            print(
                f"  {r.address:<22} {r.zip_code:<5} full={len(r.full):<4} "
                f"hop={len(r.hop):<4} missed={len(r.missed):<4} "
                f"recall={_fmt_recall(r.recall)}"
            )
        full_sum, hit_sum, group_recall = _group_recall(rows)
        print(f"  -> group recall: {_fmt_recall(group_recall)} ({hit_sum}/{full_sum})")

    print("\n" + "=" * len(header))
    print("RECALL SPLIT BY ANCHOR AVAILABILITY")
    print("=" * len(header))
    _print_split("WITH >=1 surviving anchor -- the hop's TRUE recall", with_anchor)
    _print_split("WITH 0 surviving anchors -- the anchor-COVERAGE problem, not the hop", without_anchor)

    print(
        "\nLOWER BOUND, not an estimate: this clone reproduces 1 of production's 4 "
        "house_number-bearing anchor feeds (USCRM only). Production also has SSNxDOB, "
        "the 2014 phonebook, and Historic Data -- three more feeds the live hop can "
        "anchor from that this clone cannot. More anchor feeds can only ADD surviving "
        "anchors and named residents, never remove them, so production's hop has "
        "strictly more to work with than what is measured above. Whatever recall this "
        "script reports, live recall is >= it, and the 0-anchor addresses above are "
        "the ones most likely to improve -- they are exactly where a 2nd/3rd/4th "
        "anchor feed would first start mattering."
    )


if __name__ == "__main__":
    asyncio.run(main())
