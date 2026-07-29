"""The EXPLAIN cost ceilings, and the bracket they must sit inside.

The live corpus is not reachable from the test environment, so these tests pin
the DERIVATION rather than re-measuring it: the ceilings must sit above every
access path the fixture can plan, and a deliberately runaway plan must sit
above the ceiling. docs/explain-cost-calibration.md carries the arithmetic and
the re-tuning procedure.
"""
from __future__ import annotations

import json

import pytest

from occupancy_graph.service import limits

# The four documented real-corpus access paths, expressed against the fixture.
ACCESS_PATHS = {
    "zip+prefix (173 ms - 32 s)": """
        SELECT * FROM public.records_legacy
        WHERE zip = '40505' AND address ILIKE '123 MAIN%' LIMIT 200
    """,
    "city/state+prefix (613 ms - 53 s)": """
        SELECT * FROM public.records_partitioned
        WHERE upper(state) = 'KY' AND upper(city) = 'LEXINGTON'
          AND address ILIKE '123 MAIN%' LIMIT 200
    """,
    "last_name+zip (1 ms warm / 222 ms cold)": """
        SELECT * FROM public.records_legacy
        WHERE last_name = 'Doe' AND zip = '40505' LIMIT 50
    """,
    "entity_links by hal_id (215 ms)": """
        SELECT * FROM silver.entity_links WHERE hal_id = 'HAL0001'
    """,
}

# Worst case for the planner's row estimate is 1000 rows per generate_series
# (no prosupport); best case is 10^6 each. Either way a four-way cross join is
# orders of magnitude above the ceiling, so this test does not depend on which.
RUNAWAY = """
    SELECT count(*)
    FROM generate_series(1, 1000000) a, generate_series(1, 1000000) b,
         generate_series(1, 1000000) c, generate_series(1, 1000000) d
"""


async def _plan_cost(pool, sql: str) -> float:
    async with pool.acquire() as conn:
        raw = await conn.fetchval(f"EXPLAIN (FORMAT JSON, COSTS TRUE) {sql}")
    plans = json.loads(raw) if isinstance(raw, str) else raw
    return float(plans[0]["Plan"]["Total Cost"])


def test_default_plan_cost_ceiling_is_the_calibrated_value(monkeypatch):
    monkeypatch.delenv("SQL_HATCH_MAX_PLAN_COST", raising=False)
    assert limits.max_plan_cost() == 5_000_000.0


def test_plan_cost_ceiling_is_overridable_for_retuning(monkeypatch):
    monkeypatch.setenv("SQL_HATCH_MAX_PLAN_COST", "250000")
    assert limits.max_plan_cost() == 250_000.0


def test_a_malformed_ceiling_fails_loudly_rather_than_defaulting(monkeypatch):
    monkeypatch.setenv("SQL_HATCH_MAX_PLAN_COST", "cheap")
    with pytest.raises(ValueError, match="SQL_HATCH_MAX_PLAN_COST"):
        limits.max_plan_cost()


def test_the_records_seqscan_ceiling_is_far_below_the_global_ceiling(monkeypatch):
    monkeypatch.delenv("SQL_HATCH_MAX_RECORDS_SEQSCAN_COST", raising=False)
    monkeypatch.delenv("SQL_HATCH_MAX_PLAN_COST", raising=False)
    assert limits.max_records_seqscan_cost() == 50_000.0
    assert limits.max_records_seqscan_cost() < limits.max_plan_cost() / 10


def test_records_relations_are_recognised_including_partition_children():
    assert limits.is_records_relation("records_legacy")
    assert limits.is_records_relation("records_partitioned")
    assert limits.is_records_relation("records_partitioned_p20260301")
    assert limits.is_records_relation("records_new")
    assert not limits.is_records_relation("entity_links")
    assert not limits.is_records_relation("entity_master")


async def test_every_documented_access_path_plans_below_the_ceiling(fixture_pool):
    ceiling = limits.max_plan_cost()
    for name, sql in ACCESS_PATHS.items():
        cost = await _plan_cost(fixture_pool, sql)
        assert cost < ceiling, f"{name} would be refused at cost {cost}"


async def test_a_runaway_plan_sits_above_the_ceiling(fixture_pool):
    assert await _plan_cost(fixture_pool, RUNAWAY) > limits.max_plan_cost()
