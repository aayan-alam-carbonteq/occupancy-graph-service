"""Export the GraphQL SDL to schema.graphql (the boundary contract).

The schema is built dynamically from the DB registry, so we build a tiny
sample DB, derive the schema from it, and write its SDL.
"""
from __future__ import annotations

import argparse
import tempfile
from pathlib import Path

from occupancy_graph.graphdb.sample import sample_db
from occupancy_graph.graphql.schema import create_schema


def render_sdl() -> str:
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "fixture.sqlite"
        sample_db(db)
        return create_schema(db).as_str().rstrip() + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Write the GraphQL SDL to a file.")
    parser.add_argument("--out", type=Path, default=Path("schema.graphql"))
    args = parser.parse_args()
    args.out.write_text(render_sdl(), encoding="utf-8")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
