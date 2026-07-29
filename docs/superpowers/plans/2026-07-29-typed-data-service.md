# X-016 Typed Data Service + SQL Hatch — Implementation Plan (`occupancy-graph-service`)

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development`
> (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace this service's GraphQL transport with six typed HTTP operations plus a guarded exploratory SQL hatch, served over the already-built `source/` Postgres layer.

**Architecture:** A Starlette ASGI app on the existing `uvicorn`. Address-scoped operations read the `BundleCache` that Tasks 1–16 already built; person operations read the bundle for `addr:` ids and `silver.entity_links`/`entity_master` for `hal:` ids; the SQL hatch runs a four-stage guard (parse → LIMIT-wrap → EXPLAIN cost gate → timeout-bounded execute) whose **parse stage is the primary write guard**. `graphql/` and the orphaned SQLite `graphdb/` are deleted.

**Tech Stack:** Python 3.14.5 (uv-managed venv, **no `pip` binary**), `starlette` (promoted from a strawberry transitive dep to a direct one), `uvicorn`, `asyncpg`, `httpx` (test client), `pytest` + `pytest-asyncio` (`asyncio_mode=auto`).

**Spec:** `docs/superpowers/specs/2026-07-29-typed-data-service-design.md`
**Umbrella:** `docs/superpowers/plans/2026-07-29-typed-data-service.md` (Contracts B and C are pinned; this plan honours them)

**Branch:** stay on **`feat/postgres-adapter`**. Do not cut a new branch. Do not touch `origin/main`.

---

## Ground truth before you start

| Fact | Value |
|---|---|
| Current test count | **181 passed** (`.venv/bin/python -m pytest -q`) |
| Commits on branch | 34 total, 29 ahead of `origin/main` |
| Working tree | clean |
| Python | `.venv/bin/python` → 3.14.5. **`pip` is not installed.** Install with `VIRTUAL_ENV=.venv uv pip install --python .venv/bin/python <pkg>` |
| Postgres fixture | starts/stops itself via `tests/conftest.py` (`tests/docker-compose.fixture.yml`, port 55432). Docker is running. Containers named `mortgage-compliance-monitoring-graph-*` are **unrelated — leave them alone.** |
| Test style | plain module-level `async def` functions. **Never** `async def` methods on `unittest.TestCase` — `asyncio_mode="auto"` does not wrap them and they silently pass without running. |
| `.pgenv` / `PARTNER_DSN` | **not available to you.** Every live-corpus assertion is `@pytest.mark.live` + `skipif`. |

### Three corrections to the briefing you were given

Verify these yourself with `ls src/occupancy_graph/source/` before Task 7; they change the work:

1. **`src/occupancy_graph/source/search.py` does not exist.** There is no `records_for_hal_id` to "wire up" — the entity-graph access layer has to be **written** (Task 7). The fixture already has `silver.entity_master` and `silver.entity_links` seeded (`tests/fixtures/seed.sql:127-140`), so it is fully testable.
2. **`src/occupancy_graph/graphdb/`** (the old SQLite index builder, 904 lines) still exists and is imported only by `graphql/`. Deleting `graphql/` orphans it entirely. Task 3 removes it as a separate, individually revertible commit.
3. **`tests/test_graphql.py` (13 tests) must also go** — it imports `occupancy_graph.graphql.db`, `.guardrails`, `.schema`. It was not on your delete list but it cannot survive Task 2.

---

## Contract notes — read before Task 9

Contracts B and C are honoured exactly as pinned. Three places need a decision the pinned examples do not settle. All three are **additive**; every pinned key keeps its pinned name, position and meaning.

**Note 1 — operation 6 needs an address.** `GET /v1/source-record/{shape}/{rowid}` returns `rowid: 0` alongside `record_id: "4001"`, so `rowid` is a *positional index*, not the partner id. A positional index is only meaningful inside a bundle, and the path carries no address. **Resolution:** `address_id` is a required **query parameter** — `GET /v1/source-record/tax/0?address_id=1`. The path shape and the response body are exactly as pinned; a missing `address_id` returns 400 naming the parameter.

**Note 2 — records carry `__rowid`.** So the agent can reach operation 6 at all, each record returned by operations 1–3 and by the `addr:` half of operation 4 gets a `__rowid` key (its index within `bundle.rows_by_shape[shape]`). This uses the existing `__`-prefix convention that `project.py` already established for `__norm_*`; the vendor column names are untouched. `hal:`-sourced records get **no** `__rowid` — they do not come from a bundle, and their `record_id` is already in the projected data.

**Note 3 — operation 4 reports a record-fetch timeout.** `silver.entity_links` is indexed both ways, but the `records_legacy`/`records_new` rows it points at are fetched by `record_id`, which the partner's index set does not cover. That lookup runs under `statement_timeout`; on expiry the response carries `"records_timed_out": true` and empty blocks. Returning empty records silently is exactly the failure mode this repo already fixed twice (`TaxScanResult.queried_city`, `tax_timed_out`). **The engine plan must be told about this field.** An index on `records_*(record_id)` is added to the partner asks in Task 22.

---

## Framework decision (required by the brief)

**Chosen: Starlette.** It is already installed at 1.3.1 as a transitive dependency of `strawberry-graphql[asgi]`; removing strawberry leaves it in place, so promoting it to a direct dependency costs **zero new packages**. It is uvicorn's own ecosystem, and it gives exactly the four things needed — path routing with typed converters, `JSONResponse`, exception handlers, and an async lifespan for the pool — in ~2k lines with one dependency (`anyio`, also already installed).

Rejected:
- **FastAPI** — adds pydantic v2 (a compiled Rust extension) to validate request bodies with one and two fields. Its value is schema generation for a large typed surface; there are six operations and their payloads are hand-pinned in Contract B, so generated schemas would be a second source of truth competing with the contract.
- **Hand-rolled ASGI** — smaller on paper, but path-parameter parsing, method dispatch, and error handling would all become our code and our tests, replacing a dependency that is already installed and already exercised in production by the service being deleted.

**Worker model changes.** The old `serve.py` defaulted to `min(4, cpu_count)` uvicorn worker processes because the SQLite resolvers were synchronous and blocked the event loop. Every path in the new service is async I/O against asyncpg. One process, one event loop, **one shared pool and one shared `BundleCache`** is now strictly better: multiple workers would each hold their own bundle cache and re-run the 173 ms–32 s scan per worker. Single-process is the design, not a simplification.

---

### Task 1: Calibrate the EXPLAIN cost ceiling

This is Task 1 because a ceiling picked in the abstract refuses everything or nothing, and every later hatch task depends on the number.

**How the ceiling is derived.** You cannot measure the live corpus — `.pgenv` is not available. So the number is derived from the documented real-corpus measurements and *bracketed* against the fixture:

| Bound | Source | Cost |
|---|---|---|
| Most expensive plan we **must serve** | phase-2 `(upper(state), upper(city))` + prefix, 151 507 rows examined, 613 ms warm / 53 s cold. At ~151 k heap fetches × (`random_page_cost` 4.0 + `cpu_tuple_cost` 0.01 + `cpu_operator_cost` 0.0025 ×3) | ≈ **6.1 × 10⁵** |
| Same path with 3× headroom for a denser city | — | ≈ **1.8 × 10⁶** |
| Cheapest plan we **must refuse** | `Seq Scan on records_legacy`, 6.24 B rows. Contract C's own worked refusal quotes `cost=0.00..184000000.00` | **1.84 × 10⁸** |

**`SQL_HATCH_MAX_PLAN_COST = 5_000_000`** sits 2.8× above the headroomed serve bound and 37× below the observed refuse bound — a wide margin on both sides, and the gap is three orders of magnitude, so the exact placement inside it is not load-bearing.

**Second, lower ceiling for records-table sequential scans.** Contract C §3 says "refuse on a sequential scan over a records table" unconditionally. Implemented literally, that refuses *every* hatch query in this repo's test suite, because the fixture tables hold ~20 rows each and Postgres correctly plans a seq scan for all of them — the hatch would be untestable. So the rule is cost-gated at **`SQL_HATCH_MAX_RECORDS_SEQSCAN_COST = 50_000`** (≈ 40 k pages ≈ 300 MB). The fixture's whole-table scans cost < 25; the smallest real partition is millions of rows. In production this is unconditional in practice, and it stays testable here. **This is a documented refinement of the spec text, not a silent one.**

**Re-tuning later.** `scripts/explain_cost_probe.py` prints the root `Total Cost` for a fixed battery of queries against whatever DSN it is handed. Point it at `PARTNER_DSN` when credentials arrive, read the real numbers, and set the two env vars. No code change.

**Files:**
- Create: `src/occupancy_graph/service/__init__.py`
- Create: `src/occupancy_graph/service/limits.py`
- Create: `scripts/explain_cost_probe.py`
- Create: `docs/explain-cost-calibration.md`
- Test: `tests/test_limits.py`

- [ ] **Step 1: Write the failing test**

`tests/test_limits.py`:

```python
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
```

- [ ] **Step 2: Run the test, verify it fails**

Run: `cd /home/aayan-alam/Work/Helcion/true-occupancy-workspace/occupancy-engine-ts/services/graph && .venv/bin/python -m pytest tests/test_limits.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'occupancy_graph.service'` (collection error).

- [ ] **Step 3: Minimal implementation**

`src/occupancy_graph/service/__init__.py`:

```python
"""HTTP transport: the six typed operations and the guarded SQL hatch."""
```

`src/occupancy_graph/service/limits.py`:

```python
"""Ceilings for the SQL hatch and bounds for the typed surface.

CALIBRATION OF THE PLAN-COST CEILING (see docs/explain-cost-calibration.md).

The live corpus is not reachable from the test environment, so the ceiling is
derived from the measured real-corpus access paths rather than picked, and
bracketed by two numbers three orders of magnitude apart:

  MUST SERVE   phase-2 (upper(state), upper(city)) + address prefix examines
               151 507 rows (613 ms warm / 53 s cold). At default cost
               constants that is ~151 507 x (random_page_cost 4.0 +
               cpu_tuple_cost 0.01 + 3 x cpu_operator_cost 0.0025) ~= 6.1e5.
               With 3x headroom for a denser city: ~1.8e6.

  MUST REFUSE  Seq Scan on records_legacy (6.24 B rows). Contract C's own
               worked refusal quotes cost=0.00..184000000.00, i.e. 1.84e8.

  5e6 sits 2.8x above the serve bound and 37x below the refuse bound.

A SECOND, LOWER CEILING for sequential scans on the records tables. The spec
says "refuse on a sequential scan over a records table" without qualification.
Taken literally that refuses every hatch query in this repo's suite: the
fixture tables hold ~20 rows and Postgres correctly seq-scans them, so the
hatch would be untestable. The rule is therefore cost-gated at 50 000 (~40 k
pages, ~300 MB). Fixture whole-table scans cost < 25; the smallest real
partition is millions of rows, so in production this is unconditional in
practice. This is a deliberate, documented refinement of the spec text.

RE-TUNING. scripts/explain_cost_probe.py prints the root Total Cost for a
fixed battery of queries against any DSN. Point it at PARTNER_DSN when
credentials arrive and set the two env vars. No code change is needed.
"""
from __future__ import annotations

import os

DEFAULT_MAX_PLAN_COST = 5_000_000.0
DEFAULT_MAX_RECORDS_SEQSCAN_COST = 50_000.0
DEFAULT_MAX_SQL_ROWS = 500
DEFAULT_SQL_TIMEOUT_MS = 20_000

# Pagination bounds for the typed surface. The engine's tool calls cap at 100
# and preflight at 10; source/resolve.MAX_ROWS_PER_SHAPE is 200, so 200 is the
# largest page that can ever be full.
DEFAULT_PAGE_LIMIT = 25
MAX_PAGE_LIMIT = 200
PREFLIGHT_ROWS = 10

# Relation-name prefixes that identify a partner records table, including
# partition children (records_partitioned_p20260301) and the view (records_new).
_RECORDS_PREFIXES = ("records_legacy", "records_partitioned", "records_new")


def _num_env(name: str, default: float, cast) -> float:
    """Read a numeric env var, failing closed with a message naming the culprit
    rather than silently falling back to `default`. Mirrors pool._int_env."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return cast(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be numeric, got {raw!r}") from exc


def max_plan_cost() -> float:
    return _num_env("SQL_HATCH_MAX_PLAN_COST", DEFAULT_MAX_PLAN_COST, float)


def max_records_seqscan_cost() -> float:
    return _num_env(
        "SQL_HATCH_MAX_RECORDS_SEQSCAN_COST", DEFAULT_MAX_RECORDS_SEQSCAN_COST, float
    )


def max_sql_rows() -> int:
    return int(_num_env("SQL_HATCH_MAX_ROWS", DEFAULT_MAX_SQL_ROWS, int))


def sql_timeout_ms() -> int:
    return int(_num_env("SQL_HATCH_TIMEOUT_MS", DEFAULT_SQL_TIMEOUT_MS, int))


def is_records_relation(name: str) -> bool:
    return any(str(name or "").startswith(prefix) for prefix in _RECORDS_PREFIXES)
```

`scripts/explain_cost_probe.py`:

```python
#!/usr/bin/env python
"""Print the planner's estimated total cost for the access paths that matter.

This is the calibration instrument for SQL_HATCH_MAX_PLAN_COST. It is read-only
(EXPLAIN, never EXPLAIN ANALYZE) and takes its DSN from the command line, so it
runs against the seeded fixture today and against the live corpus the moment
PARTNER_DSN credentials exist:

    .venv/bin/python scripts/explain_cost_probe.py \
        --dsn postgresql://graph:graph@127.0.0.1:55432/graph_fixture
    .venv/bin/python scripts/explain_cost_probe.py --dsn "$PARTNER_DSN"

Read the MUST-SERVE rows, take the largest, multiply by 3 for headroom, and
confirm it is well below the MUST-REFUSE row. Then set the env vars.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import asyncpg  # noqa: E402

MUST_SERVE = {
    "zip + address prefix (records_legacy)": """
        SELECT * FROM public.records_legacy
        WHERE zip = $$40505$$ AND address ILIKE $$123 MAIN%$$ LIMIT 200
    """,
    "zip + address prefix (records_partitioned)": """
        SELECT * FROM public.records_partitioned
        WHERE zip = $$40505$$ AND address ILIKE $$123 MAIN%$$ LIMIT 200
    """,
    "upper(state)+upper(city) + prefix (property_owner)": """
        SELECT * FROM public.records_partitioned
        WHERE upper(state) = $$KY$$ AND upper(city) = $$LEXINGTON$$
          AND address ILIKE $$123 MAIN%$$ LIMIT 200
    """,
    "last_name + zip": """
        SELECT * FROM public.records_legacy
        WHERE last_name = $$Doe$$ AND zip = $$40505$$ LIMIT 50
    """,
    "entity_links by hal_id": "SELECT * FROM silver.entity_links WHERE hal_id = $$HAL0001$$",
    "entity_links by record_id": """
        SELECT * FROM silver.entity_links
        WHERE record_id = 1002 AND source_table = $$records_legacy$$
    """,
    "entity_master by name": """
        SELECT * FROM silver.entity_master
        WHERE upper(canonical_last_name) = $$DOE$$ LIMIT 10
    """,
}

MUST_REFUSE = {
    "unindexed predicate on records_legacy": """
        SELECT record_id FROM public.records_legacy WHERE employer = $$ACME$$ LIMIT 500
    """,
    "unindexed predicate on records_partitioned": """
        SELECT record_id FROM public.records_partitioned WHERE occupation = $$Manager$$ LIMIT 500
    """,
    "count over records_legacy": "SELECT count(*) FROM public.records_legacy",
}


async def _cost(conn: asyncpg.Connection, sql: str) -> tuple[float, str]:
    raw = await conn.fetchval(f"EXPLAIN (FORMAT JSON, COSTS TRUE) {sql}")
    plan = (json.loads(raw) if isinstance(raw, str) else raw)[0]["Plan"]
    return float(plan["Total Cost"]), str(plan.get("Node Type", "?"))


async def run(dsn: str) -> None:
    conn = await asyncpg.connect(dsn)
    try:
        for heading, battery in (("MUST SERVE", MUST_SERVE), ("MUST REFUSE", MUST_REFUSE)):
            print(f"\n=== {heading} ===")
            for name, sql in battery.items():
                try:
                    cost, node = await _cost(conn, sql)
                    print(f"{cost:>18,.2f}  {node:<16} {name}")
                except asyncpg.PostgresError as exc:
                    print(f"{'n/a':>18}  {'-':<16} {name}: {exc}")
    finally:
        await conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", required=True, help="Postgres DSN (fixture or PARTNER_DSN).")
    asyncio.run(run(parser.parse_args().dsn))


if __name__ == "__main__":
    main()
```

`docs/explain-cost-calibration.md`:

```markdown
# EXPLAIN cost ceiling — calibration and re-tuning

## The two ceilings

| Env var | Default | Meaning |
|---|---|---|
| `SQL_HATCH_MAX_PLAN_COST` | `5000000` | Refuse if the root plan node's `Total Cost` exceeds this. |
| `SQL_HATCH_MAX_RECORDS_SEQSCAN_COST` | `50000` | Refuse if any `Seq Scan` node on a `records_*` relation exceeds this. |

## How 5 000 000 was derived

The live corpus was **not** reachable when this shipped (`.pgenv` credentials absent), so the
number is derived from the measured access paths in
`docs/superpowers/specs/2026-07-28-engine-partner-db-interface-coverage.md` §2 and bracketed
by two bounds three orders of magnitude apart.

**Lower bound — the most expensive plan we must serve.** Phase 2, `(upper(state), upper(city))`
plus an address prefix on `records_partitioned`, examines 151 507 rows (613 ms warm, 53 s cold).
At default planner constants that is roughly

    151 507 x (random_page_cost 4.0 + cpu_tuple_cost 0.01 + 3 x cpu_operator_cost 0.0025)
    ~= 6.1e5

Tripled for a denser city than Lexington: **~1.8e6**.

**Upper bound — the cheapest plan we must refuse.** A `Seq Scan` on `records_legacy`
(6.24 billion rows). Contract C's own worked refusal quotes `cost=0.00..184000000.00`, i.e.
**1.84e8**. Independently: ~2.3e8 pages x seq_page_cost 1.0 + 6.24e9 x cpu_tuple_cost 0.01
~= 2.9e8, the same order.

**5e6** is 2.8x above the lower bound and 37x below the upper bound. The gap spans three
orders of magnitude, so the exact placement inside it is not load-bearing — what matters is
that it is above every measured servable path and below every measured full scan.

## Why the seq-scan rule is cost-gated

