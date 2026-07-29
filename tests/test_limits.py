"""The EXPLAIN cost ceilings: their constants, their range rules, and a bracket.

The live corpus is not reachable from the test environment, so these tests do
not re-measure the derivation -- they pin the constants and hold the DIRECTION
of the bracket. Be honest about how loose that bracket is: the fixture's tables
hold ~20 rows, so every path it can plan costs single digits. What the two
fixture tests establish is that each documented access path falls below the
ceiling and a runaway plan falls above it, NOT that 5e6 is the right magnitude.
The arithmetic behind the magnitude, and the procedure for re-deriving it
against the real corpus, live in docs/explain-cost-calibration.md.

The range rules carry more weight than the bracket. A ceiling of inf or nan
compares False against every cost and so silently disables the gate it exists
to enforce; a statement timeout of 0 means UNLIMITED to Postgres. Those are
tested here because nothing downstream can detect them.
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


def test_the_seqscan_ceiling_is_overridable_for_retuning(monkeypatch):
    """Guards the env var NAME. Without this, renaming the string in limits.py
    breaks nothing in the suite and an operator's override is silently ignored
    forever while the service runs on the default."""
    monkeypatch.setenv("SQL_HATCH_MAX_RECORDS_SEQSCAN_COST", "125000")
    assert limits.max_records_seqscan_cost() == 125_000.0


def test_a_zero_seqscan_ceiling_is_legal_because_it_refuses_every_seqscan(monkeypatch):
    """0 is the strictest setting, not a broken one: every seq scan costs more
    than 0, so the rule becomes unconditional. The range rule must therefore be
    `>= 0`, not `> 0`."""
    monkeypatch.setenv("SQL_HATCH_MAX_RECORDS_SEQSCAN_COST", "0")
    assert limits.max_records_seqscan_cost() == 0.0


@pytest.mark.parametrize(
    "var, accessor",
    [
        ("SQL_HATCH_MAX_PLAN_COST", limits.max_plan_cost),
        ("SQL_HATCH_MAX_RECORDS_SEQSCAN_COST", limits.max_records_seqscan_cost),
    ],
)
@pytest.mark.parametrize("value", ["inf", "Infinity", "+inf", "nan", "NaN", "-1"])
def test_a_ceiling_that_could_not_refuse_anything_is_rejected(
    monkeypatch, var, accessor, value
):
    """`cost > inf` and `cost > nan` are both False for every cost, so either
    value turns the gate into an unconditional allow -- the exact opposite of
    what the operator setting it intends. Negative is meaningless."""
    monkeypatch.setenv(var, value)
    with pytest.raises(ValueError, match=var):
        accessor()


def test_the_row_cap_default_and_override(monkeypatch):
    monkeypatch.delenv("SQL_HATCH_MAX_ROWS", raising=False)
    assert limits.max_sql_rows() == 500
    monkeypatch.setenv("SQL_HATCH_MAX_ROWS", "50")
    assert limits.max_sql_rows() == 50


def test_the_statement_timeout_default_and_override(monkeypatch):
    monkeypatch.delenv("SQL_HATCH_TIMEOUT_MS", raising=False)
    assert limits.sql_timeout_ms() == 20_000
    monkeypatch.setenv("SQL_HATCH_TIMEOUT_MS", "5000")
    assert limits.sql_timeout_ms() == 5_000


@pytest.mark.parametrize("value", ["0", "-1", "none"])
def test_an_unbounded_statement_timeout_is_refused(monkeypatch, value):
    """Postgres reads `statement_timeout = 0` as UNLIMITED, so `0` -- the
    natural way to write "no timeout for the backfill job" in a compose file --
    would let one LLM-authored query run without bound against the partner
    corpus. source/pool.py already raises on this exact knob; the hatch must not
    reintroduce it."""
    monkeypatch.setenv("SQL_HATCH_TIMEOUT_MS", value)
    with pytest.raises(ValueError, match="SQL_HATCH_TIMEOUT_MS"):
        limits.sql_timeout_ms()


def test_the_timeout_refusal_names_the_consequence(monkeypatch):
    """A range error that only says "must be >= 1" invites someone to widen the
    bound. Name what 0 actually does, as pool.py does."""
    monkeypatch.setenv("SQL_HATCH_TIMEOUT_MS", "0")
    with pytest.raises(ValueError, match="UNLIMITED"):
        limits.sql_timeout_ms()


@pytest.mark.parametrize("value", ["0", "-1", "lots"])
def test_a_row_cap_that_would_not_bound_the_hatch_is_refused(monkeypatch, value):
    monkeypatch.setenv("SQL_HATCH_MAX_ROWS", value)
    with pytest.raises(ValueError, match="SQL_HATCH_MAX_ROWS"):
        limits.max_sql_rows()


def test_records_relations_are_recognised_including_partition_children():
    assert limits.is_records_relation("records_legacy")
    assert limits.is_records_relation("records_partitioned")
    assert limits.is_records_relation("records_partitioned_p20260301")
    # NOT view coverage: a plan over the records_new view names its partition
    # children, never the view itself (views are inlined at rewrite), so the
    # view is covered by the records_partitioned prefix above. This asserts only
    # that the dead records_new prefix still fails closed if that ever changes.
    assert limits.is_records_relation("records_new")
    assert not limits.is_records_relation("entity_links")
    assert not limits.is_records_relation("entity_master")


def test_relation_matching_is_case_folded():
    """Postgres emits the bare lowercase relation name in a plan node, but a
    quoted mixed-case relation would arrive as written."""
    assert limits.is_records_relation("Records_Legacy")
    assert limits.is_records_relation("RECORDS_PARTITIONED_P20260301")
    assert not limits.is_records_relation("Entity_Links")


async def test_every_documented_access_path_plans_below_the_ceiling(fixture_pool):
    ceiling = limits.max_plan_cost()
    for name, sql in ACCESS_PATHS.items():
        cost = await _plan_cost(fixture_pool, sql)
        assert cost < ceiling, f"{name} would be refused at cost {cost}"


async def test_a_runaway_plan_sits_above_the_ceiling(fixture_pool):
    assert await _plan_cost(fixture_pool, RUNAWAY) > limits.max_plan_cost()
