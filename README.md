# occupancy-graph-service

Builds the address-graph SQLite DB from cleaned CSVs and serves it over GraphQL.
Extracted from occupancy-engine; consumed as a git submodule by both
occupancy-engine (Python) and occupancy-engine-ts (TS).

## Use
    pip install -e .
    occupancy-graph-build-index --cleaned-dir <csvs> --db data/indexes/graph.sqlite
    occupancy-graph-serve --db data/indexes/graph.sqlite --port 8000

The GraphQL contract is `schema.graphql` (regenerate: `occupancy-graph-export-schema`).
