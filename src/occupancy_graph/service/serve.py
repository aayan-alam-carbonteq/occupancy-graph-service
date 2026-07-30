#!/usr/bin/env python3
"""Serve the typed data service.

    occupancy-graph-serve --host 0.0.0.0 --port 8000

The Postgres connection comes from PARTNER_DSN (see .env.example); the app
opens the pool in its lifespan and closes it on shutdown. Nothing here reads
the environment, so an unset PARTNER_DSN fails inside the lifespan with
PartnerPool.from_env's own message rather than here.

SINGLE PROCESS, deliberately -- and the absence of a --workers flag is the
decision, not an omission. The deleted GraphQL server forked uvicorn workers
because its SQLite resolvers were synchronous and blocked the event loop. Every
path here is async I/O against asyncpg, and the AddressBundle cache is
per-process: N workers would mean N caches and N cold 173 ms - 32 s scans per
address, which is the exact cost the cache exists to remove. Scale this by
running it behind a load balancer with a shared upstream Postgres, not by
forking a process that re-pays the scan.

`create_app()` is called here rather than passing the import string
"occupancy_graph.service.app:app", because uvicorn only needs the string when
it has to re-import the app in a child process -- which is precisely the
arrangement this module refuses to have.
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