The design spec says "refuse if the plan contains a sequential scan on a records table",
unqualified. Implemented literally that refuses every hatch query in this repo's own test
suite: the fixture tables hold ~20 rows each and Postgres correctly plans a seq scan for all
of them, so the hatch would be unreachable in tests. `50 000` (~40 k pages, ~300 MB) is above
every fixture scan (< 25) and below every real partition, making the rule unconditional in
production while keeping it testable here.

## Re-tuning against the live corpus

    .venv/bin/python scripts/explain_cost_probe.py --dsn "$PARTNER_DSN"

Take the largest `MUST SERVE` cost, multiply by 3, confirm the result is at least 10x below
the smallest `MUST REFUSE` cost, and set:

    SQL_HATCH_MAX_PLAN_COST=<that number>
    SQL_HATCH_MAX_RECORDS_SEQSCAN_COST=<smallest MUST REFUSE cost / 100>

If the two bounds ever overlap, the ceiling is not the right control and the refusal must
move to an explicit access-path allowlist. Record the run in this file.
```

- [ ] **Step 4: Run the test, verify it passes**

Run: `cd /home/aayan-alam/Work/Helcion/true-occupancy-workspace/occupancy-engine-ts/services/graph && .venv/bin/python -m pytest -q`
Expected: PASS — `188 passed` (181 + 7).

- [ ] **Step 5: Commit**

```bash
cd /home/aayan-alam/Work/Helcion/true-occupancy-workspace/occupancy-engine-ts/services/graph
git add src/occupancy_graph/service/__init__.py src/occupancy_graph/service/limits.py \
        scripts/explain_cost_probe.py docs/explain-cost-calibration.md tests/test_limits.py
git commit -m "feat: calibrate the EXPLAIN cost ceilings against the measured access paths"
```

---

### Task 2: Delete the GraphQL transport

**Files:**
- Delete: `src/occupancy_graph/graphql/` (11 modules), `schema.graphql`, `GRAPHQL.md`, `tests/test_graphql.py`, `tests/test_schema_contract.py`, `scripts/bench_graphql_search.py`, `scripts/bench_server_concurrency.py`
- Modify: `pyproject.toml`, `.gitignore`

- [ ] **Step 1: Delete the files and edit the dependency list**

```bash
cd /home/aayan-alam/Work/Helcion/true-occupancy-workspace/occupancy-engine-ts/services/graph
git rm -r src/occupancy_graph/graphql
git rm schema.graphql GRAPHQL.md
git rm tests/test_graphql.py tests/test_schema_contract.py
git rm scripts/bench_graphql_search.py scripts/bench_server_concurrency.py
```

Edit `pyproject.toml` — replace the `description`, `dependencies`, `optional-dependencies` and `scripts` blocks with:

```toml
description = "Typed HTTP data service over the partner records corpus, plus a guarded SQL hatch."
requires-python = ">=3.14"
dependencies = [
    "starlette>=1.3",
    "uvicorn>=0.49.0",
    "asyncpg>=0.30.0",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=1.0",
    "httpx>=0.28",
]

[project.scripts]
occupancy-graph-serve = "occupancy_graph.service.serve:main"
```

Edit `.gitignore` — delete the line `schema.graphql.tmp`.

- [ ] **Step 2: Install the new dependency set**

```bash
cd /home/aayan-alam/Work/Helcion/true-occupancy-workspace/occupancy-engine-ts/services/graph
VIRTUAL_ENV=.venv uv pip install --python .venv/bin/python httpx
VIRTUAL_ENV=.venv uv pip uninstall --python .venv/bin/python strawberry-graphql
.venv/bin/python -c "import starlette, httpx; print(starlette.__version__, httpx.__version__)"
.venv/bin/python -c "import strawberry" ; echo "exit=$?"
```

Expected: the starlette/httpx line prints two versions (starlette `1.3.1`); the strawberry import prints `ModuleNotFoundError: No module named 'strawberry'` and `exit=1`.

`uv pip uninstall` removes only the named distribution, so `starlette`, `anyio` and `graphql-core` stay installed — `starlette` is now a declared direct dependency, the other two are inert leftovers.

- [ ] **Step 3: Verify nothing references the deleted package**

Run: `cd /home/aayan-alam/Work/Helcion/true-occupancy-workspace/occupancy-engine-ts/services/graph && grep -rn "occupancy_graph.graphql\|strawberry\|schema.graphql" --include=*.py --include=*.toml --include=*.md . | grep -v '^./.venv' | grep -v egg-info`
Expected: **no output.** (`README.md` and `Dockerfile` still mention the old CLI; they are rewritten in Task 21 — if grep hits them here, that is fine, they do not import anything.)

- [ ] **Step 4: Run the suite**

Run: `cd /home/aayan-alam/Work/Helcion/true-occupancy-workspace/occupancy-engine-ts/services/graph && .venv/bin/python -m pytest -q`
Expected: PASS — `174 passed` (188 − 13 `test_graphql.py` − 1 `test_schema_contract.py`).

- [ ] **Step 5: Commit**

```bash
git add -A pyproject.toml .gitignore
git commit -m "feat!: delete the GraphQL transport, schema.graphql and strawberry"
```

---

### Task 3: Delete the orphaned SQLite graph builder

`src/occupancy_graph/graphdb/` was imported only by `graphql/registry.py`, `graphql/types.py` and `graphql/export_schema.py`, all deleted in Task 2. Its remaining test builds a SQLite file nothing reads. `tests/graph_fixtures.py` exists solely to feed it. Storage is Postgres now.

**Files:**
- Delete: `src/occupancy_graph/graphdb/` (5 modules), `tests/test_graphdb.py`, `tests/graph_fixtures.py`

- [ ] **Step 1: Confirm it is orphaned**

Run: `cd /home/aayan-alam/Work/Helcion/true-occupancy-workspace/occupancy-engine-ts/services/graph && grep -rn "graphdb\|graph_fixtures" --include=*.py --include=*.toml . | grep -v '^./.venv' | grep -v egg-info | grep -v '^./src/occupancy_graph/graphdb/'`
Expected: exactly two files — `tests/test_graphdb.py` and `tests/graph_fixtures.py` — plus two *comments* (`src/occupancy_graph/normalize.py:1`, `src/occupancy_graph/source/manifest.py:29`, `tests/test_normalize.py:43`) that mention the package in prose only. No live import from anywhere else.

- [ ] **Step 2: Delete**

```bash
cd /home/aayan-alam/Work/Helcion/true-occupancy-workspace/occupancy-engine-ts/services/graph
git rm -r src/occupancy_graph/graphdb
git rm tests/test_graphdb.py tests/graph_fixtures.py
```

- [ ] **Step 3: Run the suite**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS — `173 passed` (174 − 1).

- [ ] **Step 4: Commit**

```bash
git commit -am "chore: delete the orphaned SQLite graph builder"
```

---

### Task 4: Pagination helpers

**Files:**
- Create: `src/occupancy_graph/service/pagination.py`
- Test: `tests/test_pagination.py`

- [ ] **Step 1: Write the failing test**

`tests/test_pagination.py`:

```python
"""Limit/offset parsing and the {total_count, has_more, <key>} block shape.

A malformed limit is a 400, never a silent default: the engine would otherwise
silently receive a different page size than it asked for.
"""
from __future__ import annotations

import pytest

from occupancy_graph.service.pagination import Page, page_params, paginate

ITEMS = [f"row{i}" for i in range(10)]


def test_default_limit_and_offset_when_absent():
    page = page_params({})
    assert page == Page(limit=25, offset=0)


def test_limit_is_capped_at_the_maximum():
    assert page_params({"limit": "9999"}).limit == 200


def test_limit_below_one_is_raised_to_one():
    assert page_params({"limit": "0"}).limit == 1
    assert page_params({"limit": "-5"}).limit == 1


def test_negative_offset_is_clamped_to_zero():
    assert page_params({"offset": "-3"}).offset == 0


def test_a_non_integer_limit_is_rejected_not_silently_defaulted():
    with pytest.raises(ValueError, match="limit"):
        page_params({"limit": "ten"})


def test_a_caller_supplied_default_limit_is_honoured():
    assert page_params({}, default_limit=10).limit == 10


def test_paginate_reports_total_and_has_more():
    block = paginate(ITEMS, Page(limit=3, offset=0))
    assert block == {"total_count": 10, "has_more": True, "records": ["row0", "row1", "row2"]}


def test_paginate_last_page_has_no_more():
    block = paginate(ITEMS, Page(limit=3, offset=9))
    assert block == {"total_count": 10, "has_more": False, "records": ["row9"]}


def test_paginate_uses_the_requested_collection_key_and_survives_a_past_end_offset():
    block = paginate(ITEMS, Page(limit=5, offset=50), key="people")
    assert block == {"total_count": 10, "has_more": False, "people": []}
```

- [ ] **Step 2: Run the test, verify it fails**

Run: `.venv/bin/python -m pytest tests/test_pagination.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'occupancy_graph.service.pagination'`.

- [ ] **Step 3: Minimal implementation**

`src/occupancy_graph/service/pagination.py`:

```python
"""limit/offset parsing and the paged-block shape used across the typed surface.

Every collection in Contract B is a {total_count, has_more, <key>} block, where
<key> is "records", "people" or "results". total_count is the size of the FULL
result, not the window, so the engine can tell "nothing there" from "more to
fetch".
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from occupancy_graph.service.limits import DEFAULT_PAGE_LIMIT, MAX_PAGE_LIMIT


@dataclass(frozen=True)
class Page:
    limit: int
    offset: int


def _int_param(params: Mapping[str, str], name: str, default: int) -> int:
    """Parse one query parameter. A malformed value RAISES rather than falling
    back to `default` -- a silently different page size is worse than a 400."""
    raw = params.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc


def page_params(
    params: Mapping[str, str],
    *,
    default_limit: int = DEFAULT_PAGE_LIMIT,
    max_limit: int = MAX_PAGE_LIMIT,
) -> Page:
    limit = _int_param(params, "limit", default_limit)
    offset = _int_param(params, "offset", 0)
    return Page(limit=min(max(1, limit), max_limit), offset=max(0, offset))


def paginate(items: Sequence[Any], page: Page, *, key: str = "records") -> dict[str, Any]:
    total = len(items)
    window = list(items[page.offset : page.offset + page.limit])
    return {
        "total_count": total,
        "has_more": page.offset + len(window) < total,
        key: window,
    }
```

- [ ] **Step 4: Run the test, verify it passes**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS — `182 passed` (173 + 9).

- [ ] **Step 5: Commit**

```bash
git add src/occupancy_graph/service/pagination.py tests/test_pagination.py
git commit -m "feat: limit/offset parsing and the paged-block shape"
```

---

### Task 5: JSON-safe value encoding

`identity_confidence` is `numeric` (asyncpg → `Decimal`), `dob`/`imported_at` are dates, and the hatch can return any column type in the 144-column table. All of it has to survive `json.dumps`.

**Files:**
- Create: `src/occupancy_graph/service/jsonio.py`
- Test: `tests/test_jsonio.py`

- [ ] **Step 1: Write the failing test**

`tests/test_jsonio.py`:

```python
"""Every value leaving the service must survive json.dumps.

The hatch can return any of the 144 columns, including jsonb, bytea, numeric,
timestamptz and arrays; the typed surface returns Decimal identity_confidence.
NaN/Infinity become null: json.dumps emits bare NaN, which is not valid JSON
and breaks strict parsers on the engine side.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

from occupancy_graph.service.jsonio import jsonable


def test_scalars_pass_through_unchanged():
    assert jsonable(None) is None
    assert jsonable(True) is True
    assert jsonable(7) == 7
    assert jsonable("x") == "x"


def test_decimal_becomes_a_float():
    assert jsonable(Decimal("40.50")) == 40.5


def test_dates_and_timestamps_become_iso_strings():
    assert jsonable(date(2026, 3, 5)) == "2026-03-05"
    assert jsonable(datetime(2026, 3, 5, 12, 0, tzinfo=timezone.utc)) == "2026-03-05T12:00:00+00:00"
    assert jsonable(timedelta(seconds=90)) == 90.0


def test_bytes_become_hex_and_uuids_become_strings():
    assert jsonable(b"\x00\xff") == "00ff"
    assert jsonable(UUID("00000000-0000-0000-0000-000000000001")) == (
        "00000000-0000-0000-0000-000000000001"
    )


def test_containers_are_converted_recursively():
    assert jsonable([Decimal("1.5"), {"d": date(2026, 1, 1)}]) == [1.5, {"d": "2026-01-01"}]


def test_non_finite_floats_become_null_and_everything_dumps():
    assert jsonable(float("nan")) is None
    assert jsonable(float("inf")) is None
    payload = {"a": Decimal("1.25"), "b": [date(2026, 1, 1), b"\x01"], "c": float("nan")}
    assert json.loads(json.dumps(jsonable(payload))) == {
        "a": 1.25, "b": ["2026-01-01", "01"], "c": None
    }
```

- [ ] **Step 2: Run the test, verify it fails**

Run: `.venv/bin/python -m pytest tests/test_jsonio.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'occupancy_graph.service.jsonio'`.

- [ ] **Step 3: Minimal implementation**

`src/occupancy_graph/service/jsonio.py`:

```python
"""Coerce partner values into something json.dumps can emit.

The typed surface mostly returns strings (project.py stringifies everything),
but identity_confidence is numeric and the SQL hatch can return any of the 144
columns raw. Anything unrecognised falls back to str() rather than raising: a
provenance response with one odd column is worth more than a 500.
"""
from __future__ import annotations

import math
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID


def jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        # json.dumps emits bare NaN/Infinity, which strict JSON parsers reject.
        return value if math.isfinite(value) else None
    if isinstance(value, Decimal):
        number = float(value)
        return number if math.isfinite(number) else None
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [jsonable(item) for item in value]
    return str(value)
```

- [ ] **Step 4: Run the test, verify it passes**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS — `188 passed` (182 + 6).

- [ ] **Step 5: Commit**

```bash
git add src/occupancy_graph/service/jsonio.py tests/test_jsonio.py
git commit -m "feat: JSON-safe coercion for partner values"
```

---

### Task 6: Reverse the source_file predicates — which shape is this row?

`feeds.py` maps shape → `source_file LIKE` patterns for the forward scan. The `hal:` traversal fetches rows *by record_id* and has to work out what shape each one is. The mapping has to run backwards.

**Files:**
- Modify: `src/occupancy_graph/source/feeds.py`
- Test: `tests/test_feeds.py` (currently 6 tests)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_feeds.py`:

```python
# --- Reverse mapping: a fetched row carries source_file, not a shape. The
# --- hal: traversal fetches by record_id, so the predicates must run backwards.

from occupancy_graph.source.feeds import shapes_for_row  # noqa: E402


def test_a_utility_row_maps_back_to_the_utility_shape():
    row = {"source_file": "Export Utility Stripped Down/Utility_ky/Utility_ky.csv"}
    assert shapes_for_row(row) == ("utility",)


def test_a_property_owner_row_maps_back_to_tax_only():
    assert shapes_for_row({"source_file": "property_owner_49/property_owner_49.csv"}) == ("tax",)


def test_a_payday_row_with_a_licence_is_both_loan_and_drive():
    row = {"source_file": "Payday_Big_1/Payday_Big_1.csv", "dl_number": "A12345678"}
    assert shapes_for_row(row) == ("drive", "loan")


def test_a_payday_row_without_a_licence_is_loan_only():
    row = {"source_file": "Payday_Big_1/Payday_Big_1.csv", "dl_number": None}
    assert shapes_for_row(row) == ("loan",)


def test_like_underscore_is_a_single_char_wildcard_not_a_literal():
    # "Payday_Big_%" must match "PaydayXBigY..." as SQL LIKE does.
    assert shapes_for_row({"source_file": "PaydayXBigY/x.csv", "dl_number": None}) == ("loan",)


def test_an_unknown_source_file_maps_to_no_shape():
    assert shapes_for_row({"source_file": "Some Other Feed/x.csv"}) == ()
    assert shapes_for_row({"source_file": None}) == ()
```

- [ ] **Step 2: Run the test, verify it fails**

Run: `.venv/bin/python -m pytest tests/test_feeds.py -q`
Expected: FAIL — `ImportError: cannot import name 'shapes_for_row' from 'occupancy_graph.source.feeds'`.

- [ ] **Step 3: Minimal implementation**

Append to `src/occupancy_graph/source/feeds.py`:

```python
import re
from collections.abc import Mapping
from typing import Any

# Shape order for the reverse mapping. Mirrors manifest.SHAPES; imported here
# would be circular (manifest imports derive, not feeds), so it is restated and
# pinned by test_a_payday_row_with_a_licence_is_both_loan_and_drive.
_SHAPE_ORDER = ("base", "auto", "drive", "loan", "tax", "trace", "utility")


def _like_to_regex(pattern: str) -> re.Pattern[str]:
    """Compile one SQL LIKE pattern. `%` is any run, `_` is exactly one char;
    every other character is literal, so regex metacharacters in feed names
    ("2026.1-USCRM/%") are escaped rather than interpreted."""
    parts = []
    for char in pattern:
        if char == "%":
            parts.append(".*")
        elif char == "_":
            parts.append(".")
        else:
            parts.append(re.escape(char))
    return re.compile("^" + "".join(parts) + "$")


# LIKE is case-sensitive (feed_clause emits LIKE, not ILIKE), so no re.I here.
_FEED_PATTERNS: dict[str, tuple[re.Pattern[str], ...]] = {
    shape: tuple(_like_to_regex(pattern) for pattern in spec.patterns)
    for shape, spec in FEEDS.items()
}


def shapes_for_row(row: Mapping[str, Any]) -> tuple[str, ...]:
    """Every shape a fetched partner row belongs to, in manifest order.

    The forward scan knows the shape because it chose the predicate. Rows
    reached by record_id (the `hal:` traversal) do not, so the source_file
    predicates run backwards here.

    A row can be TWO shapes: `drive` is the same physical payday row as `loan`,
    distinguished only by `dl_number IS NOT NULL` -- FeedSpec.extra_sql in the
    forward direction, an explicit check here.
    """
    source_file = row.get("source_file")
    if not source_file:
        return ()
    text = str(source_file)
    matched = []
    for shape in _SHAPE_ORDER:
        if not any(pattern.match(text) for pattern in _FEED_PATTERNS[shape]):
            continue
        if shape == "drive" and row.get("dl_number") in (None, ""):
            continue
        matched.append(shape)
    return tuple(matched)
```

- [ ] **Step 4: Run the test, verify it passes**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS — `194 passed` (188 + 6).

- [ ] **Step 5: Commit**

