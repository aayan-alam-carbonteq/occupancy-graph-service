#!/usr/bin/env python
"""Concurrency benchmark for the GraphQL server (NO LLM).

Starts the GraphQL server as a subprocess at a given ``--workers`` value, fires
8 concurrent ``searchPersons`` POST requests via a thread pool, and reports the
total wall-clock time and the *serialization factor* (total_wall / single_query).

A single-worker server serializes concurrent requests (each blocks the one event
loop on a synchronous SQLite resolver), so the factor is roughly the concurrency
count. Multiple worker processes spread the requests across event loops, dropping
the factor toward (concurrency / workers).

Run with:
    uv run python scripts/bench_server_concurrency.py --workers 1
    uv run python scripts/bench_server_concurrency.py --workers 4
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

DEFAULT_DB = Path("data/indexes/graph.sqlite")
DEFAULT_PORT = 8021
CONCURRENCY = 8
SEARCH_QUERY = (
    '{ searchPersons(query: "JOHN SMITH", limit: 25) { totalCount nodes { person { id } } } }'
)


def _post_graphql(url: str, query: str, timeout: float = 60.0) -> tuple[int, float]:
    """POST a GraphQL query, return (status_code, elapsed_seconds)."""
    payload = json.dumps({"query": query}).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"}, method="POST"
    )
    start = time.perf_counter()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        resp.read()
        status = resp.status
    return status, time.perf_counter() - start


def _wait_ready(url: str, timeout: float = 60.0) -> bool:
    """Poll ``{ __typename }`` until the server returns HTTP 200."""
    deadline = time.time() + timeout
    payload = json.dumps({"query": "{ __typename }"}).encode("utf-8")
    while time.time() < deadline:
        try:
            req = urllib.request.Request(
                url, data=payload, headers={"Content-Type": "application/json"}, method="POST"
            )
            with urllib.request.urlopen(req, timeout=5.0) as resp:
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, ConnectionError, OSError):
            pass
        time.sleep(0.25)
    return False


def run_bench(db: Path, port: int, workers: int) -> dict:
    url = f"http://127.0.0.1:{port}/graphql"
    # Match how the CLI is exposed (console script: oe-graphql-serve ->
    # occupancy_graph.graphql.serve:main). Use the module entry point so this
    # works whether or not the console script is on PATH.
    cmd = [
        sys.executable,
        "-m",
        "occupancy_graph.graphql.serve",
        "--db",
        str(db),
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--workers",
        str(workers),
    ]
    print(f"\n=== workers={workers} ===")
    print("starting:", " ".join(cmd))
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        if not _wait_ready(url):
            raise RuntimeError("server did not become ready in time")

        # Warm up + single-request baseline (median of a few sequential calls).
        single_timings: list[float] = []
        for _ in range(3):
            status, elapsed = _post_graphql(url, SEARCH_QUERY)
            if status != 200:
                raise RuntimeError(f"baseline request returned {status}")
            single_timings.append(elapsed)
        single_wall = statistics.median(single_timings)

        # Fire CONCURRENCY requests at once via a thread pool.
        start = time.perf_counter()
        with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
            futures = [pool.submit(_post_graphql, url, SEARCH_QUERY) for _ in range(CONCURRENCY)]
            results = [f.result() for f in futures]
        total_wall = time.perf_counter() - start

        statuses = [s for s, _ in results]
        per_req = [t for _, t in results]
        ok = all(s == 200 for s in statuses)
        median_req = statistics.median(per_req)
        serialization = total_wall / single_wall if single_wall > 0 else float("nan")

        return {
            "workers": workers,
            "ok": ok,
            "single_wall_s": single_wall,
            "total_wall_s": total_wall,
            "median_req_s": median_req,
            "serialization_factor": serialization,
            "statuses": statuses,
        }
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--workers",
        type=int,
        default=None,
        help="If set, run only this worker count. Default: run both 1 and 4.",
    )
    args = parser.parse_args()

    if not args.db.exists():
        parser.error(f"DB does not exist: {args.db}")

    worker_counts = [args.workers] if args.workers is not None else [1, 4]
    summaries = []
    for w in worker_counts:
        summaries.append(run_bench(args.db, args.port, w))

    print("\n========== SUMMARY ==========")
    print(
        f"{'workers':>7} {'ok':>4} {'single_s':>10} {'total_s':>10} "
        f"{'median_s':>10} {'serial_x':>10}"
    )
    for s in summaries:
        print(
            f"{s['workers']:>7} {str(s['ok']):>4} {s['single_wall_s']:>10.3f} "
            f"{s['total_wall_s']:>10.3f} {s['median_req_s']:>10.3f} "
            f"{s['serialization_factor']:>10.2f}"
        )
    print(f"(concurrency = {CONCURRENCY} requests, query = searchPersons JOHN SMITH limit 25)")


if __name__ == "__main__":
    main()
