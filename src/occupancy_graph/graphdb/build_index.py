#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from occupancy_graph.graphdb.core import GraphDbError, build_index


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build a read-only SQLite graph index for cleaned CSV data.")
    parser.add_argument(
        "--cleaned-dir",
        type=Path,
        default=Path("data/cleaned/lexington"),
        help="Directory containing cleaned CSVs.",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=Path("data/indexes/graph.sqlite"),
        help="SQLite graph DB output path.",
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        result = build_index(args.cleaned_dir, args.db)
    except GraphDbError as exc:
        parser.error(str(exc))
    print(
        json.dumps(
            {
                "db_path": str(result.db_path),
                "source_rows": result.source_rows,
                "address_nodes": result.address_nodes,
                "address_edges": result.address_edges,
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