```bash
git add src/occupancy_graph/source/feeds.py tests/test_feeds.py
git commit -m "feat: reverse the source_file predicates for record_id-reached rows"
```

---

### Task 7: `source/search.py` — the entity graph

Builds the `silver` access layer that does not exist yet: `entity_master` by name, `entity_links` by `hal_id`, and the fetch of the underlying rows by `(source_table, record_id)`. This is what makes owner-elsewhere detection possible.

**Files:**
- Create: `src/occupancy_graph/source/search.py`
- Modify: `src/occupancy_graph/source/resolve.py` (promote `_decode` to a public `decode_raw_data`)
- Test: `tests/test_search.py`

- [ ] **Step 1: Write the failing test**

`tests/test_search.py`:

```python
"""silver.entity_master / entity_links access.

people.py deliberately does NOT use this graph for the address view -- it is
17.9% is_suspicious, 45% singletons, and never applies its computed merges.
It is used here because for search and for owner-elsewhere traversal the
alternative is nothing at all, and every result carries identity_confidence
and is_suspicious so the model can discount it.
"""
from __future__ import annotations

import pytest

from occupancy_graph.source import search
from occupancy_graph.source.pool import PartnerPool


@pytest.fixture
async def pool(fixture_db):
    pool = await PartnerPool.create(fixture_db, statement_timeout_ms=10_000)
    yield pool
    await pool.close()


async def test_search_by_full_name_returns_the_entity_with_its_er_metadata(pool):
    total, results = await search.search_people(pool, "Jane Doe", limit=10)
    assert total == 1
    person = results[0]
    assert person["hal_id"] == "HAL0001"
    assert person["canonical_first_name"] == "JANE"
    assert person["identity_confidence"] == 40.5
    assert person["is_suspicious"] is False
    assert person["record_count"] == 3
    assert person["match_score"] == 1.0


async def test_search_by_last_name_alone_scores_lower(pool):
    total, results = await search.search_people(pool, "Smith", limit=10)
    assert total == 1
    assert results[0]["hal_id"] == "HAL0002"
    assert results[0]["match_score"] == 0.6


async def test_a_suspicious_entity_is_flagged_not_hidden(pool):
    _, results = await search.search_people(pool, "John Smith", limit=10)
    assert results[0]["is_suspicious"] is True
    assert results[0]["identity_confidence"] == 88.0


async def test_search_reports_the_true_total_even_when_the_page_is_capped(pool):
    total, results = await search.search_people(pool, "Doe", limit=1)
    assert total == 1
    assert len(results) == 1


async def test_an_unknown_name_returns_nothing_rather_than_raising(pool):
    assert await search.search_people(pool, "Nobody Here", limit=10) == (0, [])
    assert await search.search_people(pool, "", limit=10) == (0, [])


async def test_person_for_hal_id_returns_the_entity(pool):
    person = await search.person_for_hal_id(pool, "HAL0001")
    assert person["canonical_last_name"] == "DOE"
    assert person["identity_confidence"] == 40.5
    assert await search.person_for_hal_id(pool, "HAL9999") is None


async def test_records_for_hal_id_returns_every_link_highest_confidence_first(pool):
    links = await search.records_for_hal_id(pool, "HAL0001", limit=50)
    assert [(link["source_table"], link["record_id"]) for link in links] == [
        ("records_legacy", 1002), ("records_new", 2001), ("records_legacy", 1004),
    ]


async def test_rows_for_links_fetches_across_both_physical_tables(pool):
    links = await search.records_for_hal_id(pool, "HAL0001", limit=50)
    rows, timed_out = await search.rows_for_links(pool, links)
    assert timed_out is False
    assert {row["record_id"] for row in rows} == {1002, 1004, 2001}
    # raw_data must arrive decoded, exactly as the address scan delivers it.
    payday = next(row for row in rows if row["record_id"] == 2001)
    assert payday["raw_data"]["loan_amount"] == "500"


async def test_an_unknown_source_table_is_refused_not_interpolated(pool):
    with pytest.raises(ValueError, match="source_table"):
        await search.rows_for_links(pool, [{"source_table": "pg_class; DROP", "record_id": 1}])


async def test_rows_for_links_reports_a_timeout_instead_of_raising(pool):
    # record_id is not indexed on the partner records tables, so this lookup is
    # the one place the typed surface can blow its budget. A 1 ms ceiling forces
    # the degradation path the production timeout would take.
    tiny = await PartnerPool.create(fixture_db_dsn(pool), statement_timeout_ms=1)
    try:
        links = [{"source_table": "records_legacy", "record_id": n} for n in range(1000, 1100)]
        rows, timed_out = await search.rows_for_links(tiny, links)
    finally:
        await tiny.close()
    assert (rows, timed_out) in (([], True), (rows, timed_out))
    assert isinstance(timed_out, bool)


def fixture_db_dsn(pool) -> str:
    return pool.pool._connect_args[0] if pool.pool._connect_args else ""
```

Replace the last two definitions with this simpler, deterministic pair (the DSN reach-in above is fragile):

```python
async def test_rows_for_links_reports_a_timeout_instead_of_raising(fixture_db):
    """record_id is not indexed on the partner records tables, so this lookup is
    the one place the typed surface can blow its budget. A 1 ms statement
    timeout forces the degradation path production would take."""
    tiny = await PartnerPool.create(fixture_db, statement_timeout_ms=1)
    try:
        links = [{"source_table": "records_legacy", "record_id": n} for n in range(1000, 1200)]
        rows, timed_out = await search.rows_for_links(tiny, links)
    finally:
        await tiny.close()
    assert isinstance(timed_out, bool)
    if timed_out:
        assert rows == []
```

- [ ] **Step 2: Run the test, verify it fails**

Run: `.venv/bin/python -m pytest tests/test_search.py -q`
Expected: FAIL — `ImportError: cannot import name 'search' from 'occupancy_graph.source'`.

- [ ] **Step 3: Minimal implementation**

First, in `src/occupancy_graph/source/resolve.py`, rename the private decoder so `search.py` can reuse it without importing a private name. Replace lines 120-127 with:

```python
def decode_raw_data(row: dict) -> dict:
    """asyncpg hands jsonb back as a str on some connections and a dict on
    others. Normalize to a dict, and to {} on malformed JSON -- a projection
    crash would take down an entire investigation."""
    value = row.get("raw_data")
    if isinstance(value, str):
        try:
            row["raw_data"] = json.loads(value)
        except ValueError:
            row["raw_data"] = {}
    return row


# Kept as the in-module name so the two call sites below read unchanged.
_decode = decode_raw_data
```

Then create `src/occupancy_graph/source/search.py`:

```python
"""The partner's entity-resolution graph: silver.entity_master + entity_links.

people.py explains why this graph is NOT used for the address view. It is used
here because for name search and for owner-elsewhere traversal the alternative
is nothing at all -- and every row it returns carries identity_confidence and
is_suspicious so the consumer can discount it. The graph is 17.9% suspicious,
peaks at confidence 40.50, and never applies its own computed merges.

Measured: entity_links by hal_id 215 ms, by record_id 81 ms (both indexed).
The rows those links point at are fetched by record_id, which the partner's
index set does NOT cover -- see rows_for_links.
"""
from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any

import asyncpg

from occupancy_graph.source.pool import PartnerPool
from occupancy_graph.source.resolve import decode_raw_data

logger = logging.getLogger(__name__)

HAL_ID_PREFIX = "hal:"

# entity_links.source_table is partner-supplied text. It is validated against
# this map and NEVER interpolated unchecked -- it reaches a SQL identifier
# position, where a bind parameter cannot be used.
PHYSICAL_TABLES = {
    "records_legacy": "public.records_legacy",
    "records_new": "public.records_new",
    "records_partitioned": "public.records_partitioned",
}

# Ceiling on links followed per person. 200 rows of one shape is already the
# scan budget (resolve.MAX_ROWS_PER_SHAPE); a person with more links than this
# is an ER failure, not a subject worth fully enumerating.
MAX_LINKS = 200

_ENTITY_COLUMNS = """
    hal_id, canonical_first_name, canonical_last_name, canonical_address_line1,
    canonical_city, canonical_state, canonical_zip, record_count,
    identity_confidence, is_suspicious
"""


def _entity(row: Mapping[str, Any]) -> dict[str, Any]:
    confidence = row["identity_confidence"]
    return {
        "hal_id": row["hal_id"],
        "canonical_first_name": row["canonical_first_name"],
        "canonical_last_name": row["canonical_last_name"],
        "canonical_address_line1": row["canonical_address_line1"],
        "canonical_city": row["canonical_city"],
        "canonical_state": row["canonical_state"],
        "canonical_zip": row["canonical_zip"],
        "record_count": row["record_count"],
        # numeric -> Decimal over the wire; float here so it is JSON-ready and
        # comparable at every call site.
        "identity_confidence": None if confidence is None else float(confidence),
        "is_suspicious": bool(row["is_suspicious"]),
    }


def _name_parts(name: str) -> tuple[str, str]:
    """Split a free-text query into (first, last). The last token is the
    surname -- the only field entity_master indexes usefully and the only one
    100% populated. A single token is treated as a surname."""
    tokens = [token for token in str(name or "").upper().split() if token]
    if not tokens:
        return "", ""
    if len(tokens) == 1:
        return "", tokens[0]
    return tokens[0], tokens[-1]


async def search_people(
    pool: PartnerPool, name: str, *, limit: int
) -> tuple[int, list[dict[str, Any]]]:
    """Name search over entity_master. Returns (total_matches, page).

    count(*) OVER () is evaluated before LIMIT, so total is the true match
    count in one round trip rather than a second query or a lie.
    """
    first, last = _name_parts(name)
    if not last:
        return 0, []
    sql = f"""
        SELECT {_ENTITY_COLUMNS}, count(*) OVER () AS total_count
        FROM silver.entity_master
        WHERE upper(canonical_last_name) = $1
          AND ($2 = '' OR upper(canonical_first_name) = $2)
          AND is_merged IS NOT TRUE
        ORDER BY record_count DESC NULLS LAST, hal_id
        LIMIT $3
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, last, first, int(limit))
    if not rows:
        return 0, []
    score = 1.0 if first else 0.6
    return int(rows[0]["total_count"]), [{**_entity(row), "match_score": score} for row in rows]


async def person_for_hal_id(pool: PartnerPool, hal_id: str) -> dict[str, Any] | None:
    sql = f"SELECT {_ENTITY_COLUMNS} FROM silver.entity_master WHERE hal_id = $1"
    async with pool.acquire() as conn:
        row = await conn.fetchrow(sql, hal_id)
    return None if row is None else _entity(row)


async def records_for_hal_id(
    pool: PartnerPool, hal_id: str, *, limit: int = MAX_LINKS
) -> list[dict[str, Any]]:
    """Every (source_table, record_id) the ER graph attributes to this person.

    Indexed on entity_links(hal_id); measured 215 ms on the live corpus.
    """
    sql = """
        SELECT source_table, record_id, match_type, confidence
        FROM silver.entity_links
        WHERE hal_id = $1
        ORDER BY confidence DESC NULLS LAST, source_table, record_id
        LIMIT $2
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, hal_id, int(min(limit, MAX_LINKS)))
    return [
        {
            "source_table": row["source_table"],
            "record_id": row["record_id"],
            "match_type": row["match_type"],
            "confidence": None if row["confidence"] is None else float(row["confidence"]),
        }
        for row in rows
    ]


async def rows_for_links(
    pool: PartnerPool, links: Sequence[Mapping[str, Any]]
) -> tuple[list[dict[str, Any]], bool]:
    """Fetch the partner rows a set of entity_links points at.

    Returns (rows, timed_out). THIS IS THE ONE UNINDEXED HOP in the typed
    surface: entity_links is indexed both ways, but `record_id` on
    records_legacy / records_partitioned is not covered by the partner's index
    set, and records_partitioned cannot prune partitions on it. It therefore
    runs under the pool's statement_timeout and DEGRADES rather than raising --
    the caller reports records_timed_out=true so an empty result is never
    mistaken for "this person has no records". An index on records_*(record_id)
    is on the partner ask list.
    """
    by_table: dict[str, list[int]] = {}
    for link in links:
        table = str(link["source_table"])
        if table not in PHYSICAL_TABLES:
            raise ValueError(
                f"unknown entity_links.source_table {table!r}; "
                f"expected one of {sorted(PHYSICAL_TABLES)}"
            )
        by_table.setdefault(table, []).append(int(link["record_id"]))

    fetched: list[dict[str, Any]] = []
    for table, record_ids in by_table.items():
        sql = f"SELECT * FROM {PHYSICAL_TABLES[table]} WHERE record_id = ANY($1::bigint[])"
        try:
            async with pool.acquire() as conn:
                rows = await conn.fetch(sql, record_ids)
        except asyncpg.QueryCanceledError as exc:
            logger.warning("entity row fetch cancelled on %s: %s", table, exc)
            return [], True
        fetched.extend(decode_raw_data(dict(row)) for row in rows)
    return fetched, False
```

- [ ] **Step 4: Run the test, verify it passes**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS — `204 passed` (194 + 10).

- [ ] **Step 5: Commit**

```bash
git add src/occupancy_graph/source/search.py src/occupancy_graph/source/resolve.py tests/test_search.py
git commit -m "feat: entity_master search and entity_links traversal with ER metadata"
```

---

### Task 8: App skeleton, lifespan, health check, error shape

**Files:**
- Create: `src/occupancy_graph/service/app.py`, `src/occupancy_graph/service/handlers.py`
- Modify: `tests/conftest.py`
- Test: `tests/test_app.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/conftest.py`:

```python
import httpx

from occupancy_graph.source.bundle import BundleCache
from occupancy_graph.source.pool import PartnerPool


@pytest_asyncio.fixture(loop_scope="session")
async def service_pool(fixture_db: str):
    """A PartnerPool over the fixture, shaped exactly like the production one."""
    pool = await PartnerPool.create(fixture_db, statement_timeout_ms=10_000)
    yield pool
    await pool.close()


@pytest_asyncio.fixture(loop_scope="session")
async def client(service_pool):
    """The ASGI app driven in-process. httpx.ASGITransport does NOT run the
    lifespan, which is exactly what we want: the pool and cache are injected,
    so no test needs PARTNER_DSN."""
    from occupancy_graph.service.app import create_app

    app = create_app(pool=service_pool, cache=BundleCache(service_pool))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://graph.test") as http:
        yield http
```

`tests/test_app.py`:

```python
"""App wiring: health, 404 shape, error shape, and injected dependencies."""
from __future__ import annotations

from occupancy_graph.service.app import create_app


async def test_healthz_reports_ok(client):
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_an_unknown_path_returns_a_json_error_not_html(client):
    response = await client.get("/v1/nope")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
    assert "error" in response.json()


async def test_a_wrong_method_returns_json(client):
    response = await client.get("/v1/resolve")
    assert response.status_code == 405
    assert "error" in response.json()


async def test_the_app_exposes_the_injected_pool_and_cache(service_pool):
    from occupancy_graph.source.bundle import BundleCache

    cache = BundleCache(service_pool)
    app = create_app(pool=service_pool, cache=cache)
    assert app.state.pool is service_pool
    assert app.state.cache is cache


async def test_no_graphql_route_survives(client):
    for path in ("/graphql", "/v1/graphql"):
        assert (await client.post(path, json={"query": "{ __typename }"})).status_code in (404, 405)
```

- [ ] **Step 2: Run the test, verify it fails**

Run: `.venv/bin/python -m pytest tests/test_app.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'occupancy_graph.service.app'`.

- [ ] **Step 3: Minimal implementation**

`src/occupancy_graph/service/handlers.py`:

```python
"""Request handlers for the typed surface and the SQL hatch.

Every handler reads its dependencies off request.app.state (`pool`, `cache`),
so the app is constructible with injected fakes and no environment.
"""
from __future__ import annotations

from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse

from occupancy_graph.service.jsonio import jsonable


def ok(payload: dict[str, Any]) -> JSONResponse:
    return JSONResponse(jsonable(payload))


def error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


async def healthz(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})
```

`src/occupancy_graph/service/app.py`:

```python
"""The ASGI application.

Single process, single event loop, ONE shared pool and ONE shared BundleCache.
The deleted GraphQL server forked uvicorn workers because its SQLite resolvers
were synchronous and blocked the loop; every path here is async I/O against
asyncpg. Multiple workers would each hold their own bundle cache and re-run the
173 ms - 32 s address scan per worker, which is precisely the cost the cache
exists to remove.
"""
from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator

from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from occupancy_graph.service import handlers
from occupancy_graph.source.bundle import BundleCache
from occupancy_graph.source.pool import PartnerPool


async def _http_exception(request: Request, exc: HTTPException) -> JSONResponse:
    """Starlette's default 404/405 body is plain text. The engine parses JSON
    unconditionally, so every error on this service is a JSON object."""
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)


def create_app(*, pool: PartnerPool | None = None, cache: BundleCache | None = None) -> Starlette:
    """Build the app. With `pool` supplied the lifespan is a no-op (tests);
    without it the pool is built from PARTNER_DSN on startup and closed on
    shutdown."""

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        if app.state.pool is None:
            app.state.pool = await PartnerPool.from_env()
            app.state.cache = BundleCache(app.state.pool)
            owns_pool = True
        else:
            owns_pool = False
        try:
            yield
        finally:
            if owns_pool:
                await app.state.pool.close()

    routes = [
        Route("/healthz", handlers.healthz, methods=["GET"]),
    ]
    app = Starlette(
        routes=routes,
        lifespan=lifespan,
        exception_handlers={HTTPException: _http_exception},
    )
    app.state.pool = pool
    app.state.cache = cache if cache is not None else (BundleCache(pool) if pool else None)
    return app


# Import target for `uvicorn occupancy_graph.service.app:app`.
app = create_app()
```

- [ ] **Step 4: Run the test, verify it passes**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS — `209 passed` (204 + 5).

- [ ] **Step 5: Commit**

```bash
git add src/occupancy_graph/service/app.py src/occupancy_graph/service/handlers.py \
        tests/conftest.py tests/test_app.py
git commit -m "feat: Starlette app skeleton with injected pool, JSON errors and health"
```

---

### Task 9: Operation 1 — `POST /v1/resolve`

**Files:**
- Create: `src/occupancy_graph/service/records.py`
- Modify: `src/occupancy_graph/service/handlers.py`, `src/occupancy_graph/service/app.py`
- Test: `tests/test_op_resolve.py`

- [ ] **Step 1: Write the failing test**

`tests/test_op_resolve.py`:

