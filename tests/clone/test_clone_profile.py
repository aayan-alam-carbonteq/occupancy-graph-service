"""Fidelity proof for the loaded partner-clone corpus. Skipped unless
`CLONE_DSN` is set -- exactly like `tests/test_live_smoke.py` skips without
`PARTNER_DSN`, so CI without a clone stays green.

Test 1 is the one that matters most: the feed round trip. It proves the
service can find, and correctly classify, the rows the loader wrote. A
mismatch between what the loader writes as `source_file` and what
`source/feeds.py` selects with `LIKE` would make rows invisible to the very
service that loaded them -- the exact class of bug that once let five of
seven shapes name a nonexistent relation (`public.records_partitioned`) while
548 tests passed against a fixture that modelled a topology production does
not have.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio

from occupancy_graph.source.feeds import FEEDS, feed_clause

CLONE_DSN = os.environ.get("CLONE_DSN")
pytestmark = pytest.mark.skipif(not CLONE_DSN, reason="CLONE_DSN is not set")

ALL_SHAPES = ("utility", "trace", "base", "loan", "drive", "auto", "tax")

POPULATION_PROFILE = Path(__file__).parents[2] / "clone" / "profiles" / "feed_population.json"
POPULATION_TARGETS: dict[str, dict[str, float]] = json.loads(POPULATION_PROFILE.read_text())

# Percentage points. See test_column_population_is_near_the_recorded_production_targets
# for why this is not tightened to hide a real loader gap.
POPULATION_TOLERANCE_PP = 5.0


@pytest_asyncio.fixture
async def clone():
    conn = await asyncpg.connect(CLONE_DSN)
    try:
        yield conn
    finally:
        await conn.close()


@pytest.mark.parametrize("shape", ALL_SHAPES)
async def test_every_shape_is_reachable_through_its_own_feed_patterns(clone, shape):
    """The round trip: loader wrote `source_file`, feeds.py selects it back with
    `LIKE`. `drive` has no feed of its own -- it is loan rows with `dl_number
    NOT NULL` -- so it passes only if the licence fold-in worked."""
    clause, params = feed_clause(shape, start_index=1)
    found = 0
    for table in FEEDS[shape].tables:
        found += await clone.fetchval(
            f"SELECT count(*) FROM public.{table} WHERE {clause}", *params
        )
    assert found > 0, f"{shape}: feeds.py patterns select nothing from the clone"


async def test_omitted_shapes_are_absent(clone):
    """voter/criminal/linkedin/realtor do not exist in production; loading them
    would let the local engine find evidence it can never find live."""
    for feed in ("voter%", "criminal%", "linkedin%", "realtor%"):
        for table in ("records_legacy", "records_new"):
            count = await clone.fetchval(
                f"SELECT count(*) FROM public.{table} WHERE source_file LIKE $1", feed
            )
            assert count == 0, f"{table}: {feed} matched {count} rows that should not exist"


async def test_house_number_is_populated_only_where_production_populates_it(clone):
    """The rule that keeps the coverage experiment valid. Our CSVs carry
    `housenumber` at ~100% on trace/auto/tax where production has it NULL;
    populating it naively would give the resident hop near-total coverage
    locally while production's is partial."""
    for feed, expect_any in (
        ("Export Utility%", False),
        ("Trace Skipping%", False),
        ("property_owner%", False),
        ("2026.1-USCRM%", True),
    ):
        got = 0
        for table in ("records_legacy", "records_new"):
            got += await clone.fetchval(
                f"""SELECT count(*) FROM public.{table}
                    WHERE source_file LIKE $1 AND house_number IS NOT NULL""",
                feed,
            )
        assert (got > 0) is expect_any, (
            f"{feed}: house_number population is {got} rows, expected "
            f"{'some' if expect_any else 'none'}"
        )


async def test_entity_links_use_only_the_ssn_blocking_key(clone):
    rows = await clone.fetch("SELECT DISTINCT match_type FROM silver.entity_links")
    assert {row["match_type"] for row in rows} == {"ssn"}


async def test_tax_rows_have_no_entity_links(clone):
    """property_owner has ssn/dob/house_number all 0% -- no blocking key at
    all, which is why production's graph contains none of it."""
    orphaned = await clone.fetchval(
        """
        SELECT count(*) FROM silver.entity_links l
        JOIN public.records_new r ON r.record_id = l.record_id
        WHERE r.source_file LIKE 'property_owner%'
        """
    )
    assert orphaned == 0


async def test_record_count_equals_the_actual_link_count(clone):
    """EMERGENT, never stamped -- search_people ORDERS BY record_count, so a
    stamped value would make entity_master contradict its own links."""
    mismatched = await clone.fetchval(
        """
        SELECT count(*) FROM silver.entity_master m
        WHERE m.record_count <> (
          SELECT count(*) FROM silver.entity_links l WHERE l.hal_id = m.hal_id
        )
        """
    )
    assert mismatched == 0


async def test_the_oracle_covers_every_loaded_record(clone):
    for table in ("records_legacy", "records_new"):
        records = await clone.fetchval(f"SELECT count(*) FROM public.{table}")
        oracled = await clone.fetchval(
            "SELECT count(*) FROM bench.true_person_record WHERE source_table = $1", table
        )
        assert records == oracled, (
            f"{table}: {records} loaded records but the oracle covers {oracled}"
        )


@pytest.mark.parametrize("shape", sorted(POPULATION_TARGETS))
async def test_column_population_is_near_the_recorded_production_targets(clone, shape):
    """NEW. The loader's type-coercion layer (clone/load.py) DROPS unparseable
    values rather than guessing -- e.g. base.csv's `businessowner` holds vendor
    codes like 'A'/'2'/'9', not booleans -- and it reported dropping 99,540
    `dob` values. That drop stacks on top of the intentional down-sampling in
    clone/loader/population.py, so actual population can land under target even
    when the sampler itself is working exactly as designed.

    This measures what the clone ACTUALLY carries, per shape/column, against
    the production targets recorded in clone/profiles/feed_population.json, at
    a ±5 percentage point tolerance. A failure here is real information about
    loader fidelity, not a reason to widen the tolerance.
    """
    columns = POPULATION_TARGETS[shape]
    clause, params = feed_clause(shape, start_index=1)
    tables = FEEDS[shape].tables

    total = 0
    for table in tables:
        total += await clone.fetchval(
            f"SELECT count(*) FROM public.{table} WHERE {clause}", *params
        )
    assert total > 0, f"{shape}: no rows selected by its own feed patterns"

    report = []
    mismatches = []
    for column, target_pct in columns.items():
        non_null = 0
        for table in tables:
            non_null += await clone.fetchval(
                f"""SELECT count(*) FROM public.{table}
                    WHERE {clause} AND {column} IS NOT NULL""",
                *params,
            )
        actual_pct = 100.0 * non_null / total
        line = (
            f"{shape}.{column}: target={target_pct:.1f}% actual={actual_pct:.1f}% "
            f"(n={non_null}/{total})"
        )
        report.append(line)
        if abs(actual_pct - target_pct) > POPULATION_TOLERANCE_PP:
            mismatches.append(line)

    print("\n" + "\n".join(report))
    assert not mismatches, (
        f"{shape}: population drifted more than {POPULATION_TOLERANCE_PP} "
        "percentage points from the recorded production target on: "
        + "; ".join(mismatches)
    )
