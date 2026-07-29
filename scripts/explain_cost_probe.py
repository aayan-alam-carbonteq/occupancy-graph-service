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

import asyncpg

MUST_SERVE = {
    "zip + address prefix (records_legacy)": """
        SELECT * FROM public.records_legacy
        WHERE zip = $$40505$$ AND address ILIKE $$123 MAIN%$$ LIMIT 200
    """,
    "zip + address prefix (records_partitioned)": """
        SELECT * FROM public.records_partitioned
        WHERE zip = $$40505$$ AND address ILIKE $$123 MAIN%$$ LIMIT 200
    """,
    "upper(state)+upper(city) + prefix, phase-2 shape (records_partitioned)": """
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
