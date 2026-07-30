"""Stage 3: EXPLAIN (never ANALYZE) against the calibrated ceilings.

The fixture tables hold ~20 rows, so Postgres correctly seq-scans all of them
and every real query plans cheaply. The refusal paths are therefore exercised by
INJECTING a low ceiling -- which is how you test a threshold -- plus one query
(a four-way generate_series cross join) whose cost exceeds the production
ceiling on any machine. docs/explain-cost-calibration.md carries the derivation.
"""
from __future__ import annotations

import pytest

from occupancy_graph.service import limits
from occupancy_graph.service.sql_guard import SqlRefused, wrap_with_limit
from occupancy_graph.service.sql_hatch import _walk, check_plan, explain_plan

INDEXED = """
    SELECT * FROM public.records_legacy
    WHERE zip = '40505' AND address ILIKE '123 MAIN%'
"""
RUNAWAY = """
    SELECT count(*)
    FROM generate_series(1, 1000000) a, generate_series(1, 1000000) b,
         generate_series(1, 1000000) c, generate_series(1, 1000000) d
"""
UNINDEXED = "SELECT record_id FROM public.records_legacy WHERE employer = 'ACME'"

# A hashed SubPlan in the predicate is charged to the SCAN's startup cost, which
# is the counter-example to "a Seq Scan always starts at 0.00".
NOT_IN = """
    SELECT record_id FROM public.records_legacy
    WHERE employer NOT IN (SELECT canonical_first_name FROM silver.entity_master)
"""


async def test_a_documented_access_path_passes_the_gate(fixture_pool):
    plan = await explain_plan(fixture_pool, wrap_with_limit(INDEXED, cap=200))
    cost = check_plan(
        plan,
        max_plan_cost=limits.max_plan_cost(),
        max_records_seqscan_cost=limits.max_records_seqscan_cost(),
    )
    assert 0.0 < cost < limits.max_plan_cost()


async def test_a_runaway_plan_is_refused_at_the_production_ceiling(fixture_pool):
    plan = await explain_plan(fixture_pool, wrap_with_limit(RUNAWAY, cap=500))
    with pytest.raises(SqlRefused) as caught:
        check_plan(
            plan,
            max_plan_cost=limits.max_plan_cost(),
            max_records_seqscan_cost=limits.max_records_seqscan_cost(),
        )
    assert caught.value.stage == "explain"
    assert "exceeds the ceiling" in caught.value.reason
    assert "Indexed paths" in caught.value.hint


async def test_a_records_table_seq_scan_is_refused_with_the_planners_own_reason(fixture_pool):
    plan = await explain_plan(fixture_pool, wrap_with_limit(UNINDEXED, cap=500))
    with pytest.raises(SqlRefused) as caught:
        check_plan(plan, max_plan_cost=1e12, max_records_seqscan_cost=0.0)
    assert caught.value.stage == "explain"
    assert caught.value.reason.startswith("Seq Scan on records_legacy (cost=0.00..")
    assert "No index supports this predicate" in caught.value.hint

    # The documented PRECEDENCE, pinned rather than left to the reading order of
    # the source: when BOTH ceilings would refuse this plan, the seq-scan rule is
    # the one that speaks, because naming the relation whose predicate was
    # unindexed is more actionable than a bare total cost.
    with pytest.raises(SqlRefused) as both:
        check_plan(plan, max_plan_cost=0.0, max_records_seqscan_cost=0.0)
    assert both.value.reason.startswith("Seq Scan on records_legacy")


async def test_the_refusal_quotes_the_planners_own_startup_cost_not_a_constant(fixture_pool):
    """A Seq Scan starts at 0.00 USUALLY, not ALWAYS -- and the difference is
    visible to the agent.

    The refusal string was originally written `(cost=0.00..{total})`, hardcoding
    the startup cost. For the plain unindexed scan in the test above that is
    accurate, which is exactly what makes the constant dangerous: it is right
    until it is silently wrong. `NOT IN (subquery)` builds a hashed SubPlan whose
    cost is charged to the scan's STARTUP, so the same fixture plans

        Seq Scan on records_legacy  (cost=1.05..2.10 rows=2 width=8)

    An agent reading `cost=0.00..2.10` here would be told the scan starts free
    when the plan says it does not. The gate still keys on Total Cost; only the
    message changed.
    """
    plan = await explain_plan(fixture_pool, wrap_with_limit(NOT_IN, cap=500))
    scan = next(
        node
        for node in _walk(plan)
        if node.get("Node Type") == "Seq Scan"
        and node.get("Relation Name") == "records_legacy"
    )
    startup, total = float(scan["Startup Cost"]), float(scan["Total Cost"])
    assert startup > 0.0, "this shape must produce a non-zero startup cost to prove anything"

    with pytest.raises(SqlRefused) as caught:
        check_plan(plan, max_plan_cost=1e12, max_records_seqscan_cost=0.0)
    assert caught.value.reason == (
        f"Seq Scan on records_legacy (cost={startup:.2f}..{total:.2f})"
    )
    assert not caught.value.reason.startswith("Seq Scan on records_legacy (cost=0.00..")


async def test_a_seq_scan_on_a_non_records_table_is_not_refused(fixture_pool):
    plan = await explain_plan(
        fixture_pool, wrap_with_limit("SELECT * FROM silver.entity_master", cap=10)
    )
    assert check_plan(plan, max_plan_cost=1e12, max_records_seqscan_cost=0.0) > 0.0


async def test_the_refusal_names_a_partition_child_by_its_own_relation_name(fixture_pool):
    plan = await explain_plan(
        fixture_pool,
        wrap_with_limit(
            "SELECT record_id FROM public.records_partitioned WHERE occupation = 'Manager'",
            cap=500,
        ),
    )
    with pytest.raises(SqlRefused) as caught:
        check_plan(plan, max_plan_cost=1e12, max_records_seqscan_cost=0.0)
    assert "records_partitioned_p" in caught.value.reason


async def test_explain_never_executes_the_query(fixture_pool):
    """A statement that would fail at runtime but plans fine proves EXPLAIN
    is not ANALYZE: division by zero is a runtime error, not a planning one.

    The DIVIDEND must be a column. The obvious vehicle, `SELECT 1/0`, does not
    work: PostgreSQL's planner constant-folds immutable functions over constant
    arguments, so int4div(1, 0) is evaluated inside eval_const_expressions and
    EXPLAIN ITSELF raises. `record_id / 0` takes a Var, is therefore not
    foldable, and survives planning untouched. Both halves are asserted so the
    distinction is pinned rather than remembered.
    """
    plan = await explain_plan(
        fixture_pool,
        wrap_with_limit("SELECT record_id / 0 AS boom FROM public.records_legacy", cap=1),
    )
    assert float(plan["Total Cost"]) >= 0.0

    # The counterpart. This is still a refusal BEFORE execution -- no table was
    # read -- but it is why the assertion above cannot use `SELECT 1/0`.
    with pytest.raises(SqlRefused) as caught:
        await explain_plan(fixture_pool, wrap_with_limit("SELECT 1/0 AS boom", cap=1))
    assert caught.value.stage == "explain"
    assert "division by zero" in caught.value.reason


async def test_a_planning_error_is_a_refusal_carrying_the_planners_message(fixture_pool):
    with pytest.raises(SqlRefused) as caught:
        await explain_plan(fixture_pool, wrap_with_limit("SELECT * FROM no_such_table", cap=1))
    assert caught.value.stage == "explain"
    assert "no_such_table" in caught.value.reason
