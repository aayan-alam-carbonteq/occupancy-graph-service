# occupancy-graph-service

A typed HTTP data service over the partner records corpus (Postgres), plus a guarded
exploratory SQL hatch. Consumed as a git submodule by `occupancy-engine-ts` at
`services/graph`.

The corpus is someone else's production database — ~7.6 B rows over 3.7 TB, reached with
read-only guest credentials. Nothing here builds, loads or owns data; it makes a corpus
that only answers indexed queries safe for an LLM agent to read.

## Run

    VIRTUAL_ENV=.venv uv pip install --python .venv/bin/python -e ".[dev]"
    export PARTNER_DSN=postgresql://USER:PASSWORD@HOST:5432/all_data?sslmode=require
    .venv/bin/occupancy-graph-serve --host 0.0.0.0 --port 8000

`.env.example` lists every variable the service reads. Never commit a filled-in copy.

Single process by design, and `--workers` is absent deliberately: the `AddressBundle` cache
is per-process, so a second worker re-pays the 173 ms – 32 s address scan into a cache
nothing else can read. Every path is async I/O against asyncpg, so one event loop keeps the
pool busy. Scale with more containers against the same Postgres.

`uvicorn occupancy_graph.service.app:app` works too — importing that module builds the app
but opens no connection; the pool is created in the lifespan from `PARTNER_DSN`.

## Surface

| Operation | Backing path |
|---|---|
| `POST /v1/resolve` `{address, zip}` | phase 1 `zip`+prefix, then phase 2 `(upper(state),upper(city))`+prefix for tax |
| `GET /v1/address/{id}/records?shapes=&limit=&offset=` | bundle |
| `GET /v1/address/{id}/people?limit=&offset=` | bundle, name-key clustering |
| `GET /v1/person/{id}/records?shapes=&limit=` | bundle for `addr:` ids, `silver.entity_links` for `hal:` ids |
| `GET /v1/people/search?name=&limit=` | `silver.entity_master` by name |
| `GET /v1/source-record/{shape}/{rowid}?address_id=` | bundle |
| `POST /v1/sql` `{query}` | guarded hatch: parse → LIMIT → EXPLAIN → execute |
| `GET /v1/schema` | curated access paths, limits and caveats |
| `GET /healthz` | liveness |

Record payloads keep the raw vendor column names (`first_name`, `ownername`, `dob_day`)
exactly as `src/occupancy_graph/source/manifest.py` defines them.

## The SQL hatch

`POST /v1/sql` runs four stages in order. **Stage 1 is the primary write guard**, not
defence in depth: `default_transaction_read_only` is a session default that
`BEGIN READ WRITE` defeats (pinned by `tests/test_pool.py`).

1. **Parse** — `pglast`, which wraps **libpg_query, PostgreSQL's own parser sources**, so
   there is no second lexer to keep in sync with the server. The rules are structural facts
   about the parse tree, not text matching: exactly one statement; the top node is a
   `SelectStmt`; no other `*Stmt` node **anywhere** in the tree (which is what refuses DML
   inside a CTE at any depth, without modelling CTEs); no `intoClause` on any `SelectStmt`
   (`SELECT … INTO` creates a table); no `LockingClause` (`FOR UPDATE`/`FOR SHARE`); and
   every `FuncCall` name read from the tree and checked against a family-prefix denylist —
   `pg_read_*`, `pg_ls_*`, `lo_*`, `dblink*`, `pg_sleep*`, plus exact names such as
   `nextval`/`setval`, which look like reads and commit a write. Structural rules bound the
   statement's shape, not its effects, so that function list is load-bearing; a read-only
   guest credential is the backstop behind it.
2. **LIMIT** — the query is wrapped in a capped subquery, never rewritten in place.
3. **EXPLAIN** (never ANALYZE) — refused above the cost ceiling, or on a sequential scan
   over a records table above a second, lower ceiling. See `docs/explain-cost-calibration.md`.
4. **Execute** — explicit `READ ONLY` transaction, `SET LOCAL statement_timeout`, row cap.

A refusal is `422` with the planner's own reason and a hint naming the indexed paths. That
hint is generated from the same list `GET /v1/schema` serves
(`src/occupancy_graph/service/schema_doc.py`), so the two can never tell the agent different
things about which paths are fast.

## Container

    docker build -t occupancy-graph-service .
    docker run --rm -p 8000:8000 -e PARTNER_DSN="postgresql://…" occupancy-graph-service

No volume, no mounted database: the corpus is remote and the image carries only code.

## Tests

    .venv/bin/python -m pytest -q

`tests/conftest.py` starts a `postgres:17` fixture container (`tests/docker-compose.fixture.yml`,
host port 55432) and loads `tests/fixtures/schema.sql` + `seed.sql` into it, so the suite needs
Docker but never the partner corpus or `PARTNER_DSN`.