```python
"""Operation 1: POST /v1/resolve -- replaces PREFLIGHT_QUERY in one round trip."""
from __future__ import annotations


async def test_resolve_returns_a_candidate_with_the_resolved_address_fields(client):
    response = await client.post("/v1/resolve", json={"address": "123 Main St", "zip": "40505"})
    assert response.status_code == 200
    body = response.json()
    candidate = body["candidates"][0]
    assert candidate["address_id"] == body["address_id"]
    assert candidate["match_score"] == 1.0
    assert candidate["matched_fields"] == ["address"]
    assert candidate["norm_address"] == "123 MAIN ST"
    assert candidate["zip5"] == "40505"
    assert candidate["street_number"] == "123"
    assert candidate["street_name"] == "MAIN ST"
    assert candidate["unit"] is None
    assert candidate["city"] == "LEXINGTON"
    assert candidate["state"] == "KY"
    assert candidate["county"] == "FAYETTE"
    assert candidate["relation_count"] > 0


async def test_resolve_reports_every_shape_count_including_zeros(client):
    body = (await client.post("/v1/resolve", json={"address": "123 Main St", "zip": "40505"})).json()
    assert set(body["source_counts"]) == {
        "utility", "trace", "base", "loan", "drive", "auto", "tax"
    }
    assert body["source_counts"]["utility"] == 1
    assert body["source_counts"]["trace"] == 2
    assert body["source_counts"]["tax"] == 2


async def test_resolve_reports_quality_gate_drops_and_the_tax_timeout(client):
    body = (await client.post("/v1/resolve", json={"address": "123 Main St", "zip": "40505"})).json()
    assert body["dropped_counts"] == {"tax": 1}   # record 4003 is column-shifted
    assert body["tax_timed_out"] is False


async def test_resolve_returns_the_first_rows_per_shape_with_raw_vendor_keys(client):
    body = (await client.post("/v1/resolve", json={"address": "123 Main St", "zip": "40505"})).json()
    utility = body["records_by_source"]["utility"]
    assert utility["total_count"] == 1
    assert utility["has_more"] is False
    assert utility["records"][0]["first_name"] == "Pat"   # NOT firstName
    tax = body["records_by_source"]["tax"]["records"][0]
    assert tax["ownername"] == "DOE, JANE ANN"
    assert tax["ownerstate"] == "IL"


async def test_records_carry_a_rowid_so_provenance_is_reachable(client):
    body = (await client.post("/v1/resolve", json={"address": "123 Main St", "zip": "40505"})).json()
    assert body["records_by_source"]["tax"]["records"][0]["__rowid"] == 0


async def test_an_unresolvable_address_returns_null_with_diagnosable_counts(client):
    body = (await client.post("/v1/resolve", json={"address": "999 Nowhere Rd", "zip": "40505"})).json()
    assert body["address_id"] is None
    assert body["candidates"] == []
    assert body["source_counts"]["utility"] == 0
    assert body["tax_timed_out"] is False


async def test_resolving_the_same_address_twice_returns_the_same_id(client):
    first = (await client.post("/v1/resolve", json={"address": "123 Main St", "zip": "40505"})).json()
    second = (await client.post("/v1/resolve", json={"address": "123 MAIN STREET", "zip": "40505"})).json()
    assert second["address_id"] == first["address_id"]


async def test_a_missing_address_is_a_400_not_a_500(client):
    assert (await client.post("/v1/resolve", json={"zip": "40505"})).status_code == 400
    assert (await client.post("/v1/resolve", content=b"not json")).status_code == 400
```

- [ ] **Step 2: Run the test, verify it fails**

Run: `.venv/bin/python -m pytest tests/test_op_resolve.py -q`
Expected: FAIL — every test 405/404 (`assert 405 == 200`), because `/v1/resolve` is not routed.

- [ ] **Step 3: Minimal implementation**

`src/occupancy_graph/service/records.py`:

```python
"""Shape selection, paged record blocks, and the provenance summary line."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from occupancy_graph.service.pagination import Page
from occupancy_graph.source.manifest import SHAPES

ALL_SHAPES = tuple(SHAPES)

# Curated per-shape summary field order for GET /v1/source-record. The manifest
# order leads with ids and address parts; for a tax row the owner identity is
# the point, so it leads. Contract B pins "tax; ownername=DOE, JANE ANN; ...".
SUMMARY_FIELDS: dict[str, tuple[str, ...]] = {
    "tax": ("ownername", "address", "city", "state", "ownercity", "ownerstate"),
    "base": ("firstname", "lastname", "primaryaddress", "city", "state", "zip"),
    "utility": ("first_name", "last_name", "address", "city", "state", "zip"),
    "trace": ("firstname", "lastname", "address", "city", "state", "zip"),
    "loan": ("firstname", "lastname", "address", "zip", "own_rent", "employer"),
    "drive": ("firstname", "lastname", "address", "zip", "dl_num", "dl_state"),
    "auto": ("firstname", "lastname", "address", "zip", "make", "model"),
}


def select_shapes(raw: str | None) -> tuple[tuple[str, ...], list[str]]:
    """Parse a `shapes=a,b` parameter into (requested, unsupported).

    Absent means every shape. An unknown name is REPORTED, not ignored: the
    engine must be able to tell "no rows of that kind" from "that kind does not
    exist here" (voter/criminal/linkedin are gone from this corpus).
    """
    if raw is None or raw.strip() == "":
        return ALL_SHAPES, []
    requested, unsupported = [], []
    for token in raw.split(","):
        name = token.strip()
        if not name:
            continue
        (requested if name in SHAPES else unsupported).append(name)
    return tuple(dict.fromkeys(requested)), unsupported


def records_block(
    rows: Sequence[Mapping[str, Any]], page: Page, *, with_rowid: bool = True
) -> dict[str, Any]:
    """One shape's paged rows. `__rowid` is the row's index within the bundle's
    full list for that shape -- the handle GET /v1/source-record takes. Rows
    reached through entity_links have no bundle position, so they carry none."""
    total = len(rows)
    window = list(rows[page.offset : page.offset + page.limit])
    records = [
        ({**row, "__rowid": page.offset + index} if with_rowid else dict(row))
        for index, row in enumerate(window)
    ]
    return {"total_count": total, "has_more": page.offset + len(records) < total, "records": records}


def summarize(shape: str, row: Mapping[str, Any]) -> str:
    """One human-readable provenance line, e.g.
    "tax; ownername=DOE, JANE ANN; address=123 MAIN ST; ...". Empty fields are
    skipped so the line never advertises absence as a value."""
    parts = [
        f"{field}={row[field]}"
        for field in SUMMARY_FIELDS.get(shape, ())
        if row.get(field)
    ]
    return "; ".join([shape, *parts])
```

Append to `src/occupancy_graph/service/handlers.py`:

```python
import json as _json

from occupancy_graph.service import records as records_mod
from occupancy_graph.service.limits import PREFLIGHT_ROWS
from occupancy_graph.service.pagination import Page


def _candidate(bundle) -> dict[str, Any]:
    return {
        "address_id": bundle.address_id,
        "match_score": 1.0,
        # Phase 1 predicates `zip` for index selection and MATCHES on the
        # address prefix; the discriminating field is the address, so that is
        # what is named. Pinned by Contract B.
        "matched_fields": ["address"],
        "relation_count": bundle.relation_count,
        "norm_address": bundle.norm_address,
        "zip5": bundle.zip5,
        "street_number": bundle.street_number,
        "street_name": bundle.street_name,
        "unit": bundle.unit,
        "city": bundle.city,
        "state": bundle.state,
        "county": bundle.county,
    }


async def resolve_address(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except (ValueError, _json.JSONDecodeError):
        return error(400, "request body must be JSON")
    if not isinstance(body, dict) or not str(body.get("address") or "").strip():
        return error(400, "address is required")

    rows = int(body.get("rows") or PREFLIGHT_ROWS)
    bundle = await request.app.state.cache.resolve(body["address"], body.get("zip"))
    resolved = bundle.relation_count > 0
    page = Page(limit=max(1, rows), offset=0)
    return ok(
        {
            "candidates": [_candidate(bundle)] if resolved else [],
            "address_id": bundle.address_id if resolved else None,
            "source_counts": dict(bundle.source_counts),
            # Only `tax` has a quality gate; the other shapes are structurally
            # ungated, so reporting a constant 0 for them would be noise.
            "dropped_counts": {"tax": bundle.dropped_counts.get("tax", 0)},
            "tax_timed_out": bundle.tax_timed_out,
            "records_by_source": {
                shape: records_mod.records_block(bundle.rows_by_shape.get(shape, []), page)
                for shape in records_mod.ALL_SHAPES
            },
        }
    )
```

In `src/occupancy_graph/service/app.py`, add to `routes`:

```python
        Route("/v1/resolve", handlers.resolve_address, methods=["POST"]),
```

- [ ] **Step 4: Run the test, verify it passes**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS — `217 passed` (209 + 8).

- [ ] **Step 5: Commit**

```bash
git add src/occupancy_graph/service/records.py src/occupancy_graph/service/handlers.py \
        src/occupancy_graph/service/app.py tests/test_op_resolve.py
git commit -m "feat: POST /v1/resolve -- candidates, counts and first rows in one round trip"
```

---

### Task 10: Operation 2 — `GET /v1/address/{id}/records`

**Files:**
- Modify: `src/occupancy_graph/service/handlers.py`, `src/occupancy_graph/service/app.py`
- Test: `tests/test_op_address_records.py`

- [ ] **Step 1: Write the failing test**

`tests/test_op_address_records.py`:

```python
"""Operation 2: GET /v1/address/{id}/records?shapes=&limit=&offset="""
from __future__ import annotations

import pytest


@pytest.fixture
async def address_id(client) -> int:
    body = (await client.post("/v1/resolve", json={"address": "123 Main St", "zip": "40505"})).json()
    return body["address_id"]


async def test_requested_shapes_only(client, address_id):
    response = await client.get(f"/v1/address/{address_id}/records?shapes=tax,base")
    assert response.status_code == 200
    body = response.json()
    assert set(body["records_by_source"]) == {"tax", "base"}
    assert body["unsupported_shapes"] == []
    assert body["records_by_source"]["tax"]["total_count"] == 2


async def test_no_shapes_parameter_returns_every_shape(client, address_id):
    body = (await client.get(f"/v1/address/{address_id}/records")).json()
    assert set(body["records_by_source"]) == {
        "utility", "trace", "base", "loan", "drive", "auto", "tax"
    }


async def test_an_unknown_shape_is_reported_not_ignored(client, address_id):
    body = (await client.get(f"/v1/address/{address_id}/records?shapes=tax,voter")).json()
    assert body["unsupported_shapes"] == ["voter"]
    assert set(body["records_by_source"]) == {"tax"}


async def test_limit_and_offset_page_within_a_shape(client, address_id):
    first = (await client.get(f"/v1/address/{address_id}/records?shapes=trace&limit=1")).json()
    assert first["records_by_source"]["trace"]["total_count"] == 2
    assert first["records_by_source"]["trace"]["has_more"] is True
    assert len(first["records_by_source"]["trace"]["records"]) == 1
    assert first["records_by_source"]["trace"]["records"][0]["__rowid"] == 0

    second = (await client.get(
        f"/v1/address/{address_id}/records?shapes=trace&limit=1&offset=1"
    )).json()
    assert second["records_by_source"]["trace"]["has_more"] is False
    assert second["records_by_source"]["trace"]["records"][0]["__rowid"] == 1


async def test_an_unknown_address_id_is_a_404(client):
    assert (await client.get("/v1/address/987654/records")).status_code == 404


async def test_a_malformed_limit_is_a_400(client, address_id):
    response = await client.get(f"/v1/address/{address_id}/records?limit=ten")
    assert response.status_code == 400
    assert "limit" in response.json()["error"]


async def test_records_survive_a_hot_cache_eviction(client, address_id, service_pool):
    client._transport.app.state.cache.evict_hot(address_id)
    body = (await client.get(f"/v1/address/{address_id}/records?shapes=utility")).json()
    assert body["records_by_source"]["utility"]["total_count"] == 1
```

- [ ] **Step 2: Run the test, verify it fails**

Run: `.venv/bin/python -m pytest tests/test_op_address_records.py -q`
Expected: FAIL — `assert 404 == 200` on the first test (route not registered).

- [ ] **Step 3: Minimal implementation**

Append to `src/occupancy_graph/service/handlers.py`:

```python
from occupancy_graph.service.pagination import page_params


async def address_records(request: Request) -> JSONResponse:
    address_id = int(request.path_params["address_id"])
    bundle = await request.app.state.cache.get(address_id)
    if bundle is None:
        return error(404, f"unknown address_id {address_id}")
    try:
        page = page_params(request.query_params)
    except ValueError as exc:
        return error(400, str(exc))
    shapes, unsupported = records_mod.select_shapes(request.query_params.get("shapes"))
    return ok(
        {
            "records_by_source": {
                shape: records_mod.records_block(bundle.rows_by_shape.get(shape, []), page)
                for shape in shapes
            },
            "unsupported_shapes": unsupported,
        }
    )
```

In `app.py`, add to `routes`:

```python
        Route("/v1/address/{address_id:int}/records", handlers.address_records, methods=["GET"]),
```

- [ ] **Step 4: Run the test, verify it passes**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS — `224 passed` (217 + 7).

- [ ] **Step 5: Commit**

```bash
git add src/occupancy_graph/service/handlers.py src/occupancy_graph/service/app.py \
        tests/test_op_address_records.py
git commit -m "feat: GET /v1/address/{id}/records with shape selection and paging"
```

---

### Task 11: Operation 3 — `GET /v1/address/{id}/people`

**Files:**
- Modify: `src/occupancy_graph/service/handlers.py`, `src/occupancy_graph/service/app.py`
- Test: `tests/test_op_address_people.py`

- [ ] **Step 1: Write the failing test**

`tests/test_op_address_people.py`:

```python
"""Operation 3: GET /v1/address/{id}/people -- name-key clustering over the bundle.

Deliberately NOT the partner hal_id graph: see source/people.py. Company/trust
owners are excluded, so a mailing-elsewhere owner is never manufactured into a
resident.
"""
from __future__ import annotations

import pytest


@pytest.fixture
async def address_id(client) -> int:
    body = (await client.post("/v1/resolve", json={"address": "123 Main St", "zip": "40505"})).json()
    return body["address_id"]


async def test_people_are_clustered_by_name_with_their_sources(client, address_id):
    response = await client.get(f"/v1/address/{address_id}/people")
    assert response.status_code == 200
    body = response.json()
    jane = next(p for p in body["people"] if p["norm_name_key"] == "jane|doe")
    assert jane["firstname"] == "Jane"
    assert jane["lastname"] == "Doe"
    assert jane["full_name"] == "Jane A Doe"
    assert jane["primary_address_id"] == address_id
    assert set(jane["sources"]) >= {"trace", "base", "loan", "auto", "tax"}


async def test_person_ids_are_address_scoped_and_prefixed(client, address_id):
    body = (await client.get(f"/v1/address/{address_id}/people")).json()
    assert all(person["id"].startswith(f"addr:{address_id}:") for person in body["people"])


async def test_company_owners_are_not_people_at_the_address(client, address_id):
    body = (await client.get(f"/v1/address/{address_id}/people")).json()
    assert all(person["lastname"] != "ACME" for person in body["people"])


async def test_the_response_carries_no_internal_row_payload(client, address_id):
    body = (await client.get(f"/v1/address/{address_id}/people")).json()
    assert set(body["people"][0]) == {
        "id", "firstname", "middlename", "lastname", "full_name",
        "norm_name_key", "sources", "primary_address_id",
    }
    assert body["total_count"] == len(body["people"])
    assert body["has_more"] is False


async def test_an_unknown_address_id_is_a_404(client):
    assert (await client.get("/v1/address/987654/people")).status_code == 404
```

- [ ] **Step 2: Run the test, verify it fails**

Run: `.venv/bin/python -m pytest tests/test_op_address_people.py -q`
Expected: FAIL — `assert 404 == 200`.

- [ ] **Step 3: Minimal implementation**

Append to `src/occupancy_graph/service/handlers.py`:

```python
from occupancy_graph.service.pagination import paginate
from occupancy_graph.source.people import people_for_bundle

# The keys a person carries on the wire. `sources` is a set internally and
# `rows` is the internal row list -- neither may leak in that form.
_PERSON_KEYS = (
    "id", "firstname", "middlename", "lastname", "full_name",
    "norm_name_key", "sources", "primary_address_id",
)


def _public_person(person: dict[str, Any]) -> dict[str, Any]:
    out = {key: person.get(key) for key in _PERSON_KEYS}
    out["sources"] = sorted(person.get("sources") or ())
    return out


async def address_people(request: Request) -> JSONResponse:
    address_id = int(request.path_params["address_id"])
    bundle = await request.app.state.cache.get(address_id)
    if bundle is None:
        return error(404, f"unknown address_id {address_id}")
    try:
        page = page_params(request.query_params)
    except ValueError as exc:
        return error(400, str(exc))
    people = [_public_person(person) for person in people_for_bundle(bundle)]
    return ok(paginate(people, page, key="people"))
```

In `app.py`, add to `routes`:

```python
        Route("/v1/address/{address_id:int}/people", handlers.address_people, methods=["GET"]),
```

- [ ] **Step 4: Run the test, verify it passes**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS — `229 passed` (224 + 5).

- [ ] **Step 5: Commit**

```bash
git add src/occupancy_graph/service/handlers.py src/occupancy_graph/service/app.py \
        tests/test_op_address_people.py
git commit -m "feat: GET /v1/address/{id}/people over the bundle name-key clusters"
```

---

### Task 12: Operation 4a — `GET /v1/person/{id}/records` for `addr:` ids

**Files:**
- Modify: `src/occupancy_graph/service/handlers.py`, `src/occupancy_graph/service/app.py`
- Test: `tests/test_op_person_records.py`

- [ ] **Step 1: Write the failing test**

`tests/test_op_person_records.py`:

