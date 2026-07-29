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

RANGE RULES. The two ceilings accept >= 0 -- 0 is the strictest setting, not a
broken one. The two operational bounds, max_sql_rows and sql_timeout_ms, require
>= 1, because 0 means "unbounded" to Postgres rather than "nothing". Neither
ceiling may be inf or nan; see _float_env for why that is not pedantry.

RE-TUNING. scripts/explain_cost_probe.py prints the root Total Cost for a
fixed battery of queries against any DSN. Point it at PARTNER_DSN when
credentials arrive and set the two env vars. No code change is needed.
"""
from __future__ import annotations

import math
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

# Relation-name prefixes that identify a partner records table. Partition
# children (records_partitioned_p20260301) match via the records_partitioned
# prefix -- and so does the records_new VIEW, though not by its own name:
# Postgres inlines views at rewrite, so a plan over records_new names its
# partition children and never the view. The records_new prefix is therefore
# dead in practice; it is kept because it costs nothing and fails closed if the
# partner ever materialises that view into a real relation.
_RECORDS_PREFIXES = ("records_legacy", "records_partitioned", "records_new")


def _float_env(name: str, default: float, *, minimum: float) -> float:
    """Read a float env var, failing closed with a message naming the culprit
    rather than silently falling back to `default`.

    Rejects inf and nan explicitly. Both parse cleanly through `float()`, and
    both are catastrophic here: the gates that consume these values are written
    `if cost > ceiling: refuse`, and `cost > inf` and `cost > nan` are False for
    every cost, so either value converts the ceiling into an unconditional
    allow. nan is the worse of the two -- it is invisible on inspection and
    serialises as bare `NaN`, which is not valid strict JSON.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number, got {raw!r}") from exc
    if not math.isfinite(value):
        raise ValueError(
            f"{name} must be a finite number, got {raw!r}. inf and nan compare "
            "False against every plan cost, which would silently disable the "
            "refusal this ceiling exists to enforce."
        )
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}, got {value!r}")
    return value


def _int_env(name: str, default: int, *, minimum: int, consequence: str = "") -> int:
    """Read an int env var, failing closed. Mirrors source.pool._int_env,
    including its range check -- `consequence` states, in the error itself, what
    an out-of-range value would actually do, so nobody widens the bound to make
    the error go away."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc
    if value < minimum:
        message = f"{name} must be >= {minimum}, got {value}."
        raise ValueError(f"{message} {consequence}".rstrip())
    return value


def max_plan_cost() -> float:
    # 0 is legal: it refuses every plan. Useful as a kill switch.
    return _float_env("SQL_HATCH_MAX_PLAN_COST", DEFAULT_MAX_PLAN_COST, minimum=0.0)


def max_records_seqscan_cost() -> float:
    # 0 is legal and is the strictest setting: every seq scan costs more than 0,
    # so the rule becomes unconditional -- which is what the spec text asks for.
    return _float_env(
        "SQL_HATCH_MAX_RECORDS_SEQSCAN_COST",
        DEFAULT_MAX_RECORDS_SEQSCAN_COST,
        minimum=0.0,
    )


def max_sql_rows() -> int:
    return _int_env(
        "SQL_HATCH_MAX_ROWS",
        DEFAULT_MAX_SQL_ROWS,
        minimum=1,
        consequence="A hatch that can return no rows is not a hatch.",
    )


def sql_timeout_ms() -> int:
    return _int_env(
        "SQL_HATCH_TIMEOUT_MS",
        DEFAULT_SQL_TIMEOUT_MS,
        minimum=1,
        consequence=(
            "Postgres treats 0 as UNLIMITED, which would let a single "
            "LLM-authored query run without bound against the partner corpus. "
            "The row cap does not help: it bounds rows returned, not work done."
        ),
    )


def is_records_relation(name: str) -> bool:
    return str(name or "").lower().startswith(_RECORDS_PREFIXES)
