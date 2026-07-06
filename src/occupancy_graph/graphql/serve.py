#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

import uvicorn

from occupancy_graph.graphql.server import create_app

# Environment variable used to hand the DB path to forked uvicorn worker
# processes. uvicorn can only run multiple workers when given an import string
# (e.g. "occupancy_graph.graphql.serve:app"), because it forks workers that
# re-import this module. The worker has no access to the parsed CLI args, so the
# DB path is passed via the environment and read back here at import time.
_DB_ENV = "GRAPH_SERVICE_DB"

# Default worker count: a few processes so concurrent agent queries run in
# parallel, but capped so we don't oversubscribe small machines.
_DEFAULT_WORKERS = min(4, (os.cpu_count() or 1))


def _build_app_from_env():
    """Build the ASGI app from the DB path in the environment.

    Used by uvicorn worker processes, which re-import this module (with the env
    var already set by the parent CLI process) to construct their own app
    instance, event loop, and read-only DB connection.
    """
    db = os.environ.get(_DB_ENV)
    if not db:
        raise RuntimeError(f"{_DB_ENV} not set")
    return create_app(Path(db))


# Module-level app that uvicorn can import by string for multi-worker mode.
# IMPORTANT: only build it when the env var is present, so that merely importing
# this module (in tests / tooling) does NOT open a DB or raise. uvicorn worker
# processes re-import this module with GRAPH_SERVICE_DB set, so ``app`` is built in
# each worker; without the env var ``app`` is simply ``None``.
app = _build_app_from_env() if os.environ.get(_DB_ENV) else None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Serve the read-only GraphQL graph database.")
    parser.add_argument("--db", type=Path, default=Path("data/indexes/graph.sqlite"), help="Graph SQLite DB path.")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host.")
    parser.add_argument("--port", type=int, default=8000, help="Bind port.")
    parser.add_argument(
        "--workers",
        type=int,
        default=_DEFAULT_WORKERS,
        help=(
            "Number of uvicorn worker processes. >1 runs each request on a "
            "separate process/event loop so concurrent agent queries are served "
            f"concurrently. Default: min(4, os.cpu_count()) = {_DEFAULT_WORKERS}."
        ),
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if not args.db.exists():
        parser.error(f"Graph SQLite DB does not exist: {args.db}")

    # Hand the resolved DB path to (re-imported) worker processes.
    os.environ[_DB_ENV] = str(Path(args.db).resolve())

    if args.workers and args.workers > 1:
        # Multi-process: uvicorn forks workers that re-import the module and
        # build their own ``app`` from GRAPH_SERVICE_DB. Must use an import string.
        uvicorn.run(
            "occupancy_graph.graphql.serve:app",
            host=args.host,
            port=args.port,
            workers=args.workers,
            log_level="info",
        )
    else:
        # Single-process: preserve the exact original behaviour (app instance,
        # no import string). Simple, and the fallback if multi-worker misbehaves.
        app = create_app(args.db)
        uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