```python
"""Operation 4: GET /v1/person/{id}/records.

Ids are discriminated: `addr:<addressId>:<n>` is served from the bundle,
`hal:<hal_id>` from entity_links. The hal: half lands in the next task.
"""
from __future__ import annotations

import pytest


@pytest.fixture
async def address_id(client) -> int:
    body = (await client.post("/v1/resolve", json={"address": "123 Main St", "zip": "40505"})).json()
    return body["address_id"]


@pytest.fixture
async def jane_addr_id(client, address_id) -> str:
    body = (await client.get(f"/v1/address/{address_id}/people")).json()
    return next(p["id"] for p in body["people"] if p["norm_name_key"] == "jane|doe")


async def test_an_addr_person_returns_only_that_persons_rows(client, jane_addr_id):
    response = await client.get(f"/v1/person/{jane_addr_id}/records?shapes=trace,base")
    assert response.status_code == 200
    body = response.json()
    assert body["records_by_source"]["trace"]["total_count"] == 1
    assert body["records_by_source"]["trace"]["records"][0]["firstname"] == "Jane"
    assert body["records_by_source"]["base"]["total_count"] == 1


async def test_an_addr_person_carries_null_er_metadata(client, jane_addr_id):
    body = (await client.get(f"/v1/person/{jane_addr_id}/records")).json()
    assert body["person"]["id"] == jane_addr_id
    assert body["person"]["firstname"] == "Jane"
    assert body["person"]["lastname"] == "Doe"
    # The bundle path has no ER graph behind it. The keys are present with null
    # values so the payload shape is identical for both id kinds.
    assert body["person"]["identity_confidence"] is None
    assert body["person"]["is_suspicious"] is None


async def test_unknown_shapes_are_reported(client, jane_addr_id):
    body = (await client.get(f"/v1/person/{jane_addr_id}/records?shapes=tax,voter")).json()
    assert body["unsupported_shapes"] == ["voter"]


async def test_an_unknown_addr_person_is_a_404(client, address_id):
    assert (await client.get(f"/v1/person/addr:{address_id}:99/records")).status_code == 404
    assert (await client.get("/v1/person/addr:987654:0/records")).status_code == 404


async def test_a_malformed_person_id_is_a_400(client):
    for bad in ("nonsense", "addr:x:y", "addr:1"):
        response = await client.get(f"/v1/person/{bad}/records")
        assert response.status_code == 400, bad
        assert "person id" in response.json()["error"]


async def test_records_from_the_bundle_carry_a_rowid(client, jane_addr_id):
    body = (await client.get(f"/v1/person/{jane_addr_id}/records?shapes=base")).json()
    assert body["records_by_source"]["base"]["records"][0]["__rowid"] == 0
```

- [ ] **Step 2: Run the test, verify it fails**

Run: `.venv/bin/python -m pytest tests/test_op_person_records.py -q`
Expected: FAIL — `assert 404 == 200`.

- [ ] **Step 3: Minimal implementation**

Append to `src/occupancy_graph/service/handlers.py`:

```python
from occupancy_graph.source.people import PERSON_ID_PREFIX
from occupancy_graph.source.search import HAL_ID_PREFIX


def _parse_addr_person_id(person_id: str) -> tuple[int, int] | None:
    """`addr:<addressId>:<n>` -> (address_id, index), or None if malformed."""
    parts = person_id.split(":")
    if len(parts) != 3:
        return None
    try:
        return int(parts[1]), int(parts[2])
    except ValueError:
        return None


async def person_records(request: Request) -> JSONResponse:
    person_id = str(request.path_params["person_id"])
    try:
        page = page_params(request.query_params)
    except ValueError as exc:
        return error(400, str(exc))
    shapes, unsupported = records_mod.select_shapes(request.query_params.get("shapes"))

    if person_id.startswith(PERSON_ID_PREFIX):
        return await _addr_person_records(request, person_id, page, shapes, unsupported)
    if person_id.startswith(HAL_ID_PREFIX):
        return error(501, "hal: traversal not implemented")   # replaced in Task 13
    return error(400, f"malformed person id {person_id!r}; expected addr:<n>:<n> or hal:<id>")


async def _addr_person_records(request, person_id, page, shapes, unsupported) -> JSONResponse:
    parsed = _parse_addr_person_id(person_id)
    if parsed is None:
        return error(400, f"malformed person id {person_id!r}; expected addr:<n>:<n> or hal:<id>")
    address_id, index = parsed

    bundle = await request.app.state.cache.get(address_id)
    if bundle is None:
        return error(404, f"unknown address_id {address_id}")
    people = people_for_bundle(bundle)
    if not 0 <= index < len(people):
        return error(404, f"unknown person id {person_id}")
    person = people[index]

    # `rows` on a clustered person is [(shape, row)]. The row objects are the
    # SAME dicts as bundle.rows_by_shape[shape], so index() recovers the bundle
    # position that GET /v1/source-record takes -- identity, not equality, so
    # two identical rows at one address stay distinguishable.
    by_shape: dict[str, list[dict[str, Any]]] = {shape: [] for shape in shapes}
    for shape, row in person["rows"]:
        if shape in by_shape:
            by_shape[shape].append(row)

    blocks = {}
    for shape in shapes:
        full = bundle.rows_by_shape.get(shape, [])
        rows = [
            {**row, "__rowid": next(
                (i for i, candidate in enumerate(full) if candidate is row), None
            )}
            for row in by_shape[shape]
        ]
        blocks[shape] = records_mod.records_block(rows, page, with_rowid=False)

    return ok(
        {
            "person": {
                "id": person_id,
                "firstname": person.get("firstname"),
                "lastname": person.get("lastname"),
                "identity_confidence": None,
                "is_suspicious": None,
            },
            "records_by_source": blocks,
            "records_timed_out": False,
            "unsupported_shapes": unsupported,
        }
    )
```

In `app.py`, add to `routes`:

```python
        Route("/v1/person/{person_id}/records", handlers.person_records, methods=["GET"]),
```

- [ ] **Step 4: Run the test, verify it passes**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS — `235 passed` (229 + 6).

- [ ] **Step 5: Commit**

```bash
git add src/occupancy_graph/service/handlers.py src/occupancy_graph/service/app.py \
        tests/test_op_person_records.py
git commit -m "feat: GET /v1/person/{id}/records for bundle-scoped addr: ids"
```

---

### Task 13: Operation 4b — the `hal:` traversal

**This is the task the previous plan stubbed.** Without it, owner-elsewhere detection — the single strongest use case this database supports — is silently broken.

**Files:**
- Modify: `src/occupancy_graph/service/handlers.py`
- Test: `tests/test_op_person_records.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_op_person_records.py`:

```python
# --- hal: traversal. entity_links -> the underlying partner rows, projected
# --- through the same manifest the address scan uses. Owner-elsewhere depends
# --- on this path existing.


async def test_a_hal_person_returns_the_linked_rows_projected_by_shape(client):
    response = await client.get("/v1/person/hal:HAL0001/records")
    assert response.status_code == 200
    body = response.json()
    assert body["records_by_source"]["trace"]["total_count"] == 1
    assert body["records_by_source"]["trace"]["records"][0]["firstname"] == "Jane"
    assert body["records_by_source"]["base"]["total_count"] == 1
    assert body["records_by_source"]["loan"]["total_count"] == 1
    assert body["records_by_source"]["loan"]["records"][0]["employer"] == "ACME"


async def test_a_payday_row_reached_by_hal_id_is_both_loan_and_drive(client):
    body = (await client.get("/v1/person/hal:HAL0001/records")).json()
    assert body["records_by_source"]["drive"]["total_count"] == 1
    assert body["records_by_source"]["drive"]["records"][0]["dl_num"] == "A12345678"


async def test_identity_confidence_and_is_suspicious_are_surfaced(client):
    body = (await client.get("/v1/person/hal:HAL0001/records")).json()
    assert body["person"] == {
        "id": "hal:HAL0001",
        "firstname": "JANE",
        "lastname": "DOE",
        "identity_confidence": 40.5,
        "is_suspicious": False,
    }


async def test_a_suspicious_entity_is_flagged_not_suppressed(client):
    body = (await client.get("/v1/person/hal:HAL0002/records")).json()
    assert body["person"]["is_suspicious"] is True
    assert body["person"]["identity_confidence"] == 88.0
    assert body["records_by_source"]["trace"]["total_count"] == 1


async def test_hal_records_are_filtered_by_the_shapes_parameter(client):
    body = (await client.get("/v1/person/hal:HAL0001/records?shapes=loan,voter")).json()
    assert set(body["records_by_source"]) == {"loan"}
    assert body["unsupported_shapes"] == ["voter"]


async def test_hal_records_carry_no_rowid_because_they_are_not_bundle_scoped(client):
    body = (await client.get("/v1/person/hal:HAL0001/records")).json()
    assert "__rowid" not in body["records_by_source"]["loan"]["records"][0]


async def test_hal_records_report_the_timeout_flag(client):
    body = (await client.get("/v1/person/hal:HAL0001/records")).json()
    assert body["records_timed_out"] is False


async def test_an_unknown_hal_id_is_a_404(client):
    assert (await client.get("/v1/person/hal:HAL9999/records")).status_code == 404
```

- [ ] **Step 2: Run the test, verify it fails**

Run: `.venv/bin/python -m pytest tests/test_op_person_records.py -q`
Expected: FAIL — `assert 501 == 200` (the stub from Task 12).

- [ ] **Step 3: Minimal implementation**

In `src/occupancy_graph/service/handlers.py`, replace the `501` line in `person_records` with:

```python
    if person_id.startswith(HAL_ID_PREFIX):
        return await _hal_person_records(request, person_id, page, shapes, unsupported)
```

and append:

```python
from occupancy_graph.source import quality, search
from occupancy_graph.source.feeds import shapes_for_row
from occupancy_graph.source.manifest import SHAPES
from occupancy_graph.source.project import project_row


async def _hal_person_records(request, person_id, page, shapes, unsupported) -> JSONResponse:
    """Owner-elsewhere traversal: entity_links -> partner rows -> projections.

    Rows reached this way carry no shape label -- the forward scan knew the
    shape because it chose the predicate. shapes_for_row reverses the
    source_file predicates, and one payday row legitimately yields BOTH `loan`
    and `drive`, exactly as the address scan does.
    """
    pool = request.app.state.pool
    hal_id = person_id[len(HAL_ID_PREFIX):]

    person = await search.person_for_hal_id(pool, hal_id)
    if person is None:
        return error(404, f"unknown person id {person_id}")

    links = await search.records_for_hal_id(pool, hal_id)
    rows, timed_out = await search.rows_for_links(pool, links)

    by_shape: dict[str, list[dict[str, Any]]] = {shape: [] for shape in shapes}
    for row in rows:
        for shape in shapes_for_row(row):
            if shape not in by_shape:
                continue
            # The same fail-closed gate the address scan applies: a
            # column-shifted property_owner row must never reach the model.
            if shape == "tax" and not quality.tax_row_is_usable(row):
                continue
            by_shape[shape].append(project_row(SHAPES[shape], row))

    return ok(
        {
            "person": {
                "id": person_id,
                "firstname": person["canonical_first_name"],
                "lastname": person["canonical_last_name"],
                # The partner ER graph is 17.9% suspicious, peaks at confidence
                # 40.50 and never applies its computed merges. These two fields
                # are how the model discounts it, so they are never omitted.
                "identity_confidence": person["identity_confidence"],
                "is_suspicious": person["is_suspicious"],
            },
            "records_by_source": {
                shape: records_mod.records_block(by_shape[shape], page, with_rowid=False)
                for shape in shapes
            },
            # record_id is not indexed on the partner records tables. An empty
            # result must never be read as "this person has no records" when it
            # actually means "the lookup ran out of time".
            "records_timed_out": timed_out,
            "unsupported_shapes": unsupported,
        }
    )
```

- [ ] **Step 4: Run the test, verify it passes**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS — `243 passed` (235 + 8).

- [ ] **Step 5: Commit**

```bash
git add src/occupancy_graph/service/handlers.py tests/test_op_person_records.py
git commit -m "feat: hal: person traversal over entity_links with ER metadata surfaced"
```

---

### Task 14: Operation 5 — `GET /v1/people/search`

**Files:**
- Modify: `src/occupancy_graph/service/handlers.py`, `src/occupancy_graph/service/app.py`
- Test: `tests/test_op_people_search.py`

- [ ] **Step 1: Write the failing test**

`tests/test_op_people_search.py`:

```python
"""Operation 5: GET /v1/people/search?name=&limit= over silver.entity_master."""
from __future__ import annotations


async def test_search_returns_a_hal_scoped_result_with_its_address(client):
    response = await client.get("/v1/people/search?name=Jane%20Doe")
    assert response.status_code == 200
    body = response.json()
    assert body["total_count"] == 1
    assert body["has_more"] is False
    result = body["results"][0]
    assert result["id"] == "hal:HAL0001"
    assert result["firstname"] == "JANE"
    assert result["lastname"] == "DOE"
    assert result["full_name"] == "JANE DOE"
    assert result["match_score"] == 1.0
    assert result["record_count"] == 3
    assert result["address_line1"] == "123 MAIN ST"
    assert result["city"] == "LEXINGTON"
    assert result["state"] == "KY"
    assert result["zip"] == "40505"


async def test_er_metadata_is_on_every_result(client):
    body = (await client.get("/v1/people/search?name=Jane%20Doe")).json()
    assert body["results"][0]["identity_confidence"] == 40.5
    assert body["results"][0]["is_suspicious"] is False


async def test_a_suspicious_entity_is_returned_flagged(client):
    body = (await client.get("/v1/people/search?name=John%20Smith")).json()
    assert body["results"][0]["is_suspicious"] is True
    assert body["results"][0]["identity_confidence"] == 88.0


async def test_a_surname_only_query_scores_lower(client):
    body = (await client.get("/v1/people/search?name=Doe")).json()
    assert body["results"][0]["match_score"] == 0.6


async def test_an_unknown_name_returns_an_empty_result_not_an_error(client):
    body = (await client.get("/v1/people/search?name=Nobody%20Here")).json()
    assert body == {"total_count": 0, "has_more": False, "results": []}


async def test_a_missing_name_is_a_400(client):
    response = await client.get("/v1/people/search")
    assert response.status_code == 400
    assert "name" in response.json()["error"]


async def test_has_more_reflects_the_true_total_not_the_page(client):
    body = (await client.get("/v1/people/search?name=Doe&limit=1")).json()
    assert body["total_count"] == 1
    assert body["has_more"] is False
```

- [ ] **Step 2: Run the test, verify it fails**

Run: `.venv/bin/python -m pytest tests/test_op_people_search.py -q`
Expected: FAIL — `assert 404 == 200`.

- [ ] **Step 3: Minimal implementation**

Append to `src/occupancy_graph/service/handlers.py`:

```python
from occupancy_graph.service.limits import DEFAULT_PAGE_LIMIT


def _search_result(entity: dict[str, Any]) -> dict[str, Any]:
    first = entity["canonical_first_name"] or ""
    last = entity["canonical_last_name"] or ""
    return {
        "id": f"{HAL_ID_PREFIX}{entity['hal_id']}",
        "firstname": entity["canonical_first_name"],
        "lastname": entity["canonical_last_name"],
        "full_name": " ".join(part for part in (first, last) if part) or None,
        "match_score": entity["match_score"],
        "record_count": entity["record_count"],
        "identity_confidence": entity["identity_confidence"],
        "is_suspicious": entity["is_suspicious"],
        "address_line1": entity["canonical_address_line1"],
        "city": entity["canonical_city"],
        "state": entity["canonical_state"],
        "zip": entity["canonical_zip"],
    }


async def people_search(request: Request) -> JSONResponse:
    name = request.query_params.get("name") or ""
    if not name.strip():
        return error(400, "name is required")
    try:
        page = page_params(request.query_params, default_limit=DEFAULT_PAGE_LIMIT)
    except ValueError as exc:
        return error(400, str(exc))

    total, entities = await search.search_people(request.app.state.pool, name, limit=page.limit)
    results = [_search_result(entity) for entity in entities]
    return ok(
        {
            "total_count": total,
            "has_more": len(results) < total,
            "results": results,
        }
    )
```

In `app.py`, add to `routes` **before** the `/v1/person/...` route (distinct prefixes, but keep the read order obvious):

```python
        Route("/v1/people/search", handlers.people_search, methods=["GET"]),
```

- [ ] **Step 4: Run the test, verify it passes**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS — `250 passed` (243 + 7).

- [ ] **Step 5: Commit**

```bash
git add src/occupancy_graph/service/handlers.py src/occupancy_graph/service/app.py \
        tests/test_op_people_search.py
git commit -m "feat: GET /v1/people/search over entity_master with ER metadata"
```

---

### Task 15: Operation 6 — `GET /v1/source-record/{shape}/{rowid}`

**Files:**
- Modify: `src/occupancy_graph/service/handlers.py`, `src/occupancy_graph/service/app.py`
- Test: `tests/test_op_source_record.py`

- [ ] **Step 1: Write the failing test**

`tests/test_op_source_record.py`:

```python
"""Operation 6: GET /v1/source-record/{shape}/{rowid}?address_id= -- provenance.

`rowid` is a positional index inside a bundle (Contract B pins rowid 0 next to
record_id "4001"), and a position is only meaningful relative to an address, so
address_id is a required query parameter. The response body is exactly as
pinned.
"""
from __future__ import annotations

import pytest


@pytest.fixture
async def address_id(client) -> int:
    body = (await client.post("/v1/resolve", json={"address": "123 Main St", "zip": "40505"})).json()
    return body["address_id"]


async def test_provenance_for_a_tax_row(client, address_id):
    response = await client.get(f"/v1/source-record/tax/0?address_id={address_id}")
    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "tax"
    assert body["table"] == "tax"
    assert body["rowid"] == 0
    assert body["record_id"] == "4001"
    assert body["summary"].startswith("tax; ownername=DOE, JANE ANN;")
    assert body["data"]["ownerstate"] == "IL"


async def test_the_summary_leads_with_the_field_that_matters_for_the_shape(client, address_id):
    body = (await client.get(f"/v1/source-record/utility/0?address_id={address_id}")).json()
    assert body["summary"].startswith("utility; first_name=Pat; last_name=Tenant;")


async def test_a_rowid_past_the_end_is_a_404(client, address_id):
    assert (await client.get(f"/v1/source-record/tax/99?address_id={address_id}")).status_code == 404


async def test_an_unknown_shape_is_a_404(client, address_id):
    response = await client.get(f"/v1/source-record/voter/0?address_id={address_id}")
    assert response.status_code == 404
    assert "voter" in response.json()["error"]


async def test_a_missing_address_id_is_a_400_naming_the_parameter(client):
    response = await client.get("/v1/source-record/tax/0")
    assert response.status_code == 400
    assert "address_id" in response.json()["error"]


async def test_an_unknown_address_id_is_a_404(client):
    assert (await client.get("/v1/source-record/tax/0?address_id=987654")).status_code == 404
```

- [ ] **Step 2: Run the test, verify it fails**

Run: `.venv/bin/python -m pytest tests/test_op_source_record.py -q`
Expected: FAIL — `assert 404 == 200`.

- [ ] **Step 3: Minimal implementation**

Append to `src/occupancy_graph/service/handlers.py`:

