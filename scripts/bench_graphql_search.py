#!/usr/bin/env python
"""Micro-benchmark for the slowest GraphQL data-layer search functions.

Times ``search_persons`` and ``search_addresses`` directly against the real
graph SQLite DB (NO server, NO LLM). Each input is run a fixed number of times
and the median wall-clock time (ms) per function is reported.

Run with:
    uv run python scripts/bench_graphql_search.py
"""

from __future__ import annotations

import statistics
import time
from pathlib import Path

from occupancy_graph.graphql.db import GraphDatabase
from occupancy_graph.graphql.types import search_addresses, search_persons

DB_PATH = Path("data/indexes/graph.sqlite")
RUNS = 3

PERSON_QUERIES = ["BRIAN TURNER", "JOHN SMITH", "KEMPA TURNER"]
ADDRESS_QUERIES = ["1000 TURNBERRY LN", "1104 SPRING RUN RD"]


def _median_ms(fn) -> tuple[float, int]:
    timings: list[float] = []
    node_count = 0
    for _ in range(RUNS):
        start = time.perf_counter()
        result = fn()
        elapsed = time.perf_counter() - start
        timings.append(elapsed * 1000.0)
        node_count = len(result.nodes)
    return statistics.median(timings), node_count


def main() -> None:
    db = GraphDatabase(DB_PATH)
    try:
        rows: list[tuple[str, str, float, int]] = []

        for query in PERSON_QUERIES:
            median, nodes = _median_ms(lambda q=query: search_persons(db, q))
            rows.append(("search_persons", query, median, nodes))

        for query in ADDRESS_QUERIES:
            median, nodes = _median_ms(lambda q=query: search_addresses(db, q))
            rows.append(("search_addresses", query, median, nodes))

        print(f"\nBenchmark over real DB ({DB_PATH}), {RUNS} runs each, median ms\n")
        print(f"{'function':<18} {'query':<22} {'median_ms':>12} {'nodes':>7}")
        print("-" * 62)
        for fn_name, query, median, nodes in rows:
            print(f"{fn_name:<18} {query:<22} {median:>12.1f} {nodes:>7}")

        print("\nPer-function median across its queries:")
        for fn_name in ("search_persons", "search_addresses"):
            medians = [m for n, _, m, _ in rows if n == fn_name]
            print(f"  {fn_name:<18} {statistics.median(medians):>10.1f} ms")
        print()
    finally:
        db.close()


if __name__ == "__main__":
    main()
