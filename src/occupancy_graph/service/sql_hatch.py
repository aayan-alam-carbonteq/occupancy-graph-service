"""Stages 3 and 4 of the hatch: the EXPLAIN cost gate and bounded execution.

Stage 1 (sql_guard.parse) is the write control. These two stages are the COST
control: they encode "this corpus only answers indexed queries" without
enumerating them, and hand the planner's own estimate back to the agent so it
can adapt rather than guess.
"""
from __future__ import annotations

import json
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import asyncpg

from occupancy_graph.service import limits
from occupancy_graph.service.jsonio import jsonable
from occupancy_graph.service.limits import is_records_relation
from occupancy_graph.service.sql_guard import SqlRefused, parse, wrap_with_limit
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


@dataclass(frozen=True)
class SqlResult:
    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    truncated: bool
    plan_cost: float
    duration_ms: int

    def as_payload(self) -> dict[str, Any]:
        return {
            "columns": self.columns,
            "rows": self.rows,
            "row_count": self.row_count,
            "truncated": self.truncated,
            "plan_cost": self.plan_cost,
            "duration_ms": self.duration_ms,
        }


def _cap_for(max_rows: int | None) -> int:
    """The row cap for one hatch call.

    `None` means "no preference" and takes the service default. Anything else is
    CLAMPED into [1, max_sql_rows], never refused -- an out-of-range integer is a
    coherent preference ("as few as possible", "as many as you have"), and this
    matches POST /v1/resolve's `rows`, which clamps with `max(1, rows)`. The two
    endpoints must not disagree about what an out-of-range number means.

    A value that is not a number at all is a different failure and is NOT handled
    here: `int()` raises, and the HTTP handler turns that into a 400 naming the
    parameter. That split is deliberate -- the identical bug in /v1/resolve's
    `rows` (an `int()` outside every try) surfaced as a 500.
    """
    if max_rows is None:
        return limits.max_sql_rows()
    return max(1, min(int(max_rows), limits.max_sql_rows()))


async def run_query(
    pool: ConnectionSource, query: str, *, max_rows: int | None = None
) -> SqlResult:
    """The four stages, in order. Any of them may raise SqlRefused.

    1. parse   -- exactly one SELECT (the write guard)
    2. wrap    -- bound the result set
    3. explain -- refuse an unservable plan
    4. execute -- READ ONLY transaction, statement timeout, row cap
    """
    cleaned = parse(query)

    cap = _cap_for(max_rows)
    # cap + 1 so a result exactly `cap` long is distinguishable from a truncated
    # one, rather than always reporting truncated=true at the boundary.
    wrapped = wrap_with_limit(cleaned, cap=cap + 1)

    plan = await explain_plan(pool, wrapped)
    plan_cost = check_plan(
        plan,
        max_plan_cost=limits.max_plan_cost(),
        max_records_seqscan_cost=limits.max_records_seqscan_cost(),
    )

    started = time.monotonic()
    try:
        async with pool.acquire() as conn:
            # Explicit READ ONLY behind the parse guard. The guard is the
            # control; this is what remains true even if the guard were wrong.
            #
            # It is NOT redundant with the pool's `default_transaction_read_only`
            # server setting, even though that setting alone would make
            # `transaction_read_only` read `on` here. The pool is an argument:
            # run_query serves whatever pool it is handed, and this qualifier is
            # the only thing that makes the transaction read-only when the pool
            # has no such default. Pinned by
            # test_stage_four_opens_the_transaction_read_only_itself, which
            # drives exactly that pool.
            async with conn.transaction(readonly=True):
                # SET LOCAL, so it is scoped to this transaction and reverts on
                # commit. It OVERRIDES the connection's startup-packet
                # statement_timeout for the duration, which is the point: the
                # hatch's budget for agent-authored SQL is its own knob
                # (SQL_HATCH_TIMEOUT_MS), not the pool's typed-query budget.
                # `SET LOCAL` requires a transaction, and there is one.
                #
                # LOCAL is NOT observable today and is still the right form. A
                # plain session `SET` would behave identically here -- nothing
                # else runs on this connection before it is released, and asyncpg
                # issues `RESET ALL` on every release (Connection.get_reset_query),
                # which restores the startup-packet value. That equivalence is
                # exactly the trap: it makes the session form's correctness
                # depend on asyncpg's release behaviour, and source/pool.py's
                # docstring records the Critical this codebase already shipped by
                # depending on that same mechanism from the other direction --
                # `RESET ALL` silently wiped the safety settings an `init=`
                # callback had SET, leaving the corpus unbounded from the second
                # acquire onward. LOCAL reverts by its own definition instead.
                # A mutation swapping LOCAL out therefore SURVIVES the suite;
                # it is an equivalent mutant, recorded here rather than papered
                # over with a test that only asserts asyncpg's internals.
                await conn.execute(f"SET LOCAL statement_timeout = {limits.sql_timeout_ms()}")
                statement = await conn.prepare(wrapped)
                # prepare() gives the column list even when zero rows come back.
                columns = [attr.name for attr in statement.get_attributes()]
                fetched = await statement.fetch()
    except asyncpg.QueryCanceledError as exc:
        raise SqlRefused(
            "explain",
            f"query exceeded the {limits.sql_timeout_ms()} ms statement timeout: {exc}",
            HINT,
        ) from exc
    except asyncpg.PostgresError as exc:
        raise SqlRefused("explain", str(exc), HINT) from exc
    duration_ms = int(round((time.monotonic() - started) * 1000))

    truncated = len(fetched) > cap
    window = fetched[:cap]
    return SqlResult(
        columns=columns,
        # `for value in record` iterates an asyncpg Record's VALUES, so jsonable()
        # never receives a Record whole -- which matters because it has no Record
        # branch and would fall through to str(), emitting the useless
        # "<Record a=1>". Pinned by test_non_json_types_survive_the_round_trip.
        rows=[[jsonable(value) for value in record] for record in window],
        row_count=len(window),
        truncated=truncated,
        plan_cost=plan_cost,
        duration_ms=duration_ms,
    )