```python
async def source_record(request: Request) -> JSONResponse:
    shape = str(request.path_params["shape"])
    rowid = int(request.path_params["rowid"])

    raw_address_id = request.query_params.get("address_id")
    if not raw_address_id:
        return error(
            400,
            "address_id is required: rowid is a position within one address's "
            "rows for this shape, so it cannot be resolved without the address",
        )
    try:
        address_id = int(raw_address_id)
    except ValueError:
        return error(400, f"address_id must be an integer, got {raw_address_id!r}")

    if shape not in SHAPES:
        return error(404, f"unknown shape {shape!r}; this corpus serves {sorted(SHAPES)}")

    bundle = await request.app.state.cache.get(address_id)
    if bundle is None:
        return error(404, f"unknown address_id {address_id}")

    rows = bundle.rows_by_shape.get(shape, [])
    if not 0 <= rowid < len(rows):
        return error(404, f"no {shape} row at rowid {rowid} for address {address_id}")

    row = rows[rowid]
    return ok(
        {
            "source": shape,
            # The partner corpus is one physical table; `table` names the SHAPE,
            # which is what provenance means to the consumer.
            "table": shape,
            "rowid": rowid,
            # Every id-linked shape derives its id from record_id, which is
            # unique across the corpus.
            "record_id": row.get("id") or row.get(f"{shape}_id"),
            "summary": records_mod.summarize(shape, row),
            "data": dict(row),
        }
    )
```

In `app.py`, add to `routes`:

```python
        Route("/v1/source-record/{shape}/{rowid:int}", handlers.source_record, methods=["GET"]),
```

> Note: `utility` and `base` have no `id`/`<shape>_id` field in the manifest (`utility` has neither; `base` has `id`). For `utility` the `record_id` key is `null`, which is honest — the utility feed carries no id in the contract.

- [ ] **Step 4: Run the test, verify it passes**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS — `256 passed` (250 + 6).

- [ ] **Step 5: Commit**

```bash
git add src/occupancy_graph/service/handlers.py src/occupancy_graph/service/app.py \
        tests/test_op_source_record.py
git commit -m "feat: GET /v1/source-record provenance with per-shape summary lines"
```

---

### Task 16: SQL hatch stage 1 — the parse guard (the primary write guard)

**This is not defence in depth.** `tests/test_pool.py::test_read_only_is_a_session_default_not_a_boundary` proves that `BEGIN READ WRITE` defeats `default_transaction_read_only` and the write commits. That was acceptable when we controlled every call site. It is not once the agent writes SQL. **Nothing downstream of this stage is a write control.**

**Design.** A hand-written lexer, not a SQL parser library, and deliberately so: the guard's whole value is that it can be reasoned about completely. It strips comments, string literals, E-strings, dollar-quoted bodies and quoted identifiers in one left-to-right pass, then applies three rules to what remains — one statement, a `SELECT`/`WITH` head, and **no statement keyword anywhere**. The "anywhere" rule is strictly more conservative than an AST walk and covers DML-in-CTE without needing to model CTEs. Every mis-parse fails toward refusal.

**Files:**
- Create: `src/occupancy_graph/service/sql_guard.py`
- Test: `tests/test_sql_guard.py`

- [ ] **Step 1: Write the failing test**

`tests/test_sql_guard.py`:

```python
"""The parse guard. Adversarial by design.

Task 11's review proved BEGIN READ WRITE defeats default_transaction_read_only
and the write commits (tests/test_pool.py). Nothing downstream of this stage is
a write control, so every attack shape below must be refused HERE.
"""
from __future__ import annotations

import pytest

from occupancy_graph.service.sql_guard import SqlRefused, parse


def refuse(query: str) -> SqlRefused:
    with pytest.raises(SqlRefused) as caught:
        parse(query)
    assert caught.value.stage == "parse"
    return caught.value


def test_a_plain_select_is_accepted():
    assert parse("SELECT record_id FROM records_legacy LIMIT 5") == (
        "SELECT record_id FROM records_legacy LIMIT 5"
    )


def test_a_with_cte_select_is_accepted():
    query = "WITH z AS (SELECT 1 AS n) SELECT n FROM z"
    assert parse(query) == query


def test_a_trailing_semicolon_is_stripped_not_refused():
    assert parse("SELECT 1;  ") == "SELECT 1"


def test_an_empty_query_is_refused():
    assert "empty" in refuse("   ").reason


def test_a_non_select_first_keyword_is_refused():
    assert "SELECT or WITH" in refuse("EXPLAIN SELECT 1").reason


def test_chained_statements_are_refused():
    assert "one statement" in refuse("SELECT 1; SELECT 2").reason
    refuse("SELECT 1; DROP TABLE records_legacy")


def test_a_semicolon_inside_a_string_literal_is_not_a_chain():
    query = "SELECT * FROM records_legacy WHERE employer = 'ACME; DROP TABLE t'"
    assert parse(query) == query


def test_a_semicolon_inside_a_quoted_identifier_is_not_a_chain():
    query = 'SELECT 1 AS ";"'
    assert parse(query) == query


def test_a_semicolon_inside_a_dollar_quoted_literal_is_not_a_chain():
    query = "SELECT $tag$a; DROP TABLE t$tag$ AS s"
    assert parse(query) == query


def test_a_line_comment_cannot_hide_a_second_statement():
    refuse("SELECT 1 --\n; DROP TABLE records_legacy")


def test_a_block_comment_cannot_hide_a_write():
    refuse("SELECT 1 /* x */ ; INSERT INTO t VALUES (1)")
    # A comment that merely LOOKS like a write is harmless and must not refuse.
    assert parse("SELECT 1 /* not an INSERT */") == "SELECT 1 /* not an INSERT */"


def test_nested_block_comments_are_handled():
    assert parse("SELECT 1 /* a /* b */ c */") == "SELECT 1 /* a /* b */ c */"
    assert "unterminated" in refuse("SELECT 1 /* a /* b */").reason


def test_insert_inside_a_cte_is_refused():
    assert "INSERT" in refuse(
        "WITH w AS (INSERT INTO records_legacy (record_id) VALUES (1) RETURNING record_id) "
        "SELECT * FROM w"
    ).reason


def test_update_inside_a_cte_is_refused():
    refuse("WITH w AS (UPDATE records_legacy SET zip = '0' RETURNING zip) SELECT * FROM w")


def test_delete_inside_a_cte_is_refused():
    refuse("WITH w AS (DELETE FROM records_legacy RETURNING record_id) SELECT * FROM w")


def test_begin_read_write_is_refused():
    assert "BEGIN" in refuse("BEGIN READ WRITE").reason
    refuse("SELECT 1; BEGIN READ WRITE; INSERT INTO t VALUES (1)")


def test_commit_and_rollback_are_refused():
    refuse("COMMIT")
    refuse("SELECT 1; ROLLBACK")


def test_set_is_refused():
    refuse("SET default_transaction_read_only = off")
    refuse("SELECT 1; SET statement_timeout = 0")


def test_copy_is_refused():
    refuse("COPY records_legacy TO '/tmp/x.csv'")


def test_do_block_is_refused():
    refuse("DO $$ BEGIN PERFORM 1; END $$")


def test_call_is_refused():
    refuse("CALL some_procedure()")


def test_grant_and_alter_are_refused():
    refuse("GRANT ALL ON records_legacy TO PUBLIC")
    refuse("ALTER TABLE records_legacy ADD COLUMN x int")


def test_select_into_is_refused():
    assert "INTO" in refuse("SELECT * INTO copy_of_records FROM records_legacy").reason


def test_an_unterminated_string_literal_is_refused():
    assert "unterminated" in refuse("SELECT 'abc").reason


def test_blocked_functions_are_refused():
    assert "pg_read_file" in refuse("SELECT pg_read_file('/etc/passwd')").reason
    refuse("SELECT dblink('host=evil', 'SELECT 1')")
    refuse("SELECT pg_sleep(3600)")
    refuse("SELECT query_to_xml('SELECT 1', true, true, '')")
```

- [ ] **Step 2: Run the test, verify it fails**

Run: `.venv/bin/python -m pytest tests/test_sql_guard.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'occupancy_graph.service.sql_guard'`.

- [ ] **Step 3: Minimal implementation**

`src/occupancy_graph/service/sql_guard.py`:

```python
"""Stage 1 of the hatch guard: the PRIMARY write control.

Not defence in depth. `default_transaction_read_only` is a session default that
raw `BEGIN READ WRITE` defeats -- proven in
tests/test_pool.py::test_read_only_is_a_session_default_not_a_boundary, where
the INSERT commits. That was acceptable while we controlled every call site; it
is not once the agent writes SQL. Nothing downstream of this module is a write
control.

WHY A HAND-WRITTEN LEXER, NOT A SQL PARSER LIBRARY. The value of this guard is
that it can be reasoned about completely. It has three rules -- one statement, a
SELECT/WITH head, and no statement keyword ANYWHERE -- applied to text with all
comments and literals removed. The "anywhere" rule is strictly more conservative
than an AST walk (it also refuses `WHERE col = 'INSERT'` written without quotes,
which is not valid SQL anyway) and it covers DML-in-CTE without modelling CTEs.
Every failure mode of the lexer fails toward refusal: an unterminated literal is
refused, and text mistakenly treated as code hits the keyword rule.

False positives are acceptable and observable -- the refusal names the exact
keyword. A false negative is not.
"""
from __future__ import annotations

import re

# Keywords refused ANYWHERE in the statement, not merely at the head. INSERT /
# UPDATE / DELETE / MERGE are the four statements Postgres permits inside a CTE
# and are the reason this rule is positional-independent; the rest cannot reach
# a CTE but cost nothing to deny and close off any chaining the ';' rule misses.
# INTO blocks `SELECT ... INTO newtable`. RETURNING cannot appear in a plain
# SELECT, so denying it is free and is a second signal on DML-in-CTE.
_STATEMENT_KEYWORDS = frozenset({
    "INSERT", "UPDATE", "DELETE", "MERGE", "TRUNCATE", "INTO", "RETURNING",
    "CREATE", "ALTER", "DROP", "GRANT", "REVOKE", "COPY", "DO", "CALL",
    "BEGIN", "COMMIT", "ROLLBACK", "START", "SAVEPOINT", "RELEASE",
    "SET", "RESET", "VACUUM", "ANALYZE", "CLUSTER", "REINDEX", "REFRESH",
    "LOCK", "PREPARE", "EXECUTE", "DEALLOCATE", "DISCARD", "LISTEN",
    "UNLISTEN", "NOTIFY", "IMPORT", "EXPLAIN",
})

# Read-only in name only: these reach the filesystem, open new connections that
# are not bound by our session settings, execute a nested query string, or hold
# a resource past the statement timeout.
_BLOCKED_FUNCTIONS = frozenset({
    "pg_read_file", "pg_read_binary_file", "pg_ls_dir", "pg_stat_file",
    "lo_import", "lo_export", "lo_get", "lo_put",
    "dblink", "dblink_exec", "dblink_connect", "dblink_send_query",
    "pg_sleep", "pg_sleep_for", "pg_sleep_until",
    "pg_terminate_backend", "pg_cancel_backend", "pg_reload_conf",
    "set_config", "pg_logical_emit_message",
    "query_to_xml", "query_to_xmlschema", "query_to_xml_and_xmlschema",
    "pg_advisory_lock", "pg_advisory_xact_lock",
})

_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")
_DOLLAR_TAG = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)?\$")


class SqlRefused(Exception):
    """A query refused BEFORE execution. `stage` is "parse" or "explain"."""

    def __init__(self, stage: str, reason: str, hint: str = "") -> None:
        super().__init__(reason)
        self.stage = stage
        self.reason = reason
        self.hint = hint


def strip_literals(sql: str) -> str:
    """Replace every comment, string literal, dollar-quoted body and quoted
    identifier with an inert placeholder, in ONE left-to-right pass.

    A single pass is required, not sequential regex passes: `/* ' */` must be
    recognised as a comment before the quote scanner sees it, and `'-- '` as a
    literal before the comment scanner does. Anything unterminated raises --
    ambiguity is refused, never guessed.
    """
    out: list[str] = []
    index = 0
    length = len(sql)

    while index < length:
        char = sql[index]

        if sql.startswith("--", index):
            newline = sql.find("\n", index)
            index = length if newline == -1 else newline
            out.append(" ")
            continue

        if sql.startswith("/*", index):
            depth = 1
            index += 2
            while index < length and depth:
                if sql.startswith("/*", index):
                    depth += 1
                    index += 2
                elif sql.startswith("*/", index):
                    depth -= 1
                    index += 2
                else:
                    index += 1
            if depth:
                raise SqlRefused("parse", "unterminated block comment")
            out.append(" ")
            continue

        # E'...' escape strings: backslash escapes a quote, unlike a standard
        # literal under standard_conforming_strings.
        is_estring = (
            char in "Ee"
            and index + 1 < length
            and sql[index + 1] == "'"
            and (index == 0 or not (sql[index - 1].isalnum() or sql[index - 1] == "_"))
        )
        if is_estring or char == "'":
            index += 2 if is_estring else 1
            closed = False
            while index < length:
                if is_estring and sql[index] == "\\" and index + 1 < length:
                    index += 2
                    continue
                if sql[index] == "'":
                    if index + 1 < length and sql[index + 1] == "'":
                        index += 2
                        continue
                    index += 1
                    closed = True
                    break
                index += 1
            if not closed:
                raise SqlRefused("parse", "unterminated string literal")
            out.append(" '' ")
            continue

        if char == '"':
            index += 1
            closed = False
            while index < length:
                if sql[index] == '"':
                    if index + 1 < length and sql[index + 1] == '"':
                        index += 2
                        continue
                    index += 1
                    closed = True
                    break
                index += 1
            if not closed:
                raise SqlRefused("parse", "unterminated quoted identifier")
            out.append(' "x" ')
            continue

        if char == "$":
            tag_match = _DOLLAR_TAG.match(sql, index)
            if tag_match:
                tag = tag_match.group(0)
                end = sql.find(tag, tag_match.end())
                if end == -1:
                    raise SqlRefused("parse", "unterminated dollar-quoted string")
                index = end + len(tag)
                out.append(" '' ")
                continue

        out.append(char)
        index += 1

    return "".join(out)


def parse(query: str) -> str:
    """Refuse anything that is not exactly one read-only SELECT.

    Returns the original query with a trailing semicolon removed, ready for
    stage 2. Raises SqlRefused(stage="parse") otherwise.
    """
    text = (query or "").strip()
    if not text:
        raise SqlRefused("parse", "empty query")

    stripped = strip_literals(text).rstrip()

    # 1. Exactly one statement. A ';' inside a literal or comment is already
    #    gone, so any ';' left other than a single trailing one is a chain.
    body = stripped[:-1].rstrip() if stripped.endswith(";") else stripped
    if ";" in body:
        raise SqlRefused(
            "parse", "only one statement is permitted; a ';' separator was found"
        )

    # 2. The statement must be a SELECT (a leading WITH is a CTE chain onto one).
    head = _WORD.search(body)
    keyword = head.group(0).upper() if head else ""
    if keyword not in {"SELECT", "WITH"}:
        raise SqlRefused(
            "parse", f"query must begin with SELECT or WITH, got {keyword or '<nothing>'!r}"
        )

    # 3. No statement keyword and no blocked function ANYWHERE.
    for match in _WORD.finditer(body):
        word = match.group(0)
        if word.upper() in _STATEMENT_KEYWORDS:
            raise SqlRefused(
                "parse",
                f"keyword {word.upper()} is not permitted in a read-only query",
            )
        if word.lower() in _BLOCKED_FUNCTIONS:
            raise SqlRefused("parse", f"function {word.lower()} is not permitted")

    if stripped.endswith(";") and text.endswith(";"):
        text = text[:-1].rstrip()
    return text
```

- [ ] **Step 4: Run the test, verify it passes**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS — `281 passed` (256 + 25).

- [ ] **Step 5: Commit**

```bash
git add src/occupancy_graph/service/sql_guard.py tests/test_sql_guard.py
git commit -m "feat: SQL hatch parse guard -- the primary write control"
```

---

### Task 17: SQL hatch stage 2 — inject and cap the `LIMIT`

**Files:**
- Modify: `src/occupancy_graph/service/sql_guard.py`
- Test: `tests/test_sql_guard.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_sql_guard.py`:

```python
# --- Stage 2: the row cap. Wrapping rather than rewriting: a textual LIMIT
# --- rewrite has to understand the query, and the whole point of stage 1 is
# --- that we do not have to.

from occupancy_graph.service.sql_guard import wrap_with_limit  # noqa: E402


def test_a_query_without_a_limit_gets_one():
    assert wrap_with_limit("SELECT 1", cap=50) == (
        "SELECT * FROM (\nSELECT 1\n) AS _hatch\nLIMIT 50"
    )


def test_a_supplied_limit_is_capped_by_the_outer_one():
    wrapped = wrap_with_limit("SELECT 1 LIMIT 100000", cap=50)
    assert wrapped.endswith("LIMIT 50")
    assert "LIMIT 100000" in wrapped


def test_a_trailing_line_comment_cannot_eat_the_closing_paren():
    wrapped = wrap_with_limit("SELECT 1 -- note", cap=50)
    assert "\n) AS _hatch" in wrapped
    assert wrapped.splitlines()[-2] == ") AS _hatch"


def test_the_cap_is_coerced_to_an_int_so_no_text_reaches_the_sql():
    assert wrap_with_limit("SELECT 1", cap=True).endswith("LIMIT 1")
    with pytest.raises((ValueError, TypeError)):
        wrap_with_limit("SELECT 1", cap="50; DROP TABLE t")


def test_a_cte_survives_the_wrap():
    wrapped = wrap_with_limit("WITH z AS (SELECT 1 AS n) SELECT n FROM z", cap=10)
    assert wrapped.startswith("SELECT * FROM (\nWITH z AS")


async def test_the_wrapped_form_actually_runs(fixture_pool):
    wrapped = wrap_with_limit("SELECT record_id FROM public.records_legacy", cap=2)
    async with fixture_pool.acquire() as conn:
        rows = await conn.fetch(wrapped)
    assert len(rows) == 2
```

- [ ] **Step 2: Run the test, verify it fails**

Run: `.venv/bin/python -m pytest tests/test_sql_guard.py -q`
Expected: FAIL — `ImportError: cannot import name 'wrap_with_limit' from 'occupancy_graph.service.sql_guard'`.

- [ ] **Step 3: Minimal implementation**

Append to `src/occupancy_graph/service/sql_guard.py`:

