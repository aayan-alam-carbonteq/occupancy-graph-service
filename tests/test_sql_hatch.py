"""Stage 3: EXPLAIN (never ANALYZE) against the calibrated ceilings.

The fixture tables hold ~20 rows, so Postgres correctly seq-scans all of them
and every real query plans cheaply. The refusal paths are therefore exercised by
INJECTING a low ceiling -- which is how you test a threshold -- plus one query
(a four-way generate_series cross join) whose cost exceeds the production
ceiling on any machine. docs/explain-cost-calibration.md carries the derivation.
"""
from __future__ import annotations

import ast
from pathlib import Path

import asyncpg
import pytest

from occupancy_graph.service import limits
from occupancy_graph.service.sql_guard import SqlRefused, parse, wrap_with_limit
from occupancy_graph.service.sql_hatch import _walk, check_plan, explain_plan, run_query

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
            "SELECT record_id FROM public.records_new WHERE occupation = 'Manager'",
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


# --- Stage 4 and the endpoint. The four stages compose here; the adversarial
# --- cases from test_sql_guard.py must be refused through the HTTP surface too.


async def test_a_valid_query_returns_columns_rows_and_the_plan_cost(client):
    response = await client.post(
        "/v1/sql",
        json={"query": "SELECT record_id, address FROM public.records_legacy WHERE zip = '40505' "
                       "AND address ILIKE '123 MAIN%' ORDER BY record_id"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["columns"] == ["record_id", "address"]
    assert body["rows"][0] == [1001, "123 MAIN ST"]
    assert body["row_count"] == len(body["rows"])
    assert body["truncated"] is False
    assert body["plan_cost"] > 0.0
    assert body["duration_ms"] >= 0


async def test_columns_are_reported_even_for_an_empty_result(client):
    body = (await client.post(
        "/v1/sql",
        json={"query": "SELECT record_id FROM public.records_legacy WHERE zip = '00000'"},
    )).json()
    assert body["columns"] == ["record_id"]
    assert body["rows"] == []
    assert body["row_count"] == 0


async def test_the_row_cap_truncates_and_says_so(client):
    body = (await client.post(
        "/v1/sql",
        json={"query": "SELECT record_id FROM public.records_new", "max_rows": 2},
    )).json()
    assert body["row_count"] == 2
    assert body["truncated"] is True


async def test_a_result_exactly_at_the_cap_is_not_reported_as_truncated(client):
    """Why stage 2 is asked for cap + 1 rows rather than cap.

    Fetching exactly `cap` rows makes "there were precisely cap rows" and "there
    were more and we cut them" indistinguishable, so the boundary would always
    have to report truncated=true -- telling the agent to paginate a complete
    result. records_legacy holds exactly 7 rows (4 shape rows + 3 resident-hop
    anchors), so cap=7 is the boundary and cap=6 is genuinely truncated.
    """
    query = "SELECT record_id FROM public.records_legacy ORDER BY record_id"
    exact = (await client.post("/v1/sql", json={"query": query, "max_rows": 7})).json()
    assert exact["row_count"] == 7
    assert exact["truncated"] is False

    short = (await client.post("/v1/sql", json={"query": query, "max_rows": 6})).json()
    assert short["row_count"] == 6
    assert short["truncated"] is True


async def test_non_json_types_survive_the_round_trip(client):
    """The three column types that json.dumps cannot emit on its own, and the
    carry-forward warning they exist to close.

    jsonable() has no asyncpg.Record branch, so a Record handed to it WHOLE falls
    through to str() and renders as the string "<Record a=1>" -- a silently
    useless payload rather than an error. Stage 4 iterates `for value in record`,
    which yields the record's VALUES, so no Record ever reaches it. The assertions
    below are what prove that: a Record rendered whole would produce a single
    string per row, not a datetime, a jsonb string and a float in three columns.
    """
    body = (await client.post(
        "/v1/sql",
        json={"query": "SELECT imported_at, raw_data, identity_confidence "
                       "FROM public.records_new, silver.entity_master "
                       "WHERE record_id = 2001 AND hal_id = 'HAL0001'"},
    )).json()
    row = body["rows"][0]
    # timestamptz -> ISO 8601 string.
    assert row[0].startswith("2026-02-10")
    # numeric -> Decimal("40.50") -> float. Not a string, and not "<Record ...>".
    assert row[2] == 40.5
    assert isinstance(row[2], float)
    # jsonb: asyncpg hands it back as text, which survives verbatim.
    assert isinstance(row[1], str)
    assert "loan_amount" in row[1]
    # Three columns, three values -- the shape a whole-Record render would lose.
    assert len(row) == len(body["columns"]) == 3
    assert not any(str(value).startswith("<Record") for value in row)


async def test_the_parse_guard_refuses_through_the_endpoint(client):
    for query in (
        "SELECT 1; DROP TABLE public.records_legacy",
        "BEGIN READ WRITE",
        "WITH w AS (INSERT INTO public.records_legacy (record_id) VALUES (1) RETURNING record_id) "
        "SELECT * FROM w",
        "SET default_transaction_read_only = off",
        "COPY public.records_legacy TO '/tmp/x.csv'",
        "DO $$ BEGIN PERFORM 1; END $$",
        "CALL nothing()",
        "SELECT pg_read_file('/etc/passwd')",
    ):
        response = await client.post("/v1/sql", json={"query": query})
        assert response.status_code == 422, query
        body = response.json()
        assert body["refused"] is True
        assert body["stage"] == "parse"
        assert body["reason"]


async def test_the_explain_gate_refuses_through_the_endpoint(client, monkeypatch):
    """Also pins that the ceiling is read AT CALL TIME, not at import.

    monkeypatch.setenv only works here because limits.max_records_seqscan_cost()
    consults os.environ on every call and run_query calls it per request. And "0"
    has to be ACCEPTED: the ceilings were hardened to require >= 0 precisely so
    that 0 stays legal -- it is the strictest setting (every seq scan costs more
    than nothing), which is what makes this refusal forceable on a 20-row fixture
    whose scans would otherwise cost ~1.
    """
    monkeypatch.setenv("SQL_HATCH_MAX_RECORDS_SEQSCAN_COST", "0")
    assert limits.max_records_seqscan_cost() == 0.0

    response = await client.post(
        "/v1/sql",
        json={"query": "SELECT record_id FROM public.records_legacy WHERE employer = 'ACME'"},
    )
    assert response.status_code == 422
    body = response.json()
    assert body["refused"] is True
    assert body["stage"] == "explain"
    assert body["reason"].startswith("Seq Scan on records_legacy")
    assert "Indexed paths" in body["hint"]


async def test_the_ceiling_reverts_when_the_environment_does(client):
    """The other half of the call-time claim: with the env var gone, the SAME
    query the previous test saw refused is served. A ceiling captured at import
    would leave that test's 0 in place for the rest of the session.

    It returns no rows -- `employer` is only populated on records_new --
    and that is beside the point: the assertion is that the plan was ACCEPTED, so
    the query reached execution and reported its columns.
    """
    response = await client.post(
        "/v1/sql",
        json={"query": "SELECT record_id FROM public.records_legacy WHERE employer = 'ACME'"},
    )
    assert response.status_code == 200
    body = response.json()
    assert "refused" not in body
    assert body["columns"] == ["record_id"]
    assert body["row_count"] == 0
    assert body["plan_cost"] > 0.0


async def test_a_missing_query_field_is_a_400(client):
    assert (await client.post("/v1/sql", json={})).status_code == 400
    assert (await client.post("/v1/sql", content=b"not json")).status_code == 400


async def test_max_rows_refuses_garbage_and_clamps_out_of_range(client):
    """The 500-bug that was already fixed once, on /v1/resolve's `rows`.

    `int(max_rows)` inline in the run_query call sits outside every try, so
    {"max_rows": "abc"} raised ValueError out of the handler and surfaced as a
    500. The coercion now happens in the handler's error path and returns a 400
    NAMING the parameter.

    The two failure modes are deliberately different, and match `rows` exactly so
    the two endpoints cannot disagree: a value that is not a number is a client
    bug and is REFUSED; an out-of-range integer is a coherent preference and is
    CLAMPED into [1, SQL_HATCH_MAX_ROWS].
    """
    for bad in ("abc", "5; DROP TABLE t", "1e3", [], {}, "", "  "):
        response = await client.post(
            "/v1/sql", json={"query": "SELECT 1", "max_rows": bad}
        )
        assert response.status_code == 400, bad
        assert response.json()["error"] == "max_rows must be an integer", bad

    query = "SELECT record_id FROM public.records_legacy ORDER BY record_id"
    for value in (0, -5):
        body = (await client.post(
            "/v1/sql", json={"query": query, "max_rows": value}
        )).json()
        assert body["row_count"] == 1, value
        assert body["truncated"] is True, value

    # Above the service ceiling: clamped down to it, never honoured.
    body = (await client.post(
        "/v1/sql", json={"query": query, "max_rows": 10 ** 9}
    )).json()
    # 4 original legacy rows + 3 resident-hop anchor rows (seed.sql).
    assert body["row_count"] == 7
    assert body["truncated"] is False


async def test_a_refused_write_did_not_happen(client, service_pool):
    """The write guard is only real if nothing landed.

    THE PINNED LITERAL IS GONE. This was written `after == before == 4`, and 4
    was already stale: later tasks added two silver.entity_links rows (the
    owner-elsewhere link for HAL0001, and the column-shifted tax link for
    HAL0002), so the table holds 6. Correcting the number was not worth it. The
    load-bearing assertion is `after == before` -- the count did not MOVE -- and
    the only thing the constant added on top was a guard against the vacuous
    case where the table was empty all along, which `> 0` covers without coupling
    this test to the seed. A seed-coupled literal has to be edited by every
    future task that touches fixtures, and each of those edits is an invitation
    to make the test agree with whatever the run produced.
    """
    async with service_pool.acquire() as conn:
        before = await conn.fetchval("SELECT count(*) FROM silver.entity_links")
    await client.post(
        "/v1/sql",
        json={"query": "WITH w AS (DELETE FROM silver.entity_links RETURNING hal_id) "
                       "SELECT * FROM w"},
    )
    await client.post("/v1/sql", json={"query": "SELECT 1; DELETE FROM silver.entity_links"})
    async with service_pool.acquire() as conn:
        after = await conn.fetchval("SELECT count(*) FROM silver.entity_links")
    assert after == before > 0


async def test_execution_runs_in_a_read_only_transaction(client):
    """Belt and braces behind the parse guard: even if a write ever slipped
    through stage 1, the executing transaction is explicitly READ ONLY.

    THIS TEST NO LONGER DISTINGUISHES WHAT IT CLAIMS, and says so rather than
    pretending. source/pool.py used to apply `default_transaction_read_only` via
    asyncpg's `init=`, which `RESET ALL` wiped on release, so it held for exactly
    one acquire; it now travels in the startup packet and holds for every
    connection's whole life. `transaction_read_only` therefore reads `on` here
    whether or not stage 4 opens a read-only transaction, so this asserts the
    composed surface's behaviour and nothing about the qualifier.

    test_stage_four_opens_the_transaction_read_only_itself is the actual pin.
    """
    body = (await client.post(
        "/v1/sql", json={"query": "SELECT current_setting('transaction_read_only') AS ro"}
    )).json()
    assert body["rows"][0][0] == "on"


async def test_stage_four_opens_the_transaction_read_only_itself(fixture_db):
    """The real pin for `conn.transaction(readonly=True)`.

    Driven against a pool with NO `default_transaction_read_only` server setting,
    so the session default is `off` and the ONLY thing that can make the
    executing transaction read-only is stage 4's own qualifier. Delete the
    `readonly=True` and this reads "off".

    That case is not hypothetical: run_query takes the pool as an argument and
    serves whatever it is handed, so the qualifier is what holds when the pool's
    configuration is not what this repo's PartnerPool provides.
    """
    pool = await asyncpg.create_pool(fixture_db, min_size=1, max_size=1)
    try:
        # Negative control: this pool really is writable by default, otherwise
        # the assertion below would prove nothing.
        async with pool.acquire() as conn:
            assert await conn.fetchval(
                "SELECT current_setting('transaction_read_only')"
            ) == "off"
            async with conn.transaction():
                assert await conn.fetchval(
                    "SELECT current_setting('transaction_read_only')"
                ) == "off"
            # ...and a write inside a transaction opened the way stage 4 opens
            # one is rejected by the server, which is what "read only" buys.
            async with conn.transaction(readonly=True):
                with pytest.raises(asyncpg.exceptions.ReadOnlySQLTransactionError):
                    await conn.execute("CREATE TEMP TABLE hatch_probe (a int)")

        result = await run_query(
            pool, "SELECT current_setting('transaction_read_only') AS ro"
        )
        assert result.rows[0][0] == "on"
    finally:
        await pool.close()


async def test_the_hatchs_own_statement_timeout_is_the_one_in_force(client, monkeypatch):
    """Which statement_timeout wins inside stage 4's transaction.

    Two are in play: the connection's, set in the STARTUP PACKET by
    PartnerPool.create (the conftest service_pool uses 10 000 ms -> "10s"), and
    the hatch's own, applied with `SET LOCAL` inside the transaction. SET LOCAL
    overrides the session value for the remainder of the transaction and reverts
    at commit, so the hatch's value is the one in force while agent SQL runs --
    which is the requirement: the hatch's budget for LLM-authored SQL is its own
    knob, not the pool's typed-query budget.

    7531 ms is chosen because Postgres renders it as "7531ms" rather than folding
    it to a whole number of seconds, so the value is unambiguous, and because it
    can be confused with neither the pool's "10s" nor the hatch default "20s".
    """
    monkeypatch.setenv("SQL_HATCH_TIMEOUT_MS", "7531")
    body = (await client.post(
        "/v1/sql", json={"query": "SELECT current_setting('statement_timeout') AS t"}
    )).json()
    assert body["rows"][0][0] == "7531ms"
    assert body["rows"][0][0] != "10s"  # the pool's startup-packet value lost


async def test_a_runaway_query_is_refused_before_execution(client):
    response = await client.post("/v1/sql", json={"query": RUNAWAY})
    assert response.status_code == 422
    assert response.json()["stage"] == "explain"


# --- The whole adversarial corpus, driven through HTTP. ---------------------
#
# The endpoint is the real attack surface; test_sql_guard.py exercises parse()
# in isolation. Rather than copying a handful of attacks across, the corpus is
# EXTRACTED from that file's own source, so an attack added there is
# automatically driven through HTTP here and cannot be covered in one place only.

_GUARD_TEST_FILE = Path(__file__).parent / "test_sql_guard.py"

# What each parametrize argname holds. Naming them all -- including the ones with
# nothing to contribute -- is what lets the extractor FAIL LOUDLY on an argname
# it has never seen, instead of silently dropping a new family of attacks and
# reporting a corpus that quietly shrank.
_SQL_ARGNAMES = frozenset({"query", "spelling"})
_FUNCTION_ARGNAMES = frozenset({"listed", "sibling"})   # bare names -> SELECT <name>(1)
_NON_SQL_ARGNAMES = frozenset({"node_type"})            # expected refusal substrings


def _literal_strings(node: ast.AST):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        yield node.value
    elif isinstance(node, (ast.Tuple, ast.List)):
        for item in node.elts:
            yield from _literal_strings(item)


def _guard_corpus() -> list[str]:
    """Every SQL string test_sql_guard.py hands to the parse guard.

    Read from that file's AST rather than by importing it, because the queries
    live in `refuse(...)` / `parse(...)` call arguments and in parametrize lists,
    not in any value a module import would expose.

    The parametrize ARGNAMES are what keep this honest: they say which tuple
    position holds SQL, so a node type name ("InsertStmt") or a bare function
    name ("pg_advisory_lock") is never mistaken for a query. Bare function names
    are reassembled into the call shape the guard test uses, `SELECT <name>(1)`.
    """
    tree = ast.parse(_GUARD_TEST_FILE.read_text(encoding="utf-8"))
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if (isinstance(node.func, ast.Name)
                and node.func.id in ("refuse", "parse") and node.args):
            found.extend(_literal_strings(node.args[0]))
        if (isinstance(node.func, ast.Attribute)
                and node.func.attr == "parametrize" and len(node.args) == 2):
            names = [name.strip() for name in ast.literal_eval(node.args[0]).split(",")]
            unknown = set(names) - _SQL_ARGNAMES - _FUNCTION_ARGNAMES - _NON_SQL_ARGNAMES
            assert not unknown, (
                f"unrecognised parametrize argname(s) {sorted(unknown)} in "
                f"{_GUARD_TEST_FILE.name}. Classify them above -- silently "
                "skipping them would shrink this corpus without failing."
            )
            for case in node.args[1].elts:
                values = list(_literal_strings(case))
                if len(values) != len(names):
                    continue
                for name, value in zip(names, values):
                    if name in _SQL_ARGNAMES:
                        found.append(value)
                    elif name in _FUNCTION_ARGNAMES:
                        found.append(f"SELECT {value}(1)")
    return sorted(set(found))


_COUNTED_TABLES = (
    "silver.entity_links",
    "silver.entity_master",
    "public.records_legacy",
    "public.records_new",
)


async def test_every_adversarial_query_in_the_guard_corpus_is_refused_through_http(
    client, service_pool
):
    """All 160 of them, not the eight the plan listed.

    Two assertions, and the second matters as much as the first: every query the
    guard refuses must be refused THROUGH HTTP carrying the guard's own stage and
    reason -- proving the endpoint relays the verdict rather than inventing a
    generic one -- and every query the guard ACCEPTS must not be refused at stage
    parse, which is what would catch an endpoint that had quietly become
    stricter than the control it wraps. (Accepted queries may still be refused at
    stage explain: many of them read a table named `t` that does not exist.)
    """
    refusals: dict[str, str] = {}
    blank: list[str] = []
    accepted: list[str] = []
    for query in _guard_corpus():
        try:
            parse(query)
        except SqlRefused as refusal:
            # A query that is blank after stripping is a malformed REQUEST, not
            # an attack, and the handler answers 400 before the guard is
            # consulted. parse() refuses it too ("empty query"), so the two agree
            # it does not run; 400 is simply the more accurate HTTP verdict for a
            # field that is effectively absent.
            if query.strip():
                refusals[query] = refusal.reason
            else:
                blank.append(query)
        else:
            accepted.append(query)

    # A floor, not an equality: the corpus is meant to grow. It fails loudly if
    # the extractor ever stops finding the attacks (a rename of `refuse`, a new
    # parametrize shape), which would otherwise turn this test green and empty.
    assert len(refusals) >= 150, f"corpus collapsed to {len(refusals)} attacks"
    assert len(accepted) >= 40, f"only {len(accepted)} accepted queries found"

    async with service_pool.acquire() as conn:
        before = {
            table: await conn.fetchval(f"SELECT count(*) FROM {table}")
            for table in _COUNTED_TABLES
        }

    for query, reason in refusals.items():
        response = await client.post("/v1/sql", json={"query": query})
        assert response.status_code == 422, query
        body = response.json()
        assert body["refused"] is True, query
        assert body["stage"] == "parse", query
        # The guard's OWN message, relayed verbatim.
        assert body["reason"] == reason, query
        assert body["hint"] == "", query

    for query in blank:
        response = await client.post("/v1/sql", json={"query": query})
        assert response.status_code == 400, query
        assert response.json()["error"] == "query is required", query

    for query in accepted:
        body = (await client.post("/v1/sql", json={"query": query})).json()
        assert body.get("stage") != "parse", query

    async with service_pool.acquire() as conn:
        after = {
            table: await conn.fetchval(f"SELECT count(*) FROM {table}")
            for table in _COUNTED_TABLES
        }
    assert after == before
    assert all(count > 0 for count in before.values()), before
