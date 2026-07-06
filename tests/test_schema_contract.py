from pathlib import Path

from occupancy_graph.graphql.export_schema import render_sdl


def test_committed_schema_matches_live():
    committed = Path("schema.graphql").read_text(encoding="utf-8")
    assert committed == render_sdl(), (
        "schema.graphql is stale; run `occupancy-graph-export-schema` and commit."
    )