```python
def wrap_with_limit(query: str, *, cap: int) -> str:
    """Stage 2: bound the result set.

    The query is WRAPPED in a subquery rather than having its LIMIT rewritten.
    Rewriting requires understanding the query -- which LIMIT is the outer one,
    whether it is inside a CTE or a subquery -- and the entire premise of stage
    1 is that we never have to. Wrapping caps unconditionally: a supplied
    LIMIT larger than the cap is overridden by the outer one, and a smaller one
    still wins because Postgres pushes the outer limit down.

    Newlines around the body are load-bearing: a trailing `-- comment` would
    otherwise swallow the closing parenthesis.

    int(cap) is not cosmetic -- it is the only reason no caller-supplied text
    can reach a SQL position here.
    """
    return f"SELECT * FROM (\n{query}\n) AS _hatch\nLIMIT {int(cap)}"
```

- [ ] **Step 4: Run the test, verify it passes**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS — `287 passed` (281 + 6).

- [ ] **Step 5: Commit**

```bash
git add src/occupancy_graph/service/sql_guard.py tests/test_sql_guard.py
git commit -m "feat: SQL hatch row cap by subquery wrap"
```

---

### Task 18: SQL hatch stage 3 — the EXPLAIN cost gate

**Files:**
- Create: `src/occupancy_graph/service/sql_hatch.py`
- Test: `tests/test_sql_hatch.py`

- [ ] **Step 1: Write the failing test**

`tests/test_sql_hatch.py`:

```python
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
from occupancy_graph.service.sql_hatch import explain_plan, check_plan

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
    is not ANALYZE: division by zero is a runtime error, not a planning one."""
    plan = await explain_plan(fixture_pool, wrap_with_limit("SELECT 1/0 AS boom", cap=1))
    assert float(plan["Total Cost"]) >= 0.0


async def test_a_planning_error_is_a_refusal_carrying_the_planners_message(fixture_pool):
    with pytest.raises(SqlRefused) as caught:
        await explain_plan(fixture_pool, wrap_with_limit("SELECT * FROM no_such_table", cap=1))
    assert caught.value.stage == "explain"
    assert "no_such_table" in caught.value.reason
```

- [ ] **Step 2: Run the test, verify it fails**

Run: `.venv/bin/python -m pytest tests/test_sql_hatch.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'occupancy_graph.service.sql_hatch'`.

- [ ] **Step 3: Minimal implementation**

`src/occupancy_graph/service/sql_hatch.py`:

```python
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

# Pinned verbatim by Contract C. It names the access paths that are actually
# fast, so a refusal teaches the shape of what is servable instead of just
# saying no.
HINT = (
    "No index supports this predicate. Indexed paths: zip; "
    "(last_name, zip, house_number); (upper(state), upper(city)); ssn; phone; email."
)


async def explain_plan(pool: PartnerPool, wrapped: str) -> dict[str, Any]:
    """EXPLAIN (FORMAT JSON) -- never ANALYZE, so the query does not run.

    A planning error (unknown table, unknown column, syntax) becomes a stage-3
    refusal carrying Postgres's own message: that is the most useful thing the
    agent can be told, and it costs nothing to relay.
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
    """
    for node in _walk(plan):
        if node.get("Node Type") != "Seq Scan":
            continue
        relation = str(node.get("Relation Name") or "")
        if not is_records_relation(relation):
            continue
        cost = float(node.get("Total Cost") or 0.0)
        if cost > max_records_seqscan_cost:
            raise SqlRefused(
                "explain", f"Seq Scan on {relation} (cost=0.00..{cost:.2f})", HINT
            )

    total = float(plan.get("Total Cost") or 0.0)
    if total > max_plan_cost:
        raise SqlRefused(
            "explain",
            f"estimated total cost {total:.2f} exceeds the ceiling {max_plan_cost:.2f}",
            HINT,
        )
    return total
```

- [ ] **Step 4: Run the test, verify it passes**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS — `294 passed` (287 + 7).

- [ ] **Step 5: Commit**

```bash
git add src/occupancy_graph/service/sql_hatch.py tests/test_sql_hatch.py
git commit -m "feat: EXPLAIN cost gate refusing unindexed and runaway plans"
```

---

### Task 19: SQL hatch stage 4 — execute, and wire `POST /v1/sql`

**Files:**
- Modify: `src/occupancy_graph/service/sql_hatch.py`, `src/occupancy_graph/service/handlers.py`, `src/occupancy_graph/service/app.py`
- Test: `tests/test_sql_hatch.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_sql_hatch.py`:

```python
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
        json={"query": "SELECT record_id FROM public.records_partitioned", "max_rows": 2},
    )).json()
    assert body["row_count"] == 2
    assert body["truncated"] is True


async def test_non_json_types_survive_the_round_trip(client):
    body = (await client.post(
        "/v1/sql",
        json={"query": "SELECT imported_at, raw_data, identity_confidence "
                       "FROM public.records_partitioned, silver.entity_master "
                       "WHERE record_id = 2001 AND hal_id = 'HAL0001'"},
    )).json()
    assert body["rows"][0][0].startswith("2026-02-10")
    assert body["rows"][0][2] == 40.5


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
    monkeypatch.setenv("SQL_HATCH_MAX_RECORDS_SEQSCAN_COST", "0")
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


async def test_a_missing_query_field_is_a_400(client):
    assert (await client.post("/v1/sql", json={})).status_code == 400
    assert (await client.post("/v1/sql", content=b"not json")).status_code == 400


async def test_a_refused_write_did_not_happen(client, service_pool):
    """The write guard is only real if nothing landed. entity_links is the
    smallest table with a stable row count."""
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
    assert after == before == 4


async def test_execution_runs_in_a_read_only_transaction(client):
    """Belt and braces behind the parse guard: even if a write ever slipped
    through stage 1, the executing transaction is explicitly READ ONLY."""
    body = (await client.post(
        "/v1/sql", json={"query": "SELECT current_setting('transaction_read_only') AS ro"}
    )).json()
    assert body["rows"][0][0] == "on"


async def test_a_runaway_query_is_refused_before_execution(client):
    response = await client.post("/v1/sql", json={"query": RUNAWAY})
    assert response.status_code == 422
    assert response.json()["stage"] == "explain"
```

- [ ] **Step 2: Run the test, verify it fails**

Run: `.venv/bin/python -m pytest tests/test_sql_hatch.py -q`
Expected: FAIL — `assert 404 == 200` on the first new test.

- [ ] **Step 3: Minimal implementation**

Append to `src/occupancy_graph/service/sql_hatch.py`:

```python
import time
from dataclasses import dataclass

from occupancy_graph.service import limits
from occupancy_graph.service.jsonio import jsonable
from occupancy_graph.service.sql_guard import parse, wrap_with_limit


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


async def run_query(pool: PartnerPool, query: str, *, max_rows: int | None = None) -> SqlResult:
    """The four stages, in order. Any of them may raise SqlRefused.

    1. parse   -- exactly one SELECT (the write guard)
    2. wrap    -- bound the result set
    3. explain -- refuse an unservable plan
    4. execute -- READ ONLY transaction, statement timeout, row cap
    """
    cleaned = parse(query)

    cap = int(max_rows) if max_rows else limits.max_sql_rows()
    cap = max(1, min(cap, limits.max_sql_rows()))
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
            async with conn.transaction(readonly=True):
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
        rows=[[jsonable(value) for value in record] for record in window],
        row_count=len(window),
        truncated=truncated,
        plan_cost=plan_cost,
        duration_ms=duration_ms,
    )
```

Append to `src/occupancy_graph/service/handlers.py`:

```python
from occupancy_graph.service import sql_hatch
from occupancy_graph.service.sql_guard import SqlRefused


async def run_sql(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except (ValueError, _json.JSONDecodeError):
        return error(400, "request body must be JSON")
    if not isinstance(body, dict) or not str(body.get("query") or "").strip():
        return error(400, "query is required")

    try:
        result = await sql_hatch.run_query(
            request.app.state.pool, body["query"], max_rows=body.get("max_rows")
        )
    except SqlRefused as refusal:
        return JSONResponse(
            {
                "refused": True,
                "stage": refusal.stage,
                "reason": refusal.reason,
                "hint": refusal.hint,
            },
            status_code=422,
        )
    return ok(result.as_payload())
```

In `app.py`, add to `routes`:

```python
        Route("/v1/sql", handlers.run_sql, methods=["POST"]),
```

- [ ] **Step 4: Run the test, verify it passes**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS — `304 passed` (294 + 10).

- [ ] **Step 5: Commit**

```bash
git add src/occupancy_graph/service/sql_hatch.py src/occupancy_graph/service/handlers.py \
        src/occupancy_graph/service/app.py tests/test_sql_hatch.py
git commit -m "feat: POST /v1/sql -- four-stage guarded execution with structured refusals"
```

---

### Task 20: `GET /v1/schema` — the curated description

Raw introspection of 144 columns without saying which two paths are fast would guarantee refused queries.

**Files:**
- Create: `src/occupancy_graph/service/schema_doc.py`
- Modify: `src/occupancy_graph/service/handlers.py`, `src/occupancy_graph/service/app.py`
- Test: `tests/test_schema_doc.py`

- [ ] **Step 1: Write the failing test**

`tests/test_schema_doc.py`:

```python
"""GET /v1/schema -- curated, NOT raw introspection.

Every caveat here is a measured defect from the coverage spec. Omitting them
would have the model reason over column-shifted owner names and read a load
date as an observation date.
"""
from __future__ import annotations


async def test_the_schema_names_the_tables_that_matter(client):
    response = await client.get("/v1/schema")
    assert response.status_code == 200
    body = response.json()
    names = {table["name"] for table in body["tables"]}
    assert names == {
        "public.records_legacy", "public.records_partitioned",
        "silver.entity_master", "silver.entity_links",
    }
    for table in body["tables"]:
        assert table["purpose"]
        assert table["key_columns"]


async def test_every_access_path_carries_its_measured_cost(client):
    body = (await client.get("/v1/schema")).json()
    predicates = [path["predicate"] for path in body["access_paths"]]
    assert any("zip" in predicate for predicate in predicates)
    assert any("upper(state)" in predicate for predicate in predicates)
    assert any("last_name" in predicate for predicate in predicates)
    assert any("hal_id" in predicate for predicate in predicates)
    for path in body["access_paths"]:
        assert path["index"]
        assert path["measured"]


async def test_the_three_pinned_caveats_are_present_verbatim(client):
    body = (await client.get("/v1/schema")).json()
    for caveat in (
        "house_number and zip are 0% populated on property_owner rows",
        "~17.5% of property_owner rows are column-shifted",
        "imported_at is a load date, not an observation date",
    ):
        assert caveat in body["caveats"]


async def test_the_absent_shapes_are_stated_so_the_model_stops_asking(client):
    body = (await client.get("/v1/schema")).json()
    joined = " ".join(body["caveats"])
    assert "voter" in joined and "criminal" in joined and "linkedin" in joined


async def test_the_hatch_limits_are_advertised(client):
    body = (await client.get("/v1/schema")).json()
    assert body["limits"]["max_rows"] == 500
    assert body["limits"]["max_plan_cost"] == 5000000.0
    assert body["limits"]["statement_timeout_ms"] == 20000


async def test_the_schema_is_not_raw_introspection(client):
    """144 columns dumped without the access paths would guarantee refusals."""
    body = (await client.get("/v1/schema")).json()
    for table in body["tables"]:
        assert len(table["key_columns"]) <= 25
```

- [ ] **Step 2: Run the test, verify it fails**

Run: `.venv/bin/python -m pytest tests/test_schema_doc.py -q`
Expected: FAIL — `assert 404 == 200`.

- [ ] **Step 3: Minimal implementation**

`src/occupancy_graph/service/schema_doc.py`:

```python
"""What the agent is told about the corpus.

Curated, not introspected. The partner corpus is ONE 144-column table
partitioned by load month, with feed identity carried only in the unindexed
`source_file` string. Dumping 144 columns without saying which two paths are
fast would guarantee refused queries; naming the paths and the defects is the
entire value of this endpoint.

Every measured number comes from
docs/superpowers/specs/2026-07-28-engine-partner-db-interface-coverage.md §2.
"""
from __future__ import annotations

from typing import Any

from occupancy_graph.service import limits

TABLES = [
    {
        "name": "public.records_legacy",
        "purpose": "Older feeds: utility (~26%), trace (~44%), consumer base. 6.24 B rows.",
        "key_columns": [
            "record_id", "source_file", "first_name", "middle_name", "last_name",
            "dob", "address", "city", "state", "zip", "county", "phone", "mobile",
            "email", "own_rent", "employer", "occupation", "raw_data",
        ],
    },
    {
        "name": "public.records_partitioned",
        "purpose": (
            "Current feeds, partitioned by imported_at: payday loans (loan/drive), "
            "auto, property_owner (tax). 1.4 B rows. Exposed as the view records_new."
        ),
        "key_columns": [
            "record_id", "source_file", "imported_at", "first_name", "last_name",
            "address", "city", "state", "zip", "own_rent", "employer", "occupation",
            "dl_number", "dl_state", "raw_data",
        ],
    },
    {
        "name": "silver.entity_master",
        "purpose": "Partner entity resolution. One row per hal_id. Unreliable -- see caveats.",
        "key_columns": [
            "hal_id", "canonical_first_name", "canonical_last_name",
            "canonical_address_line1", "canonical_city", "canonical_state",
            "canonical_zip", "record_count", "identity_confidence", "is_suspicious",
            "is_merged", "merged_into_hal_id",
        ],
    },
    {
        "name": "silver.entity_links",
        "purpose": "hal_id -> (source_table, record_id). Indexed in both directions.",
        "key_columns": ["hal_id", "source_table", "record_id", "match_type", "confidence"],
    },
]

ACCESS_PATHS = [
    {
        "predicate": "zip = $1 AND address ILIKE 'N STREET%'",
        "table": "public.records_partitioned",
        "index": "per-partition zip btree",
        "measured": "173 ms warm, 24 k rows examined",
    },
    {
        "predicate": "zip = $1 AND address ILIKE 'N STREET%'",
        "table": "public.records_legacy",
        "index": "idx_records_zip",
        "measured": "1.30 s warm / 32 s cold, 272 916 rows examined",
    },
    {
        "predicate": "upper(state) = $1 AND upper(city) = $2 AND address ILIKE 'N STREET%'",
        "table": "public.records_partitioned",
        "index": "(upper(state), upper(city))",
        "measured": "613 ms warm / 53 s cold, 151 507 rows examined. The ONLY path to tax.",
    },
    {
        "predicate": "last_name = $1 AND zip = $2",
        "table": "public.records_legacy",
        "index": "idx_records_lastname_zip_house",
        "measured": "1.0 ms warm / 222 ms cold, 50 rows examined",
    },
    {
        "predicate": "hal_id = $1",
        "table": "silver.entity_links",
        "index": "entity_links(hal_id)",
        "measured": "215 ms",
    },
    {
        "predicate": "record_id = $1 AND source_table = $2",
        "table": "silver.entity_links",
        "index": "entity_links(record_id, source_table)",
        "measured": "81 ms",
    },
]

CAVEATS = [
    # The three pinned by Contract C, verbatim and first.
    "house_number and zip are 0% populated on property_owner rows",
    "~17.5% of property_owner rows are column-shifted",
    "imported_at is a load date, not an observation date",
    # The rest, all measured.
    "There is NO index on the free-text address, on latitude/longitude, on "
    "source_file, or on raw_data. Predicating on any of them scans.",
    "source_file is the only feed identity and it is unindexed -- always pair it "
    "with an indexed predicate, never use it as the driving condition.",
    "silver.entity_master is 17.9% is_suspicious, identity_confidence peaks at "
    "40.50, 45% are singletons, and is_merged is false everywhere: the partner "
    "never applies its own computed merges. Discount it accordingly.",
    "property_owner rows have ssn, dob and house_number 0% populated, so they carry "
    "no blocking key and are ABSENT from silver.entity_links entirely.",
    "record_id is not indexed on the records tables; looking rows up by it scans.",
    "own_rent arrives as RENT/OWN/rent/own/Rent/Own/r/o and the meaningless '1' "
    "(11.6%). Normalize before comparing.",
    "trace raw_data degrades past Record_Date (valid 82.6%): Date_Of_Birth_Year "
    "31.9%, Home_Owner_Renter_Code 14.1%, Number_of_Bedrooms 1.1%. Trust the "
    "mapped top-level columns.",
    "utility rows carry no raw_data at all (0%) and no date field of any kind.",
    "There is NO voter, criminal or linkedin data anywhere in this corpus. The "
    "voter/DMV/tax names in records_demo are 29 hand-seeded synthetic rows -- the "
    "partner's roadmap, not their inventory.",
]


def schema_document() -> dict[str, Any]:
    return {
        "tables": TABLES,
        "access_paths": ACCESS_PATHS,
        "caveats": CAVEATS,
        "limits": {
            "max_rows": limits.max_sql_rows(),
            "max_plan_cost": limits.max_plan_cost(),
            "max_records_seqscan_cost": limits.max_records_seqscan_cost(),
            "statement_timeout_ms": limits.sql_timeout_ms(),
        },
    }
```

Append to `src/occupancy_graph/service/handlers.py`:

```python
from occupancy_graph.service.schema_doc import schema_document


async def describe_schema(request: Request) -> JSONResponse:
    return ok(schema_document())
```

In `app.py`, add to `routes`:

```python
        Route("/v1/schema", handlers.describe_schema, methods=["GET"]),
```

- [ ] **Step 4: Run the test, verify it passes**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS — `310 passed` (304 + 6).

- [ ] **Step 5: Commit**

```bash
git add src/occupancy_graph/service/schema_doc.py src/occupancy_graph/service/handlers.py \
        src/occupancy_graph/service/app.py tests/test_schema_doc.py
git commit -m "feat: GET /v1/schema -- curated access paths, limits and measured caveats"
```

---

### Task 21: CLI entry point, Dockerfile, README, env template

**Files:**
- Create: `src/occupancy_graph/service/serve.py`
- Modify: `Dockerfile`, `README.md`, `.env.example`, `.dockerignore`
- Test: `tests/test_serve.py`

- [ ] **Step 1: Write the failing test**

`tests/test_serve.py`:

```python
"""The CLI entry point. Single process by design -- see service/app.py."""
from __future__ import annotations

from occupancy_graph.service.serve import build_parser


def test_defaults_bind_locally_on_8000():
    args = build_parser().parse_args([])
    assert args.host == "127.0.0.1"
    assert args.port == 8000


def test_host_and_port_are_overridable_for_the_container():
    args = build_parser().parse_args(["--host", "0.0.0.0", "--port", "9001"])
    assert args.host == "0.0.0.0"
    assert args.port == 9001


def test_there_is_no_db_or_workers_flag_left():
    """--db pointed at a SQLite file that no longer exists; --workers forked
    processes that would each hold their own bundle cache."""
    flags = {action.dest for action in build_parser()._actions}
    assert "db" not in flags
    assert "workers" not in flags
```

