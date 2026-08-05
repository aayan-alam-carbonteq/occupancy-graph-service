# The partner-clone container

A local, faithful stand-in for the partner's Postgres corpus (`all_data`, reached in
production only via `PARTNER_DSN` with read-only guest credentials). It is loaded from
Lexington CSVs into a Postgres 17 container running the exact `ddl/*.sql` this service's
test fixture also loads (`tests/docker-compose.fixture.yml`), so the two schemas can never
drift apart. Unlike the fixture, this container's data volume is **named and persistent**:
it survives `docker compose restart`, container recreation, and host reboots, because a
full load is 2.36M rows and nobody should have to pay that cost per test run. The fixture
is torn down (`down -v`) after every session; the clone is not torn down by anything in
this repo.

## What this CANNOT test

Stated up front, because both gaps are easy to forget once queries start returning answers
and everything "looks" like production.

**Performance.** 2.36M rows fit comfortably in RAM — this container's `shared_buffers` is
1 GB, larger than the whole clone. Production's pathology is structural: ~7.6B rows over
~3,749 GB, with cold cache reads costing ~195 ms of random page I/O each. An unindexed
address scan that never finishes in production (the reason `POST /v1/sql`'s EXPLAIN-cost
guard exists at all — see `docs/explain-cost-calibration.md`) returns in milliseconds here,
because there's nothing to be slow against. **Plan shape transfers here; latency does not.**
Use this clone to confirm a query hits the index you expect (`EXPLAIN` node types, join
order, which relation is scanned), never to reason about whether it will be fast enough in
production, and never to recalibrate the SQL-hatch cost ceilings — those stay pinned to
measurements taken against the live corpus.

**Entity-resolution quality.** Production's `silver.entity_master` / `silver.entity_links`
are built by blocking on SSN across the real corpus. This clone's loader synthesises its own
SSNs and its own clusters (see `ddl/004_bench.sql`'s `bench.true_person*` oracle tables,
which record the ground truth the loader itself computed). That means you can test how
`source/search.py` and the `/v1/person/*` routes **consume** the entity graph — clustering
logic, confidence discounting, the `hal:` vs `addr:` id split — but you cannot use this clone
to ask whether a *better* entity-resolution algorithm would produce different or more
correct verdicts. There is no independently-known truth here except the truth the loader
manufactured.

## Build

Bring the container up and load the shared DDL, in the same sorted order the test fixture
uses (the numeric prefix on each `ddl/*.sql` file *is* the dependency order):

```bash
docker compose -f clone/docker-compose.clone.yml up -d --wait

for f in ddl/*.sql; do
  docker compose -f clone/docker-compose.clone.yml exec -T graph-clone-db \
    psql -U clone -d partner_clone -v ON_ERROR_STOP=1 < "$f"
done
```

This creates `records_legacy`, the partitioned `records_new` (five
`records_partitioned_*` children), production's full index set, and the `silver` and
`bench` schemas — but loads no rows. Row loading is a separate, later step: the loader
(Task 11) reads Lexington CSVs and populates `records_legacy` / `records_new` /
`silver.*` / `bench.*` from them. The loader takes the CSV directory as a **configuration
argument** (never a hardcoded path) — that data lives in a different repo and is
DVC-tracked, so pinning a path here would silently break the moment that repo's DVC cache
moved.

### Running the loader

```bash
.venv/bin/python -m clone.load --csv-dir /path/to/occupancy-engine/data/cleaned/lexington
```

Run it as a **module** (`-m clone.load`), from the repo root — never as a script path
(`python clone/load.py`). `clone` is not an installed package (unlike `occupancy_graph`,
which is reachable because `src/` is on the path via this project's editable install), so
`python clone/load.py` puts only the `clone/` directory on `sys.path`, not the repo root,
and every `from clone...` import inside it fails with `ModuleNotFoundError: No module
named 'clone'`. `-m clone.load` runs from the repo root instead, which is on `sys.path` by
construction, so the package resolves.

## Point the graph service at it

No service code changes — none, now or ever. The service only ever learns where its
database is from `PARTNER_DSN`:

```bash
export PARTNER_DSN=postgresql://clone:clone@127.0.0.1:55433/partner_clone
.venv/bin/occupancy-graph-serve --host 127.0.0.1 --port 8017
```

Port 55433 (not 55432, the fixture's port) so a clone and the test suite can run
side by side without colliding.

## Reset

```bash
docker compose -f clone/docker-compose.clone.yml down -v
```

`-v` drops the named volume along with the containers — this throws away every loaded row
and requires rebuilding from `ddl/*.sql` and reloading CSVs. Without `-v`,
`docker compose down` / `up` (or `restart`) leaves the data untouched; that is the entire
reason this container exists instead of reusing the fixture's compose file.

---

## Findings

Measured results, the provenance investigation, the four bugs this work
surfaced, and what cannot be closed with this data all live in
[`docs/partner-clone-findings.md`](../docs/partner-clone-findings.md).

This file stays operational: how to build the clone, point the service at it,
and reset it.
