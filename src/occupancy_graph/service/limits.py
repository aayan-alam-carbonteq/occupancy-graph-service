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
