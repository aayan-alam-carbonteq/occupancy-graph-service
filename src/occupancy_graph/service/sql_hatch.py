"""Stages 3 and 4 of the hatch: the EXPLAIN cost gate and bounded execution.

Stage 1 (sql_guard.parse) is the write control. These two stages are the COST
control: they encode "this corpus only answers indexed queries" without
enumerating them, and hand the planner's own estimate back to the agent so it
can adapt rather than guess.
"""
from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import asyncpg

from occupancy_graph.service.limits import is_records_relation
from occupancy_graph.service.sql_guard import SqlRefused
from occupancy_graph.source.pool import PartnerPool

# Anything exposing `acquire()` as an async context manager yielding an asyncpg
# connection. Production passes a PartnerPool; tests/conftest.py's `fixture_pool`
# is a RAW asyncpg.Pool, whose own `acquire()` is also an async context manager.
# Both satisfy every use below, and the stage-3 tests genuinely drive the raw
# pool, so annotating these functions `PartnerPool` alone would have been a
# documented falsehood rather than a type. Runtime behaviour is unaffected --
# this is a widening of the annotation to match what is already passed.
ConnectionSource = PartnerPool | asyncpg.Pool

# Pinned verbatim by Contract C. It names the access paths that are actually
# fast, so a refusal teaches the shape of what is servable instead of just
# saying no.
HINT = (
    "No index supports this predicate. Indexed paths: zip; "
    "(last_name, zip, house_number); (upper(state), upper(city)); ssn; phone; email."
)


async def explain_plan(pool: ConnectionSource, wrapped: str) -> dict[str, Any]:
    """EXPLAIN (FORMAT JSON) -- never ANALYZE, so the query does not run.

    A planning error (unknown table, unknown column, syntax) becomes a stage-3
    refusal carrying Postgres's own message: that is the most useful thing the
    agent can be told, and it costs nothing to relay.

    NOT EVERY error here is strictly a *planning* error. The planner constant-
    folds immutable expressions over constant arguments, so `SELECT 1/0` raises
    "division by zero" from EXPLAIN itself -- the expression was evaluated, but
    at plan time and without touching a table. That is still a refusal before
    execution, which is what this stage promises; it is not evidence that the
    query ran. See test_explain_never_executes_the_query for the shape that
    separates the two.
    """
    try:
        async with pool.acquire() as conn:
            raw = await conn.fetchval(f"EXPLAIN (FORMAT JSON, COSTS TRUE) {wrapped}")
    except asyncpg.PostgresError as exc:
        raise SqlRefused("explain", str(exc), HINT) from exc
    plans = json.loads(raw) if isinstance(raw, str) else raw
    return plans[0]["Plan"]


def _walk(node: dict[str, Any]) -> Iterator[dict[str, Any]]:
    yield node
    for child in node.get("Plans") or ():
        yield from _walk(child)


def check_plan(
    plan: dict[str, Any], *, max_plan_cost: float, max_records_seqscan_cost: float
) -> float:
    """Refuse an unservable plan. Returns the root total cost when it passes.

    The seq-scan rule fires FIRST because its refusal is the more actionable
    one: "Seq Scan on records_legacy" tells the agent which predicate was
    unindexed, where a bare total cost does not.

    THE STARTUP COST IS READ FROM THE NODE, not printed as a constant 0.00. A
    plain Seq Scan does start at 0.00, which is why a constant looked safe, but
    it is not an invariant: a hashed SubPlan in the predicate is charged to the
    scan's startup cost, so

        SELECT record_id FROM public.records_legacy
        WHERE employer NOT IN (SELECT canonical_first_name FROM silver.entity_master)

    plans as `Seq Scan on records_legacy (cost=1.05..2.10)` against this repo's
    own fixture. This string is a message an LLM agent READS AND REASONS ABOUT,
    so quoting a startup cost the plan does not have would be teaching it
    something false about its own query. Pinned by
    test_the_refusal_quotes_the_planners_own_startup_cost_not_a_constant.
    """
    for node in _walk(plan):
        if node.get("Node Type") != "Seq Scan":
            continue
        relation = str(node.get("Relation Name") or "")
        if not is_records_relation(relation):
            continue
        cost = float(node.get("Total Cost") or 0.0)
        if cost > max_records_seqscan_cost:
            startup = float(node.get("Startup Cost") or 0.0)
            raise SqlRefused(
                "explain",
                f"Seq Scan on {relation} (cost={startup:.2f}..{cost:.2f})",
                HINT,
            )

    total = float(plan.get("Total Cost") or 0.0)
    if total > max_plan_cost:
        raise SqlRefused(
            "explain",
            f"estimated total cost {total:.2f} exceeds the ceiling {max_plan_cost:.2f}",
            HINT,
        )
    return total