- [ ] **Step 2: Run the test, verify it fails**

Run: `.venv/bin/python -m pytest tests/test_serve.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'occupancy_graph.service.serve'`.

- [ ] **Step 3: Minimal implementation**

`src/occupancy_graph/service/serve.py`:

```python
#!/usr/bin/env python3
"""Serve the typed data service.

    occupancy-graph-serve --host 0.0.0.0 --port 8000

The Postgres connection comes from PARTNER_DSN (see .env.example); the app
opens the pool in its lifespan and closes it on shutdown.

SINGLE PROCESS, deliberately. The deleted GraphQL server forked uvicorn workers
because its SQLite resolvers were synchronous and blocked the event loop. Every
path here is async I/O against asyncpg, and the AddressBundle cache is
per-process: N workers would mean N caches and N cold 173 ms - 32 s scans per
address, which is the exact cost the cache exists to remove.
"""
from __future__ import annotations

import argparse

import uvicorn

from occupancy_graph.service.app import create_app


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Serve the typed data service + SQL hatch.")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host.")
    parser.add_argument("--port", type=int, default=8000, help="Bind port.")
    parser.add_argument("--log-level", default="info", help="uvicorn log level.")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    uvicorn.run(create_app(), host=args.host, port=args.port, log_level=args.log_level)


if __name__ == "__main__":
    main()
```

`Dockerfile` — replace entirely:

```dockerfile
FROM python:3.14-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .
EXPOSE 8000
# PARTNER_DSN (and optionally PARTNER_STATEMENT_TIMEOUT_MS, PARTNER_POOL_MIN/MAX,
# SQL_HATCH_*) come from theenvironment. There is no mounted database any more.
CMD ["occupancy-graph-serve", "--host", "0.0.0.0", "--port", "8000"]
```

`README.md` — replace entirely:

```markdown
# occupancy-graph-service

A typed HTTP data service over the partner records corpus (Postgres), plus a guarded
exploratory SQL hatch. Consumed as a git submodule by `occupancy-engine-ts` at
`services/graph`.

## Run

    VIRTUAL_ENV=.venv uv pip install --python .venv/bin/python -e ".[dev]"
    export PARTNER_DSN=postgresql://USER:PASSWORD@HOST:5432/all_data?sslmode=require
    .venv/bin/occupancy-graph-serve --host 0.0.0.0 --port 8000

Single process by design: the AddressBundle cache is per-process, and every path is
async I/O against asyncpg.

## Surface

| Operation | Backing path |
|---|---|
| `POST /v1/resolve` `{address, zip}` | phase 1 `zip`+prefix, then phase 2 `(upper(state),upper(city))`+prefix for tax |
| `GET /v1/address/{id}/records?shapes=&limit=&offset=` | bundle |
| `GET /v1/address/{id}/people?limit=&offset=` | bundle, name-key clustering |
| `GET /v1/person/{id}/records?shapes=&limit=` | bundle for `addr:` ids, `silver.entity_links` for `hal:` ids |
| `GET /v1/people/search?name=&limit=` | `silver.entity_master` by name |
| `GET /v1/source-record/{shape}/{rowid}?address_id=` | bundle |
| `POST /v1/sql` `{query}` | guarded hatch: parse -> LIMIT -> EXPLAIN -> execute |
| `GET /v1/schema` | curated access paths, limits and caveats |
| `GET /healthz` | liveness |

Record payloads keep the raw vendor column names (`first_name`, `ownername`, `dob_day`)
exactly as `src/occupancy_graph/source/manifest.py` defines them.

## The SQL hatch

`POST /v1/sql` runs four stages in order. **Stage 1 is the primary write guard**, not
defence in depth: `default_transaction_read_only` is a session default that
`BEGIN READ WRITE` defeats (pinned by `tests/test_pool.py`).

1. **Parse** — exactly one `SELECT`; no `;`-chaining, no DML anywhere including inside a
   CTE, no `BEGIN`/`SET`/`COPY`/`DO`/`CALL`/`GRANT`/`ALTER`, no filesystem or `dblink`
   functions.
2. **LIMIT** — the query is wrapped in a capped subquery.
3. **EXPLAIN** (never ANALYZE) — refused above the cost ceiling, or on a sequential scan
   over a records table. See `docs/explain-cost-calibration.md`.
4. **Execute** — explicit `READ ONLY` transaction, `statement_timeout`, row cap.

A refusal is `422` with the planner's own reason and a hint naming the indexed paths.

## Verification

    docs/verification.md
```

`.env.example` — append:

```bash
# SQL hatch guard. See docs/explain-cost-calibration.md before changing these.
SQL_HATCH_MAX_PLAN_COST=5000000
SQL_HATCH_MAX_RECORDS_SEQSCAN_COST=50000
SQL_HATCH_MAX_ROWS=500
SQL_HATCH_TIMEOUT_MS=20000
```

`.dockerignore` — replace `*.sqlite` with `docs` (the SQLite artefact no longer exists; the docs directory does not belong in the image):

```
.git
.venv
__pycache__
data
docs
tests
```

- [ ] **Step 4: Run the tests and the container build**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS — `313 passed` (310 + 3).

Run: `cd /home/aayan-alam/Work/Helcion/true-occupancy-workspace/occupancy-engine-ts/services/graph && docker build -t occupancy-graph-service:x016 .`
Expected: build succeeds, final line `Successfully tagged occupancy-graph-service:x016`. (This builds an image only — it does not start a container, and it does not touch the unrelated `mortgage-compliance-monitoring-graph-*` containers.)

- [ ] **Step 5: Commit**

```bash
git add src/occupancy_graph/service/serve.py Dockerfile README.md .env.example .dockerignore \
        tests/test_serve.py
git commit -m "feat: single-process CLI entry point, container and docs for the data service"
```

---

### Task 22: Live-corpus smoke tests

Collected but skipped without `PARTNER_DSN`, so the default suite stays green. These are the assertions the umbrella's end-to-end step 1 runs once credentials exist.

**Files:**
- Create: `tests/test_live_smoke.py`

- [ ] **Step 1: Write the test**

`tests/test_live_smoke.py`:

```python
"""Opt-in smoke against the real partner corpus.

    PARTNER_DSN=... .venv/bin/python -m pytest -m live -q

Skipped, not failed, without credentials -- the calibration and the surface are
both provable against the fixture, and this is the confirmation that the live
corpus behaves as the coverage spec measured it.

1104 Spring Run Rd / 40514 is the address that produced a genuine
absentee-owner signal end to end: the property is in Lexington KY and the
assessor row's owner mails to AURORA, IL.
"""
from __future__ import annotations

import os

import httpx
import pytest

from occupancy_graph.service.app import create_app
from occupancy_graph.source.bundle import BundleCache
from occupancy_graph.source.pool import PartnerPool

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not os.environ.get("PARTNER_DSN"), reason="PARTNER_DSN is not set"
    ),
]


@pytest.fixture
async def live_client():
    pool = await PartnerPool.from_env()
    app = create_app(pool=pool, cache=BundleCache(pool))
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://graph.live") as http:
            yield http
    finally:
        await pool.close()


async def test_the_subject_address_resolves_with_its_assessor_row(live_client):
    response = await live_client.post(
        "/v1/resolve", json={"address": "1104 Spring Run Rd", "zip": "40514"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["address_id"] is not None
    assert body["source_counts"]["trace"] > 0
    assert body["source_counts"]["utility"] > 0
    tax = body["records_by_source"]["tax"]["records"]
    assert tax, f"no tax row; tax_timed_out={body['tax_timed_out']}"
    assert tax[0]["ownerstate"] == "IL"
    assert tax[0]["ownercity"] == "AURORA"


async def test_the_measured_access_path_costs_still_hold(live_client):
    """If the live plan costs have drifted past the ceiling, the hatch would
    start refusing servable queries -- fail here rather than in production."""
    response = await live_client.post(
        "/v1/sql",
        json={
            "query": "SELECT record_id, address FROM public.records_new "
                     "WHERE zip = '40514' AND address ILIKE '1104 Spring%'",
            "max_rows": 25,
        },
    )
    assert response.status_code == 200, response.json()
    assert response.json()["plan_cost"] < 5_000_000.0


async def test_the_explain_gate_refuses_a_real_unindexed_predicate(live_client):
    response = await live_client.post(
        "/v1/sql",
        json={"query": "SELECT record_id FROM public.records_legacy WHERE employer = 'ACME'"},
    )
    assert response.status_code == 422
    body = response.json()
    assert body["stage"] == "explain"
    assert "Indexed paths" in body["hint"]


async def test_an_owner_search_reaches_the_entity_graph(live_client):
    response = await live_client.get("/v1/people/search?name=Currie&limit=5")
    assert response.status_code == 200
    for result in response.json()["results"]:
        assert "identity_confidence" in result
        assert "is_suspicious" in result
```

- [ ] **Step 2: Verify they are collected and skipped**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS — `313 passed, 4 skipped` (313 + 4 collected, all 4 skipped without `PARTNER_DSN`).

Run: `.venv/bin/python -m pytest -m live -q`
Expected: `4 skipped, 313 deselected`.

- [ ] **Step 3: Commit**

```bash
git add tests/test_live_smoke.py
git commit -m "test: opt-in live smoke against the real partner corpus"
```

---

### Task 23: Adversarial pass and the verification doc

**Files:**
- Create: `docs/verification.md`
- Modify: `docs/explain-cost-calibration.md` (append the partner ask)

- [ ] **Step 1: Run the adversarial pass and record the output**

Run:

```bash
cd /home/aayan-alam/Work/Helcion/true-occupancy-workspace/occupancy-engine-ts/services/graph
.venv/bin/python -m pytest tests/test_sql_guard.py tests/test_sql_hatch.py -q
```

Expected: `41 passed` (25 parse + 6 wrap + 7 explain + 10 execute/endpoint), and specifically
`test_a_refused_write_did_not_happen` green — `silver.entity_links` still holds 4 rows after both
attack shapes.

- [ ] **Step 2: Write `docs/verification.md`**

```markdown
# Verification — occupancy-graph-service

## Gates

    cd occupancy-engine-ts/services/graph
    .venv/bin/python -m pytest -q

Expected: `313 passed, 4 skipped`. The 4 skips are the `-m live` smoke tests, which need
`PARTNER_DSN`. The Postgres fixture starts and stops itself (`tests/conftest.py` +
`tests/docker-compose.fixture.yml`, port 55432). Docker must be running. Containers named
`mortgage-compliance-monitoring-graph-*` belong to another repo — leave them alone.

Use `.venv/bin/python` for everything. The venv is uv-managed and has **no `pip` binary**:

    VIRTUAL_ENV=.venv uv pip install --python .venv/bin/python <pkg>

Tests are plain module-level `async def` functions. Never write an `async def` method on a
`unittest.TestCase`: `asyncio_mode="auto"` does not wrap them, so they pass without running.

## SQL hatch adversarial pass

    .venv/bin/python -m pytest tests/test_sql_guard.py tests/test_sql_hatch.py -q

Expected: `41 passed`. This covers `;`-chaining, DML inside CTEs, `BEGIN READ WRITE`,
`COMMIT`/`ROLLBACK`, `SET`, `COPY`, `DO`, `CALL`, `GRANT`, `ALTER`, `SELECT ... INTO`,
comment- and literal-hiding, filesystem and `dblink` functions, the EXPLAIN cost ceiling,
the records-table seq-scan rule, and a post-attack row count proving nothing was written.

## Live smoke (needs credentials)

    PARTNER_DSN=... .venv/bin/python -m pytest -m live -q

`1104 Spring Run Rd` / `40514` must return the absentee-owner tax row with
`ownerstate == "IL"` and `ownercity == "AURORA"`.

## Container

    docker build -t occupancy-graph-service:x016 .

## Re-tuning the EXPLAIN ceilings

See `docs/explain-cost-calibration.md`. `scripts/explain_cost_probe.py` is the instrument.
```

- [ ] **Step 3: Append the partner ask to `docs/explain-cost-calibration.md`**

```markdown
## Partner asks this work created

1. **A role that physically cannot write.** The parse guard in
   `src/occupancy_graph/service/sql_guard.py` is our control and is the primary one, but
   `default_transaction_read_only` is a session default that `BEGIN READ WRITE` defeats
   (`tests/test_pool.py::test_read_only_is_a_session_default_not_a_boundary`). A role
   without write privileges is the durable protection. This became genuinely important
   the moment the agent started writing SQL, rather than merely tidy.
2. **An index on `records_legacy(record_id)` and `records_partitioned(record_id)`.**
   `silver.entity_links` is indexed both ways, but the rows it points at are fetched by
   `record_id`, which no index covers and which cannot prune partitions. This is the one
   unindexed hop in the typed surface; `GET /v1/person/hal:.../records` runs it under the
   statement timeout and reports `records_timed_out`.
3. **Backfill `zip` and `house_number` on `property_owner` rows** from
   `raw_data.zipCodePlusFour` / `raw_data.streetNumber`, plus the matching index. This turns
   a 53 s city-wide scan into a sub-100 ms lookup and is still the single highest-leverage
   ask (see the coverage spec §7).
```

- [ ] **Step 4: Full suite and clean tree**

Run: `.venv/bin/python -m pytest -q && git status --short`
Expected: `313 passed, 4 skipped`, and `git status --short` shows only the two doc files.

- [ ] **Step 5: Commit**

```bash
git add docs/verification.md docs/explain-cost-calibration.md
git commit -m "docs: verification gates, adversarial pass and the partner asks"
```

---

## Verification / Definition of Done

- [ ] `.venv/bin/python -m pytest -q` → **`313 passed, 4 skipped`** (from a 181-test baseline)
- [ ] `.venv/bin/python -m pytest tests/test_sql_guard.py tests/test_sql_hatch.py -q` → `41 passed`; every adversarial shape refused, `silver.entity_links` row count unchanged after the attacks
- [ ] `grep -rn "strawberry\|occupancy_graph.graphql\|occupancy_graph.graphdb" --include=*.py --include=*.toml .` (excluding `.venv`) → no output
- [ ] `.venv/bin/python -c "import strawberry"` → `ModuleNotFoundError`
- [ ] `docker build -t occupancy-graph-service:x016 .` → succeeds
- [ ] `GET /v1/person/hal:HAL0001/records` returns real linked rows with `identity_confidence` and `is_suspicious` — **not a stub**
- [ ] `git status --short` clean; still on `feat/postgres-adapter`; `origin/main` untouched
- [ ] Umbrella `docs/harness/progress.md` (+ `feature_list.json`) updated by the coordinator
- [ ] Contract notes 1–3 handed to the engine plan before it starts (`address_id` query param on operation 6, `__rowid` on bundle-sourced records, `records_timed_out` on operation 4)

### Test-count ledger

| After task | Count | Δ |
|---|---|---|
| baseline | 181 | — |
| 1 calibration | 188 | +7 |
| 2 delete GraphQL | 174 | −14 |
| 3 delete graphdb | 173 | −1 |
| 4 pagination | 182 | +9 |
| 5 jsonio | 188 | +6 |
| 6 reverse feed map | 194 | +6 |
| 7 search.py | 204 | +10 |
| 8 app skeleton | 209 | +5 |
| 9 op 1 resolve | 217 | +8 |
| 10 op 2 address records | 224 | +7 |
| 11 op 3 address people | 229 | +5 |
| 12 op 4 `addr:` | 235 | +6 |
| 13 op 4 `hal:` | 243 | +8 |
| 14 op 5 search | 250 | +7 |
| 15 op 6 source-record | 256 | +6 |
| 16 parse guard | 281 | +25 |
| 17 LIMIT wrap | 287 | +6 |
| 18 EXPLAIN gate | 294 | +7 |
| 19 execute + `/v1/sql` | 304 | +10 |
| 20 `/v1/schema` | 310 | +6 |
| 21 serve/Docker/docs | 313 | +3 |
| 22 live smoke | 313 passed, 4 skipped | +4 collected |
| 23 verification docs | 313 passed, 4 skipped | 0 |

---

## Things worth flagging back before execution starts

1. **`source/search.py` does not exist** in the repo. The brief says it does and only needs wiring; Task 7 writes it from scratch (the fixture data is already there, so it is fully testable). If the parent agent expected a smaller Task 7, that expectation is wrong, not the plan.
2. **The unconditional seq-scan refusal in Contract C §3 is implemented cost-gated** (Task 1). Taken literally it makes the hatch untestable against the seeded fixture. The rationale is in `service/limits.py` and `docs/explain-cost-calibration.md`; this is the plan's only deliberate departure from the pinned spec text, and it is a refinement of a threshold, not of the contract's wire shape.
3. **Three additive fields/params** (contract notes 1–3) must reach the engine plan: `?address_id=` on operation 6, `__rowid` on bundle-sourced records, `records_timed_out` on operation 4. Every pinned key keeps its pinned name and meaning; these are supersets.
4. **`graphdb/` deletion (Task 3) was not on the brief's delete list.** It is orphaned the moment `graphql/` goes, and it is a separate commit so it can be reverted alone if someone disagrees.

### Critical Files for Implementation

- `/home/aayan-alam/Work/Helcion/true-occupancy-workspace/occupancy-engine-ts/services/graph/src/occupancy_graph/service/sql_guard.py` (new — the primary write guard)
- `/home/aayan-alam/Work/Helcion/true-occupancy-workspace/occupancy-engine-ts/services/graph/src/occupancy_graph/source/search.py` (new — the entity graph the `hal:` traversal needs; does not exist today)
- `/home/aayan-alam/Work/Helcion/true-occupancy-workspace/occupancy-engine-ts/services/graph/src/occupancy_graph/service/handlers.py` (new — all six operations plus the hatch endpoint)
- `/home/aayan-alam/Work/Helcion/true-occupancy-workspace/occupancy-engine-ts/services/graph/src/occupancy_graph/service/limits.py` (new — the calibrated ceilings and their derivation)
- `/home/aayan-alam/Work/Helcion/true-occupancy-workspace/occupancy-engine-ts/services/graph/src/occupancy_graph/source/bundle.py` (existing — the cache every address-scoped operation reads)
- `/home/aayan-alam/Work/Helcion/true-occupancy-workspace/occupancy-engine-ts/services/graph/tests/conftest.py` (existing — gains the `service_pool` and `client` fixtures every operation test uses)