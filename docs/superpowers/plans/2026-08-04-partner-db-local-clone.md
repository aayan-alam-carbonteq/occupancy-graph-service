# Local Partner-DB Clone Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A locally-managed Postgres that reproduces the partner corpus (`all_data`) faithfully enough to test hypotheses before putting them to the client, loaded from the Lexington CSVs.

**Architecture:** One shared DDL artifact feeds both the throwaway test fixture and a persistent clone container, so they cannot drift. A loader inverts `manifest.py` (rather than restating it) to turn per-shape CSVs into 144-column partner rows, stamps feed identity from `feeds.py`, and down-samples every column to production's measured per-feed population rate. The entity graph is built by reproducing production's *mechanism* — synthetic SSNs populated only where production has them, then blocked on — so its profile emerges rather than being imposed.

**Tech Stack:** Python 3.14, asyncpg, Postgres 17 (docker compose), pytest.

**Spec:** `../../../../docs/superpowers/specs/2026-08-04-partner-db-local-clone-design.md` (workspace repo)

---

## File Structure

```
services/graph/
  ddl/                                  NEW — shared, consumed by fixture AND clone
    001_records.sql                     records_legacy + records_new + 5 partitions (144 cols)
    002_indexes.sql                     full production index set
    003_silver.sql                      entity_master + entity_links + indexes
    004_bench.sql                       bench schema — the oracle, NOT partner surface
  clone/                                NEW
    docker-compose.clone.yml            persistent volume, port 55433
    load.py                             CLI entry point
    profiles/
      records_catalog.json              recorded production column catalog
      feed_population.json              recorded per-feed column population targets
    loader/
      __init__.py
      csvsource.py                      read CSVs, strip padded headers
      mapping.py                        invert manifest.py -> (columns, raw_data)
      feedplan.py                       shape -> source_file / imported_at / table
      population.py                     deterministic per-feed down-sampling
      identity.py                       union-find clusters + synthetic SSN
      entity.py                         entity_master + entity_links via ssn blocking
      writer.py                         COPY into Postgres
  tests/clone/                          NEW
    test_mapping.py  test_feedplan.py  test_population.py
    test_identity.py  test_entity.py
    test_ddl_matches_production.py  test_clone_profile.py
  tests/fixtures/schema.sql             MODIFY — becomes a thin include of ddl/
  tests/conftest.py                     MODIFY — load ddl/*.sql then seed.sql
```

**Boundary rule:** nothing under `clone/` may be imported by `src/occupancy_graph/`. The service must be unaware the clone exists; it is reached only via `PARTNER_DSN`.

---

## Phase 1 — Schema

### Task 1: Extract the shared records DDL, add the two missing partitions

**Files:**
- Create: `ddl/001_records.sql`
- Modify: `tests/fixtures/schema.sql`
- Modify: `tests/conftest.py:28-38`
- Test: `tests/clone/test_ddl_matches_production.py`

- [ ] **Step 1: Record the production catalog as a test fixture**

Create `clone/profiles/records_catalog.json` by running this against the live corpus (requires `PARTNER_DSN`; the file is committed so the test never needs credentials):

```bash
python - <<'PY'
import asyncio, os, json, asyncpg
async def main():
    c = await asyncpg.connect(os.environ["PARTNER_DSN"], server_settings={
        "default_transaction_read_only": "on", "statement_timeout": "60000"})
    rows = await c.fetch("""
        SELECT a.attname, format_type(a.atttypid, a.atttypmod) AS typ
        FROM pg_attribute a WHERE a.attrelid='public.records_legacy'::regclass
          AND a.attnum>0 AND NOT a.attisdropped ORDER BY a.attnum""")
    json.dump([[r["attname"], r["typ"]] for r in rows],
              open("clone/profiles/records_catalog.json","w"), indent=0)
    await c.close()
asyncio.run(main())
PY
```

Expected: a 144-entry JSON array.

- [ ] **Step 2: Write the failing test**

```python
# tests/clone/test_ddl_matches_production.py
"""The check that would have caught `records_partitioned`.

A fixture that models a topology production does not have cannot fail on the
difference -- that is how five of seven shapes came to name a nonexistent
relation while 548 tests passed. This asserts our DDL against a recorded
catalog of the real thing.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

CATALOG = Path(__file__).parents[2] / "clone" / "profiles" / "records_catalog.json"


async def test_records_legacy_matches_the_production_catalog(fixture_pool):
    expected = [(name, typ) for name, typ in json.loads(CATALOG.read_text())]
    async with fixture_pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT a.attname, format_type(a.atttypid, a.atttypmod) AS typ
            FROM pg_attribute a WHERE a.attrelid='public.records_legacy'::regclass
              AND a.attnum>0 AND NOT a.attisdropped ORDER BY a.attnum""")
    actual = [(r["attname"], r["typ"]) for r in rows]
    assert len(actual) == 144
    assert actual == expected


async def test_records_new_has_all_five_production_partitions(fixture_pool):
    async with fixture_pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT c.relname, pg_get_expr(c.relpartbound, c.oid) AS bound
            FROM pg_class c JOIN pg_inherits i ON i.inhrelid=c.oid
            WHERE i.inhparent='public.records_new'::regclass ORDER BY c.relname""")
    got = {r["relname"]: r["bound"] for r in rows}
    assert set(got) == {
        "records_partitioned_p20251201", "records_partitioned_p20260101",
        "records_partitioned_p20260201", "records_partitioned_p20260301",
        "records_partitioned_default",
    }
    assert "2025-12-01" in got["records_partitioned_p20251201"]
    assert "2026-01-01" in got["records_partitioned_p20260101"]
    assert got["records_partitioned_default"] == "DEFAULT"
```

- [ ] **Step 3: Run it and watch it fail**

Run: `.venv/bin/python -m pytest tests/clone/test_ddl_matches_production.py -v`
Expected: FAIL — `test_records_new_has_all_five_production_partitions` errors because only three partitions exist.

- [ ] **Step 4: Move the DDL into `ddl/001_records.sql`**

Cut the `DROP …`, `CREATE TABLE public.records_legacy (…)`, `CREATE TABLE public.records_new (…) PARTITION BY RANGE (imported_at)` and partition blocks out of `tests/fixtures/schema.sql` into a new `ddl/001_records.sql`, preserving them byte-for-byte (they are already verified exact — do not retype the 144 columns). Then add the two missing partitions:

```sql
-- Production has FIVE partitions. p20251201 holds 2019.2_USA_Consumer_LF and
-- 24mm loan-txt; p20260101 holds 2019.2_USA_Consumer_LF and PD loan_master.
-- Their absence here is why the fixture could not represent the base feed's
-- records_new half.
CREATE TABLE public.records_partitioned_p20251201
  PARTITION OF public.records_new
  FOR VALUES FROM ('2025-12-01') TO ('2026-01-01');

CREATE TABLE public.records_partitioned_p20260101
  PARTITION OF public.records_new
  FOR VALUES FROM ('2026-01-01') TO ('2026-02-01');
```

- [ ] **Step 5: Make `schema.sql` a pointer, and teach conftest to load `ddl/`**

`tests/fixtures/schema.sql` keeps only what is fixture-specific. Replace its records/silver DDL with a comment:

```sql
-- The records + silver DDL now lives in ../../ddl/ and is shared with the
-- persistent clone (clone/docker-compose.clone.yml), so the two can never
-- drift. conftest.py loads ddl/*.sql before this file. Anything remaining
-- here is fixture-only.
```

In `tests/conftest.py`, replace the load loop:

```python
DDL_DIR = Path(__file__).parents[1] / "ddl"


def _psql(sql_path: Path) -> None:
    subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE), "exec", "-T",
         "graph-fixture-db", "psql", "-U", "graph", "-d", "graph_fixture",
         "-v", "ON_ERROR_STOP=1"],
        stdin=sql_path.open("rb"), check=True,
    )


@pytest.fixture(scope="session")
def fixture_db() -> str:
    """Start the fixture Postgres, load shared DDL + fixture seed, yield the DSN."""
    _compose("up", "-d", "--wait")
    try:
        for path in sorted(DDL_DIR.glob("*.sql")):
            _psql(path)
        for name in ("schema.sql", "seed.sql"):
            sql = FIXTURE_DIR / name
            if sql.exists():
                _psql(sql)
        yield TEST_DSN
    finally:
        _compose("down", "-v")
```

- [ ] **Step 6: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS — the two new tests pass and all 562 existing tests still pass (the DDL is byte-identical, only relocated).

- [ ] **Step 7: Commit**

```bash
git add ddl/001_records.sql clone/profiles/records_catalog.json \
        tests/fixtures/schema.sql tests/conftest.py tests/clone/test_ddl_matches_production.py
git commit -m "refactor(ddl): share records DDL between fixture and clone; add the 2 missing partitions"
```

---

### Task 2: Full production index set

**Files:**
- Create: `ddl/002_indexes.sql`
- Test: `tests/clone/test_ddl_matches_production.py` (extend)

- [ ] **Step 1: Write the failing test**

Append to `tests/clone/test_ddl_matches_production.py`:

```python
# Every index production carries on records_legacy. Access-path behaviour is
# the whole point of the clone: a missing index silently changes the plan the
# experiments are meant to observe.
EXPECTED_LEGACY_INDEXES = {
    "records_pkey", "idx_records_zip", "idx_records_lastname_zip_house",
    "idx_records_legacy_zip_house", "idx_records_legacy_state_city",
    "idx_records_first_last", "idx_records_last_name_trgm", "idx_records_dob",
    "idx_records_email", "idx_records_email2", "idx_records_mobile",
    "idx_records_phone", "idx_records_ssn", "idx_records_ssn2",
}


async def test_records_legacy_carries_the_production_index_set(fixture_pool):
    async with fixture_pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT indexname FROM pg_indexes
            WHERE schemaname='public' AND tablename='records_legacy'""")
    assert {r["indexname"] for r in rows} == EXPECTED_LEGACY_INDEXES


async def test_every_partition_carries_a_record_id_index(fixture_pool):
    """record_id IS indexed on every relation in production. The cost there is
    heap I/O, not the index -- see source/search.py::rows_for_links."""
    async with fixture_pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT tablename FROM pg_indexes
            WHERE schemaname='public' AND tablename LIKE 'records_partitioned_%'
              AND indexdef ~ '\\(record_id'""")
    assert len({r["tablename"] for r in rows}) == 5
```

- [ ] **Step 2: Run it and watch it fail**

Run: `.venv/bin/python -m pytest tests/clone/test_ddl_matches_production.py -v`
Expected: FAIL — only ~4 legacy indexes exist.

- [ ] **Step 3: Write `ddl/002_indexes.sql`**

```sql
-- Production's index set, dumped from pg_indexes on 2026-08-04. Reproduced in
-- full because the clone exists to observe ACCESS PATHS: a missing index
-- changes the plan, which is the one thing that transfers to production even
-- though latency does not.
--
-- pg_trgm is required by the last_name GIN indexes.
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ---- records_legacy (14) ----------------------------------------------------
CREATE UNIQUE INDEX records_pkey ON public.records_legacy USING btree (record_id);
CREATE INDEX idx_records_zip ON public.records_legacy USING btree (zip) WHERE zip IS NOT NULL;
CREATE INDEX idx_records_lastname_zip_house ON public.records_legacy USING btree (last_name, zip, house_number);
CREATE INDEX idx_records_legacy_zip_house ON public.records_legacy USING btree (zip, house_number);
CREATE INDEX idx_records_legacy_state_city ON public.records_legacy USING btree (upper(state), upper(city));
CREATE INDEX idx_records_first_last ON public.records_legacy USING btree (first_name, last_name)
  WHERE first_name IS NOT NULL AND last_name IS NOT NULL;
CREATE INDEX idx_records_last_name_trgm ON public.records_legacy USING gin (last_name gin_trgm_ops)
  WHERE last_name IS NOT NULL;
CREATE INDEX idx_records_dob ON public.records_legacy USING btree (dob) WHERE dob IS NOT NULL;
CREATE INDEX idx_records_email ON public.records_legacy USING btree (lower(email)) WHERE email IS NOT NULL;
CREATE INDEX idx_records_email2 ON public.records_legacy USING btree (lower(email2)) WHERE email2 IS NOT NULL;
CREATE INDEX idx_records_mobile ON public.records_legacy USING btree (mobile) WHERE mobile IS NOT NULL;
CREATE INDEX idx_records_phone ON public.records_legacy USING btree (phone) WHERE phone IS NOT NULL;
CREATE INDEX idx_records_ssn ON public.records_legacy USING btree (ssn) WHERE ssn IS NOT NULL;
CREATE INDEX idx_records_ssn2 ON public.records_legacy USING btree (ssn2) WHERE ssn2 IS NOT NULL;

-- ---- records_new: declared on the PARENT so every partition inherits -------
-- Production declares these per-partition; declaring on the parent produces
-- the same per-partition indexes and cannot skip a partition by accident.
CREATE INDEX ON public.records_new USING btree (record_id);
CREATE INDEX ON public.records_new USING btree (zip);
CREATE INDEX ON public.records_new USING btree (address_id);
CREATE INDEX ON public.records_new USING btree (city, state);
CREATE INDEX ON public.records_new USING btree (upper(state), upper(city));
CREATE INDEX ON public.records_new USING btree (last_name, zip, house_number);
CREATE INDEX ON public.records_new USING btree (first_name, last_name);
CREATE INDEX ON public.records_new USING gin (last_name gin_trgm_ops);
CREATE INDEX ON public.records_new USING gin (tsv_name);
CREATE INDEX ON public.records_new USING btree (dob) WHERE dob IS NOT NULL;
CREATE INDEX ON public.records_new USING btree (phone);
CREATE INDEX ON public.records_new USING btree (mobile);
CREATE INDEX ON public.records_new USING btree (ssn);
CREATE INDEX ON public.records_new USING btree (ssn2);
CREATE INDEX ON public.records_new USING btree (email);
CREATE INDEX ON public.records_new USING btree (lower(email));
CREATE INDEX ON public.records_new USING btree (email2);
CREATE INDEX ON public.records_new USING btree (lower(email2));
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/clone/test_ddl_matches_production.py -q`
Expected: PASS (2 new index tests + the 2 from Task 1).

- [ ] **Step 5: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS — no regressions. Note some existing tests assert plan shapes; the added indexes may change them. If any fail, the plan they assert is now *more* production-like — update the assertion and say so in the commit.

- [ ] **Step 6: Commit**

```bash
git add ddl/002_indexes.sql tests/clone/test_ddl_matches_production.py
git commit -m "feat(ddl): reproduce production's full index set on records_legacy and records_new"
```

---

### Task 3: silver + bench DDL

**Files:**
- Create: `ddl/003_silver.sql`, `ddl/004_bench.sql`
- Modify: `tests/fixtures/schema.sql` (drop its silver block)

- [ ] **Step 1: Write `ddl/003_silver.sql`**

```sql
CREATE SCHEMA IF NOT EXISTS silver;

CREATE TABLE silver.entity_master (
  hal_id char(15) PRIMARY KEY,
  canonical_first_name varchar(100), canonical_last_name varchar(100),
  canonical_ssn char(9), canonical_email varchar(255), canonical_phone char(10),
  canonical_address_line1 varchar(255), canonical_city varchar(100),
  canonical_state char(2), canonical_zip varchar(10), canonical_dob date,
  record_count integer,
  first_seen_at timestamptz, last_seen_at timestamptz,
  created_at timestamptz, updated_at timestamptz,
  is_merged boolean, merged_into_hal_id char(15),
  canonical_source_table varchar(50), canonical_record_id bigint,
  canonical_selection_score numeric(5,2), canonical_selection_evidence jsonb,
  anomaly_flags jsonb, is_suspicious boolean,
  identity_confidence numeric(5,2), corroboration_evidence jsonb
);

CREATE TABLE silver.entity_links (
  id bigserial,
  hal_id char(15) NOT NULL,
  source_table varchar(50) NOT NULL,
  record_id bigint NOT NULL,
  match_type varchar(50),
  confidence numeric(3,2),
  created_at timestamptz DEFAULT now()
);

-- Indexed BOTH ways in production: by hal_id (215 ms) and UNIQUE on
-- (source_table, record_id) (81 ms). source/search.py depends on both.
CREATE INDEX ON silver.entity_links USING btree (hal_id);
CREATE UNIQUE INDEX ON silver.entity_links USING btree (source_table, record_id);
CREATE INDEX ON silver.entity_master USING btree (upper(canonical_last_name));
```

- [ ] **Step 2: Write `ddl/004_bench.sql`**

```sql
-- THE ORACLE. Deliberately OUTSIDE the partner surface: nothing in
-- src/occupancy_graph reads this schema, and the clone would still be faithful
-- if it were dropped. It records the TRUE person clusters the loader computed,
-- so correctness tests can assert exactly which records a hal_id must return --
-- something production can never tell us, because we do not know its ground
-- truth.
CREATE SCHEMA IF NOT EXISTS bench;

CREATE TABLE bench.true_person (
  person_id bigint PRIMARY KEY,
  synthetic_ssn char(9) NOT NULL,
  first_name text, last_name text,
  address text, zip text,
  record_count integer NOT NULL
);

CREATE TABLE bench.true_person_record (
  person_id bigint NOT NULL,
  source_table text NOT NULL,
  record_id bigint NOT NULL,
  shape text NOT NULL,
  PRIMARY KEY (source_table, record_id)
);

CREATE INDEX ON bench.true_person_record (person_id);
```

- [ ] **Step 3: Remove the silver block from `tests/fixtures/schema.sql`**

Delete its `CREATE SCHEMA silver;`, `CREATE TABLE silver.entity_master …`, `CREATE TABLE silver.entity_links …` and their indexes — `ddl/003_silver.sql` now owns them and conftest loads it first.

- [ ] **Step 4: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS, 562+ tests. The silver DDL is a superset of the fixture's (it adds the real column list), so existing seed inserts still apply.

- [ ] **Step 5: Commit**

```bash
git add ddl/003_silver.sql ddl/004_bench.sql tests/fixtures/schema.sql
git commit -m "feat(ddl): production-shaped silver entity graph + bench oracle schema"
```

---

### Task 4: The clone container

**Files:**
- Create: `clone/docker-compose.clone.yml`, `clone/README.md`

- [ ] **Step 1: Write the compose file**

```yaml
# The PERSISTENT clone. Distinct from tests/docker-compose.fixture.yml, which is
# torn down (`down -v`) after every test session. This one keeps its volume so a
# 2.36M-row load survives restarts.
services:
  graph-clone-db:
    image: postgres:17
    environment:
      POSTGRES_USER: clone
      POSTGRES_PASSWORD: clone
      POSTGRES_DB: partner_clone
    ports:
      - "55433:5432"
    volumes:
      - graph-clone-data:/var/lib/postgresql/data
    # Sized for a 2.36M-row load with the full index set; the defaults make
    # index builds crawl.
    command:
      - postgres
      - -c
      - shared_buffers=1GB
      - -c
      - maintenance_work_mem=1GB
      - -c
      - work_mem=64MB
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U clone -d partner_clone"]
      interval: 2s
      timeout: 3s
      retries: 30

volumes:
  graph-clone-data:
```

- [ ] **Step 2: Write `clone/README.md`**

````markdown
# Local partner-DB clone

A faithful local stand-in for the partner corpus (`all_data`), loaded from the
Lexington CSVs. See the design spec for what it can and cannot test — in
particular it reproduces **access-path shape but not latency**, and it cannot
be used to evaluate entity-resolution quality.

## Build

```bash
docker compose -f clone/docker-compose.clone.yml up -d --wait
for f in ddl/*.sql; do
  docker compose -f clone/docker-compose.clone.yml exec -T graph-clone-db \
    psql -U clone -d partner_clone -v ON_ERROR_STOP=1 < "$f"
done
.venv/bin/python clone/load.py --csv-dir /path/to/occupancy-engine/data/cleaned/lexington
```

## Use it

```bash
export PARTNER_DSN=postgresql://clone:clone@127.0.0.1:55433/partner_clone
.venv/bin/occupancy-graph-serve --host 127.0.0.1 --port 8017
```

No service code changes — the clone is reached only through `PARTNER_DSN`.

## Reset

```bash
docker compose -f clone/docker-compose.clone.yml down -v
```
````

- [ ] **Step 3: Verify it starts and takes the DDL**

```bash
docker compose -f clone/docker-compose.clone.yml up -d --wait
for f in ddl/*.sql; do docker compose -f clone/docker-compose.clone.yml exec -T \
  graph-clone-db psql -U clone -d partner_clone -v ON_ERROR_STOP=1 < "$f"; done
docker compose -f clone/docker-compose.clone.yml exec -T graph-clone-db \
  psql -U clone -d partner_clone -c "\dt public.*"
```

Expected: `records_legacy`, `records_new`, and the five `records_partitioned_*` tables listed, no errors.

- [ ] **Step 4: Commit**

```bash
git add clone/docker-compose.clone.yml clone/README.md
git commit -m "feat(clone): persistent partner-clone container"
```

---

## Phase 2 — Record ETL

### Task 5: CSV source with padded-header handling

**Files:**
- Create: `clone/loader/__init__.py`, `clone/loader/csvsource.py`
- Test: `tests/clone/test_csvsource.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/clone/test_csvsource.py
from pathlib import Path

from clone.loader.csvsource import read_shape_csv


def test_padded_headers_and_values_are_stripped(tmp_path: Path):
    """base.csv ships space-padded headers ("firstname         ,"). This broke a
    real parse during design -- `row["id"]` returned None and the shape looked
    like it had no id column at all."""
    p = tmp_path / "base.csv"
    p.write_text("firstname         , lastname , zip  \n" " JANE , DOE , 40505 \n")
    rows = list(read_shape_csv(p))
    assert rows == [{"firstname": "JANE", "lastname": "DOE", "zip": "40505"}]


def test_blank_values_become_empty_strings_not_none(tmp_path: Path):
    p = tmp_path / "x.csv"
    p.write_text("a,b\n1,\n")
    assert list(read_shape_csv(p)) == [{"a": "1", "b": ""}]
```

- [ ] **Step 2: Run it and watch it fail**

Run: `.venv/bin/python -m pytest tests/clone/test_csvsource.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'clone'`.

- [ ] **Step 3: Implement**

```python
# clone/loader/csvsource.py
"""Read a shape CSV into stripped dicts.

base.csv is space-padded in both headers and values; every other file is not.
Stripping unconditionally costs nothing and removes a whole class of silent
key-miss bugs.
"""
from __future__ import annotations

import csv
from collections.abc import Iterator
from pathlib import Path


def read_shape_csv(path: Path) -> Iterator[dict[str, str]]:
    with path.open(newline="", encoding="utf-8", errors="replace") as handle:
        for row in csv.DictReader(handle):
            yield {
                (key or "").strip(): (value or "").strip()
                for key, value in row.items()
                if key is not None
            }
```

Add an empty `clone/__init__.py` and `clone/loader/__init__.py` so the package imports.

- [ ] **Step 4: Run the test**

Run: `.venv/bin/python -m pytest tests/clone/test_csvsource.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add clone/__init__.py clone/loader/__init__.py clone/loader/csvsource.py tests/clone/test_csvsource.py
git commit -m "feat(clone): CSV reader that strips padded headers"
```

---

### Task 6: Invert `manifest.py` into partner columns + raw_data

**Files:**
- Create: `clone/loader/mapping.py`
- Test: `tests/clone/test_mapping.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/clone/test_mapping.py
"""The ETL must READ manifest.py, never restate it.

manifest.py is the single source of truth for shape<->column mapping. A loader
carrying its own copy would let the clone silently desync from the contract the
service reads -- the same class of drift that let records_partitioned survive.
"""
from clone.loader.mapping import partner_row_for


def test_col_origins_become_partner_columns():
    cols, raw = partner_row_for("utility", {
        "first_name": "PAT", "last_name": "TENANT", "zip": "40505"})
    assert cols["first_name"] == "PAT"
    assert cols["last_name"] == "TENANT"
    assert cols["zip"] == "40505"
    assert raw == {}


def test_raw_origins_go_into_raw_data_not_a_column():
    """trace.email_02 is raw("Email_02") -- it lives in the jsonb, and there is
    no email_02 column on the partner table to receive it."""
    cols, raw = partner_row_for("trace", {"email_02": "x@y.com"})
    assert raw == {"Email_02": "x@y.com"}
    assert "email_02" not in cols


def test_aliased_columns_use_the_partner_name():
    """trace.cellphone is col("mobile")."""
    cols, _ = partner_row_for("trace", {"cellphone": "5551112222"})
    assert cols == {"mobile": "5551112222"}


def test_derived_and_absent_origins_write_nothing():
    """trace.housenumber is derived() -- computed at read time from address.
    Writing it would populate the resident-hop's anchor column on a feed where
    production has it NULL, inflating local hop coverage (spec §7)."""
    cols, raw = partner_row_for("trace", {"housenumber": "1104", "address": "1104 SPRING RUN RD"})
    assert "house_number" not in cols
    assert cols == {"address": "1104 SPRING RUN RD"}
    assert raw == {}


def test_empty_values_are_omitted_entirely():
    cols, raw = partner_row_for("utility", {"first_name": "", "last_name": "DOE"})
    assert cols == {"last_name": "DOE"}
```

- [ ] **Step 2: Run it and watch it fail**

Run: `.venv/bin/python -m pytest tests/clone/test_mapping.py -v`
Expected: FAIL — `No module named 'clone.loader.mapping'`.

- [ ] **Step 3: Implement**

```python
# clone/loader/mapping.py
"""Invert manifest.py: shape CSV row -> (partner columns, raw_data).

manifest.py maps partner storage -> shape contract. The clone needs the reverse,
and it derives it by READING the manifest rather than restating it, so the two
can never disagree.

  col(X)      -> write the value into partner column X
  raw(K)      -> write the value into raw_data[K]
  derived(fn) -> write NOTHING; the read path computes it
  absent()    -> write NOTHING; declared unavailable in the corpus

`derived` writing nothing is load-bearing, not an optimisation: trace/auto/base
declare `housenumber` as derived, and production genuinely has house_number NULL
on those feeds. Materialising it would hand the resident-hop anchor rows
production does not have.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from occupancy_graph.source.manifest import SHAPES


def partner_row_for(shape: str, csv_row: Mapping[str, str]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return (columns, raw_data) for one CSV row of `shape`."""
    spec = SHAPES[shape]
    columns: dict[str, Any] = {}
    raw_data: dict[str, Any] = {}
    for field_name, origin in spec.fields.items():
        value = csv_row.get(field_name)
        if value is None or value == "":
            continue
        if origin.kind == "col":
            columns[origin.key] = value
        elif origin.kind == "raw":
            raw_data[origin.key] = value
        # "derived" and "absent" intentionally write nothing.
    return columns, raw_data
```

- [ ] **Step 4: Run the test**

Run: `.venv/bin/python -m pytest tests/clone/test_mapping.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add clone/loader/mapping.py tests/clone/test_mapping.py
git commit -m "feat(clone): derive the ETL mapping by inverting manifest.py"
```

---

### Task 7: Feed identity and table routing

**Files:**
- Create: `clone/loader/feedplan.py`
- Test: `tests/clone/test_feedplan.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/clone/test_feedplan.py
"""source_file and imported_at are not cosmetic: feeds.py selects rows by
source_file LIKE, and bounds the tax scan to one partition by imported_at. A row
whose feed identity is wrong is invisible to the service that loaded it."""
import re

from occupancy_graph.source.feeds import FEEDS, _like_to_regex

from clone.loader.feedplan import FEED_PLANS, plan_for


def test_every_zip_shape_has_a_plan():
    for shape in ("utility", "trace", "base", "loan", "auto", "tax"):
        assert plan_for(shape), f"{shape} has no feed plan"


def test_every_plans_source_file_matches_its_own_feeds_pattern():
    """The round trip that would have caught records_partitioned."""
    for plan in FEED_PLANS:
        patterns = [_like_to_regex(p) for p in FEEDS[plan.shape].patterns]
        assert any(rx.match(plan.source_file) for rx in patterns), (
            f"{plan.shape}: {plan.source_file!r} matches none of "
            f"{FEEDS[plan.shape].patterns}")


def test_plans_target_the_table_feeds_py_declares():
    for plan in FEED_PLANS:
        assert plan.table in FEEDS[plan.shape].tables


def test_tax_lands_inside_the_partition_feeds_py_bounds_it_to():
    """feeds.py bounds tax to [2026-03-01, 2026-04-01). Outside it, the assessor
    rows exist but no query can see them."""
    tax = [p for p in FEED_PLANS if p.shape == "tax"]
    assert tax
    for plan in tax:
        assert plan.imported_at.startswith("2026-03")


def test_loan_and_drive_share_one_payday_feed():
    """Production has no drive feed: drive IS loan-with-a-licence."""
    loan = {p.source_file for p in FEED_PLANS if p.shape == "loan"}
    assert loan
    assert not [p for p in FEED_PLANS if p.shape == "drive"]
```

- [ ] **Step 2: Run it and watch it fail**

Run: `.venv/bin/python -m pytest tests/clone/test_feedplan.py -v`
Expected: FAIL — `No module named 'clone.loader.feedplan'`.

- [ ] **Step 3: Implement**

```python
# clone/loader/feedplan.py
"""Which physical feed each shape's rows are written as.

source_file strings are REAL production directory names (verified against the
live corpus 2026-08-03/04), because feeds.py selects on them with LIKE. The
imported_at date routes a row into the production partition that holds that feed.

There is deliberately NO drive plan: production has no drive feed. drive rows are
payday rows carrying dl_number, folded into loan by loader/identity.py.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FeedPlan:
    shape: str
    table: str            # "records_legacy" | "records_new"
    source_file: str
    imported_at: str | None   # None for records_legacy (not partitioned)
    weight: float = 1.0       # share of the shape's rows routed to this plan


FEED_PLANS: tuple[FeedPlan, ...] = (
    FeedPlan("utility", "records_legacy", "Export Utility Stripped Down/Utility_ky/Utility_ky.csv", None),
    FeedPlan("trace", "records_legacy",
             "Trace Skipping Oct 2025/2025_Historical_database_1/2025_Historical_database_1.csv", None),
    # base spans both roots in production. feed_id_coverage proportions are
    # ~1:7 toward records_new (USCRM+CoReg 18.6k vs 2019.2_LF 130.6k sampled).
    FeedPlan("base", "records_legacy", "2026.1-USCRM/uscrm_ky.csv", None, weight=0.125),
    FeedPlan("base", "records_new", "2019.2_USA_Consumer_LF/lf_ky.csv", "2026-01-15", weight=0.875),
    FeedPlan("loan", "records_new", "Payday_Big_2026/payday_ky.csv", "2026-02-15"),
    FeedPlan("auto", "records_new", "auto-verified/auto_ky.csv", "2026-03-15"),
    # MUST be inside [2026-03-01, 2026-04-01): feeds.py prunes the tax scan to
    # that partition, so a row outside it is silently unreachable.
    FeedPlan("tax", "records_new", "property_owner_49/property_owner_ky.csv", "2026-03-15"),
)


def plan_for(shape: str) -> tuple[FeedPlan, ...]:
    return tuple(p for p in FEED_PLANS if p.shape == shape)
```

- [ ] **Step 4: Run the test**

Run: `.venv/bin/python -m pytest tests/clone/test_feedplan.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add clone/loader/feedplan.py tests/clone/test_feedplan.py
git commit -m "feat(clone): feed identity and partition routing verified against feeds.py"
```

---

### Task 8: Deterministic per-feed population down-sampling

**Files:**
- Create: `clone/loader/population.py`, `clone/profiles/feed_population.json`
- Test: `tests/clone/test_population.py`

- [ ] **Step 1: Record the production targets**

Create `clone/profiles/feed_population.json`. Values are `feed_id_coverage` percentages, read live on 2026-08-04, keyed by the *shape* the loader is writing:

```json
{
  "utility": {"ssn": 0.0,  "dob": 93.9, "phone": 41.9, "email": 0.0,  "house_number": 0.0},
  "trace":   {"ssn": 0.0,  "dob": 34.2, "phone": 76.0, "email": 8.9,  "house_number": 0.0},
  "base":    {"ssn": 0.0,  "dob": 86.0, "phone": 0.0,  "email": 53.9, "house_number": 100.0},
  "loan":    {"ssn": 95.8, "dob": 94.4, "phone": 84.1, "email": 95.9, "house_number": 0.0},
  "auto":    {"ssn": 0.0,  "dob": 0.0,  "phone": 15.1, "email": 0.0,  "house_number": 0.0},
  "tax":     {"ssn": 0.0,  "dob": 0.0,  "phone": 0.0,  "email": 0.0,  "house_number": 0.0}
}
```

- [ ] **Step 2: Write the failing test**

```python
# tests/clone/test_population.py
"""Column population is behaviour, not decoration.

packet_gates.ts gates SIX OF SEVEN packets on field presence, so a clone with
phone at 90% where production has 41.9% runs packets production would skip, and
every coverage number drifts.
"""
from clone.loader.population import keep_value, load_targets


def test_zero_percent_columns_are_always_dropped():
    """utility.ssn is 0.0% in production. This is what makes the entity graph
    link only payday rows -- see spec §6."""
    targets = load_targets()
    assert not any(keep_value("utility", "ssn", f"row{i}", targets) for i in range(200))


def test_hundred_percent_columns_are_always_kept():
    targets = load_targets()
    assert all(keep_value("base", "house_number", f"row{i}", targets) for i in range(200))


def test_partial_columns_land_near_the_target_rate():
    targets = load_targets()
    kept = sum(keep_value("trace", "phone", f"row{i}", targets) for i in range(10_000))
    assert 74.0 <= kept / 100 <= 78.0     # target 76.0%


def test_sampling_is_deterministic_not_random():
    """Reruns must be byte-identical or nothing downstream is reproducible."""
    targets = load_targets()
    first = [keep_value("trace", "phone", f"row{i}", targets) for i in range(500)]
    second = [keep_value("trace", "phone", f"row{i}", targets) for i in range(500)]
    assert first == second


def test_unlisted_columns_are_kept_untouched():
    targets = load_targets()
    assert keep_value("utility", "address", "row1", targets) is True
```

- [ ] **Step 3: Run it and watch it fail**

Run: `.venv/bin/python -m pytest tests/clone/test_population.py -v`
Expected: FAIL — `No module named 'clone.loader.population'`.

- [ ] **Step 4: Implement**

```python
# clone/loader/population.py
"""Down-sample columns to production's per-feed population rate.

Hash-based, never RNG: the same row must make the same decision on every run, or
the clone is not reproducible and no measurement taken against it is comparable
to the last one.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path

PROFILE = Path(__file__).parents[1] / "profiles" / "feed_population.json"


def load_targets() -> Mapping[str, Mapping[str, float]]:
    return json.loads(PROFILE.read_text())


def keep_value(shape: str, column: str, row_key: str,
               targets: Mapping[str, Mapping[str, float]]) -> bool:
    """True if this row should carry `column`.

    A column with no recorded target is kept unchanged -- absence of a target
    means "we never measured this", not "production has none".
    """
    pct = targets.get(shape, {}).get(column)
    if pct is None:
        return True
    if pct <= 0.0:
        return False
    if pct >= 100.0:
        return True
    digest = hashlib.blake2b(f"{shape}|{column}|{row_key}".encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big") % 10_000 < int(pct * 100)
```

- [ ] **Step 5: Run the test**

Run: `.venv/bin/python -m pytest tests/clone/test_population.py -v`
Expected: PASS (5 tests).

- [ ] **Step 6: Commit**

```bash
git add clone/loader/population.py clone/profiles/feed_population.json tests/clone/test_population.py
git commit -m "feat(clone): deterministic per-feed column population matching production"
```

---

### Task 9: Person clustering, synthetic SSN, and the drive fold-in

**Files:**
- Create: `clone/loader/identity.py`
- Test: `tests/clone/test_identity.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/clone/test_identity.py
from clone.loader.identity import (
    DRIVE_JOIN_KEY, PersonIndex, synthetic_ssn,
)


def test_union_find_joins_two_addresses_through_a_shared_phone():
    """THE property owner-elsewhere traversal depends on. Grouping by
    (name, address) would split every mover into separate entities and the
    traversal would return nothing. Trace has addresses/id mean 1.84, max 17."""
    idx = PersonIndex()
    a = idx.add(first="JANE", last="DOE", address="1 A ST", zip="40505", dob="", phone="5551112222")
    b = idx.add(first="JANE", last="DOE", address="9 B RD", zip="40505", dob="", phone="5551112222")
    assert idx.person_of(a) == idx.person_of(b)


def test_different_people_at_one_address_stay_separate():
    idx = PersonIndex()
    a = idx.add(first="JANE", last="DOE", address="1 A ST", zip="40505", dob="", phone="")
    b = idx.add(first="JOHN", last="SMITH", address="1 A ST", zip="40505", dob="", phone="")
    assert idx.person_of(a) != idx.person_of(b)


def test_dob_links_across_addresses_when_phone_is_absent():
    idx = PersonIndex()
    a = idx.add(first="JANE", last="DOE", address="1 A ST", zip="40505", dob="1980-01-01", phone="")
    b = idx.add(first="JANE", last="DOE", address="9 B RD", zip="40515", dob="1980-01-01", phone="")
    assert idx.person_of(a) == idx.person_of(b)


def test_synthetic_ssn_uses_the_never_issued_900_range():
    """900-999 area numbers are never issued by the SSA, so a synthetic SSN can
    never collide with a real person's."""
    for person_id in (0, 1, 12345, 999_999):
        ssn = synthetic_ssn(person_id)
        assert len(ssn) == 9 and ssn.isdigit()
        assert 900 <= int(ssn[:3]) <= 999


def test_synthetic_ssn_is_stable_and_distinct():
    assert synthetic_ssn(42) == synthetic_ssn(42)
    assert len({synthetic_ssn(i) for i in range(5_000)}) == 5_000


def test_drive_join_key_is_person_and_address_not_id():
    """`id` is NOT a cross-shape key: cd076219 is GARY HILES in loan and
    BRIANNA HILES in drive. Measured match rates: (id,first,last) 29.8%,
    (addr,zip,first,last) 55.4%."""
    row = {"id": "cd076219", "address": "934 Dayton Ave", "zip": "40505",
           "firstname": "gary", "lastname": "hiles"}
    assert DRIVE_JOIN_KEY(row) == ("934 DAYTON AVE", "40505", "GARY", "HILES")
```

- [ ] **Step 2: Run it and watch it fail**

Run: `.venv/bin/python -m pytest tests/clone/test_identity.py -v`
Expected: FAIL — `No module named 'clone.loader.identity'`.

- [ ] **Step 3: Implement**

```python
# clone/loader/identity.py
"""True person clusters, the synthetic SSN they get, and the drive fold-in.

CLUSTERING IS UNION-FIND, NOT GROUPING. A person seen at address A and address B
must become ONE entity, or `owner elsewhere` -- the heuristic whose entire job is
finding the owner's records at OTHER addresses -- returns nothing.

The clusters computed here are the ORACLE (bench schema). They are NOT
entity_master: that is built in entity.py from the much smaller subset carrying a
synthetic SSN, mirroring production's ssn blocking.
"""
from __future__ import annotations

from collections.abc import Mapping

_U = lambda value: (value or "").strip().upper()

# Measured 2026-08-04: this key matches 55.4% of drive rows to a loan row,
# against 29.8% for (id, first, last). `id` is reliable only WITHIN a shape.
DRIVE_JOIN_KEY = lambda row: (
    _U(row.get("address")), (row.get("zip") or "").strip(),
    _U(row.get("firstname")), _U(row.get("lastname")),
)


def synthetic_ssn(person_id: int) -> str:
    """A 9-digit SSN in the 900-999 area range, which the SSA never issues."""
    area = 900 + person_id % 100
    group = person_id // 100 % 100
    serial = person_id // 10_000 % 10_000
    return f"{area:03d}{group:02d}{serial:04d}"


class PersonIndex:
    """Union-find over three blocking keys, strongest first.

        (first, last, dob)            -> name_dob
        (first, last, phone)          -> name_phone
        (first, last, address, zip)   -> name_address
    """

    def __init__(self) -> None:
        self._parent: list[int] = []
        self._keys: dict[tuple, int] = {}

    def add(self, *, first: str, last: str, address: str, zip: str,
            dob: str, phone: str) -> int:
        node = len(self._parent)
        self._parent.append(node)
        first, last = _U(first), _U(last)
        if not last:
            return node          # unnameable rows never join a cluster
        for key in (
            ("dob", first, last, (dob or "").strip()) if dob else None,
            ("phone", first, last, (phone or "").strip()) if phone else None,
            ("addr", first, last, _U(address), (zip or "").strip()) if address and zip else None,
        ):
            if key is None:
                continue
            seen = self._keys.get(key)
            if seen is None:
                self._keys[key] = node
            else:
                self._union(node, seen)
        return node

    def _find(self, node: int) -> int:
        root = node
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[node] != root:      # path compression
            self._parent[node], node = root, self._parent[node]
        return root

    def _union(self, a: int, b: int) -> None:
        ra, rb = self._find(a), self._find(b)
        if ra != rb:
            self._parent[ra] = rb

    def person_of(self, node: int) -> int:
        return self._find(node)

    def person_ids(self) -> Mapping[int, int]:
        """node -> dense person_id, assigned in first-seen order for stability."""
        dense: dict[int, int] = {}
        out: dict[int, int] = {}
        for node in range(len(self._parent)):
            root = self._find(node)
            if root not in dense:
                dense[root] = len(dense)
            out[node] = dense[root]
        return out
```

- [ ] **Step 4: Run the test**

Run: `.venv/bin/python -m pytest tests/clone/test_identity.py -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add clone/loader/identity.py tests/clone/test_identity.py
git commit -m "feat(clone): union-find person clustering + never-issued synthetic SSNs"
```

---

### Task 10: Row writer

**Files:**
- Create: `clone/loader/writer.py`
- Test: `tests/clone/test_writer.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/clone/test_writer.py
import json

from clone.loader.writer import RecordBatch


def test_batch_emits_every_catalog_column_in_order():
    """COPY is positional: a column list that drifts from the catalog writes
    values into the wrong columns silently."""
    batch = RecordBatch()
    batch.add(record_id=1, table="records_legacy",
              source_file="Export Utility Stripped Down/x.csv", imported_at=None,
              columns={"first_name": "PAT", "zip": "40505"}, raw_data={})
    row = batch.rows("records_legacy")[0]
    assert len(row) == len(batch.columns)
    assert row[batch.columns.index("first_name")] == "PAT"
    assert row[batch.columns.index("last_name")] is None


def test_raw_data_is_serialised_as_json():
    batch = RecordBatch()
    batch.add(record_id=2, table="records_new", source_file="Payday_Big_2026/x.csv",
              imported_at="2026-02-15", columns={}, raw_data={"Email_02": "a@b.c"})
    row = batch.rows("records_new")[0]
    assert json.loads(row[batch.columns.index("raw_data")]) == {"Email_02": "a@b.c"}


def test_empty_raw_data_is_null_not_an_empty_object():
    """utility carries NO raw_data at all in production (0%)."""
    batch = RecordBatch()
    batch.add(record_id=3, table="records_legacy", source_file="Export Utility Stripped Down/x.csv",
              imported_at=None, columns={}, raw_data={})
    assert batch.rows("records_legacy")[0][batch.columns.index("raw_data")] is None
```

- [ ] **Step 2: Run it and watch it fail**

Run: `.venv/bin/python -m pytest tests/clone/test_writer.py -v`
Expected: FAIL — `No module named 'clone.loader.writer'`.

- [ ] **Step 3: Implement**

```python
# clone/loader/writer.py
"""Accumulate partner rows and COPY them in.

Column order comes from the recorded production catalog, not from a hand-kept
list here, because COPY is positional and a drifted list writes values into the
wrong columns without erroring.
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

import asyncpg

CATALOG = Path(__file__).parents[1] / "profiles" / "records_catalog.json"

# Populated by the loader, never by the CSV mapping.
_LOADER_OWNED = {"record_id", "source_file", "imported_at", "raw_data", "tsv_name"}


class RecordBatch:
    def __init__(self) -> None:
        self.columns: list[str] = [name for name, _ in json.loads(CATALOG.read_text())]
        self._rows: dict[str, list[list[Any]]] = defaultdict(list)

    def add(self, *, record_id: int, table: str, source_file: str,
            imported_at: str | None, columns: dict[str, Any],
            raw_data: dict[str, Any]) -> None:
        values: dict[str, Any] = dict(columns)
        values["record_id"] = record_id
        values["source_file"] = source_file
        values["imported_at"] = imported_at
        values["raw_data"] = json.dumps(raw_data) if raw_data else None
        self._rows[table].append([values.get(name) for name in self.columns])

    def rows(self, table: str) -> list[list[Any]]:
        return self._rows[table]

    async def flush(self, conn: asyncpg.Connection) -> int:
        total = 0
        for table, rows in self._rows.items():
            if not rows:
                continue
            await conn.copy_records_to_table(
                table, schema_name="public", columns=self.columns, records=rows)
            total += len(rows)
        self._rows.clear()
        return total
```

- [ ] **Step 4: Run the test**

Run: `.venv/bin/python -m pytest tests/clone/test_writer.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add clone/loader/writer.py tests/clone/test_writer.py
git commit -m "feat(clone): positional row writer driven by the recorded catalog"
```

---

### Task 11: The `load.py` entry point

**Files:**
- Create: `clone/load.py`

- [ ] **Step 1: Implement**

```python
# clone/load.py
"""Load the Lexington CSVs into a local partner clone.

    python clone/load.py --csv-dir /path/to/occupancy-engine/data/cleaned/lexington

The CSV directory is CONFIGURATION, never a hardcoded cross-repo path: it lives
in another repo and is DVC-tracked.

Shapes voter / criminal / linkedin / realtor are deliberately NOT loaded. The
first three are absent from all 114 production feeds; realtor is external
evidence the backend injects per-run, not partner data. Loading them would let
the local engine find evidence it can never find live.
"""
from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

import asyncpg

from clone.loader.csvsource import read_shape_csv
from clone.loader.feedplan import FEED_PLANS
from clone.loader.identity import DRIVE_JOIN_KEY, PersonIndex, synthetic_ssn
from clone.loader.mapping import partner_row_for
from clone.loader.population import keep_value, load_targets
from clone.loader.writer import RecordBatch

SHAPE_FIELDS = {
    "utility": ("first_name", "last_name", "address", "zip", "dob", "phone"),
    "trace": ("firstname", "lastname", "address", "zip", "", "phone"),
    "base": ("firstname", "lastname", "primaryaddress", "zip", "dob", "phone"),
    "loan": ("firstname", "lastname", "address", "zip", "", ""),
    "auto": ("firstname", "lastname", "address", "zip", "", "phone"),
    "tax": ("firstname", "lastname", "address", "zip", "", ""),
}
# Disjoint id ranges: record_id is unique only WITHIN a storage family
# (source/search.py::_PhysicalTable).
ID_BASE = {"records_legacy": 1_000_000_000, "records_new": 7_000_000_000}


def _identity_fields(shape: str, row: dict[str, str]) -> dict[str, str]:
    first, last, addr, zipc, dob, phone = SHAPE_FIELDS[shape]
    return dict(
        first=row.get(first, ""), last=row.get(last, ""),
        address=row.get(addr, ""), zip=row.get(zipc, ""),
        dob=row.get(dob, "") if dob else "", phone=row.get(phone, "") if phone else "",
    )


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv-dir", type=Path, required=True)
    parser.add_argument("--dsn", default=os.environ.get(
        "CLONE_DSN", "postgresql://clone:clone@127.0.0.1:55433/partner_clone"))
    parser.add_argument("--limit-per-shape", type=int, default=None,
                        help="Deterministic head-N per shape, for fast CI cycles.")
    args = parser.parse_args()

    targets = load_targets()
    index = PersonIndex()
    staged: list[tuple] = []          # (node, shape, plan, columns, raw_data, row)
    counters = dict(ID_BASE)

    # Pass 1 -- read every shape, map it, and register identities.
    for plan in FEED_PLANS:
        path = args.csv_dir / f"{plan.shape}.csv"
        if not path.exists():
            raise SystemExit(f"missing {path}")
        kept = 0
        for position, row in enumerate(read_shape_csv(path)):
            if args.limit_per_shape and kept >= args.limit_per_shape:
                break
            # base spans two feeds; split deterministically by position.
            if plan.shape == "base":
                to_legacy = position % 8 == 0
                if (plan.table == "records_legacy") != to_legacy:
                    continue
            columns, raw_data = partner_row_for(plan.shape, row)
            node = index.add(**_identity_fields(plan.shape, row))
            staged.append((node, plan, columns, raw_data, row))
            kept += 1
        print(f"  staged {kept:,} rows for {plan.shape} -> {plan.table}", flush=True)

    # Pass 2 -- fold drive licences onto their person's payday rows.
    licences: dict[tuple, tuple[str, str]] = {}
    for row in read_shape_csv(args.csv_dir / "drive.csv"):
        licences.setdefault(DRIVE_JOIN_KEY(row), (row.get("dl_num", ""), row.get("dl_state", "")))

    persons = index.person_ids()
    batch = RecordBatch()
    oracle: list[tuple[int, str, int, str]] = []
    for node, plan, columns, raw_data, row in staged:
        person_id = persons[node]
        record_id = counters[plan.table]
        counters[plan.table] += 1
        row_key = f"{plan.shape}:{record_id}"

        if plan.shape == "loan":
            licence = licences.get(DRIVE_JOIN_KEY(row))
            if licence:
                columns["dl_number"], columns["dl_state"] = licence
        if keep_value(plan.shape, "ssn", row_key, targets):
            columns["ssn"] = synthetic_ssn(person_id)
        for optional in ("dob", "phone", "email", "house_number"):
            if optional in columns and not keep_value(plan.shape, optional, row_key, targets):
                columns.pop(optional)

        batch.add(record_id=record_id, table=plan.table, source_file=plan.source_file,
                  imported_at=plan.imported_at, columns=columns, raw_data=raw_data)
        oracle.append((person_id, plan.table, record_id, plan.shape))

    conn = await asyncpg.connect(args.dsn)
    try:
        written = await batch.flush(conn)
        await conn.copy_records_to_table(
            "true_person_record", schema_name="bench",
            columns=["person_id", "source_table", "record_id", "shape"], records=oracle)
        await conn.execute("""
            INSERT INTO bench.true_person (person_id, synthetic_ssn, record_count)
            SELECT person_id, lpad((900 + person_id % 100)::text, 3, '0')
                   || lpad((person_id / 100 % 100)::text, 2, '0')
                   || lpad((person_id / 10000 % 10000)::text, 4, '0'),
                   count(*)
            FROM bench.true_person_record GROUP BY person_id""")
        await conn.execute("ANALYZE public.records_legacy")
        await conn.execute("ANALYZE public.records_new")
    finally:
        await conn.close()
    print(f"loaded {written:,} records, {len(set(p for p, *_ in oracle)):,} oracle persons")


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 2: Run it against the clone**

```bash
docker compose -f clone/docker-compose.clone.yml up -d --wait
for f in ddl/*.sql; do docker compose -f clone/docker-compose.clone.yml exec -T \
  graph-clone-db psql -U clone -d partner_clone -v ON_ERROR_STOP=1 < "$f"; done
.venv/bin/python clone/load.py --csv-dir /home/aayan-alam/Work/Helcion/occupancy-engine/data/cleaned/lexington
```

Expected: per-shape staged counts, then `loaded 2,3xx,xxx records, ~7xx,xxx oracle persons`.

- [ ] **Step 3: Commit**

```bash
git add clone/load.py
git commit -m "feat(clone): CSV -> partner-corpus loader with oracle emission"
```

---

## Phase 3 — Entity graph and verification

### Task 12: Build `entity_master` / `entity_links` by ssn blocking

**Files:**
- Create: `clone/loader/entity.py`
- Modify: `clone/load.py` (call it after the record flush)
- Test: `tests/clone/test_entity.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/clone/test_entity.py
"""The graph is built by REPRODUCING production's mechanism, not its statistics.

Production blocks on ssn (100% of sampled links, avg confidence 0.99), and ssn
exists almost only in the payday feeds. That one fact CAUSES both the
97.5:2.5 records_new:records_legacy skew and tax's total absence from
entity_links -- so we reproduce the cause and let the profile emerge.
"""
from clone.loader.entity import build_entities, stamped_confidence, stamped_suspicious


def test_only_ssn_bearing_rows_are_linked():
    rows = [
        {"record_id": 1, "source_table": "records_new", "ssn": "900010000", "shape": "loan"},
        {"record_id": 2, "source_table": "records_new", "ssn": None, "shape": "tax"},
        {"record_id": 3, "source_table": "records_legacy", "ssn": None, "shape": "utility"},
    ]
    masters, links = build_entities(rows)
    assert [l["record_id"] for l in links] == [1]
    assert len(masters) == 1


def test_tax_rows_are_never_linked():
    """property_owner has ssn, dob and house_number all 0% -- no blocking key --
    so production's entity_links contains none of it."""
    rows = [{"record_id": 9, "source_table": "records_new", "ssn": None, "shape": "tax"}]
    _, links = build_entities(rows)
    assert links == []


def test_shared_ssn_collapses_to_one_entity():
    rows = [
        {"record_id": 1, "source_table": "records_new", "ssn": "900010000", "shape": "loan"},
        {"record_id": 2, "source_table": "records_new", "ssn": "900010000", "shape": "loan"},
    ]
    masters, links = build_entities(rows)
    assert len(masters) == 1
    assert masters[0]["record_count"] == 2
    assert len(links) == 2


def test_record_count_is_emergent_never_stamped():
    """Stamping record_count to production's 2.65 mean would make entity_master
    contradict entity_links, and search_people ORDERS BY record_count."""
    rows = [{"record_id": i, "source_table": "records_new", "ssn": "900010000",
             "shape": "loan"} for i in range(7)]
    masters, links = build_entities(rows)
    assert masters[0]["record_count"] == len(links) == 7


def test_links_carry_productions_match_type_and_confidence():
    rows = [{"record_id": 1, "source_table": "records_new", "ssn": "900010000", "shape": "loan"}]
    _, links = build_entities(rows)
    assert links[0]["match_type"] == "ssn"
    assert 0.95 <= float(links[0]["confidence"]) <= 1.0


def test_stamped_metadata_matches_the_measured_production_distribution():
    confidences = [stamped_confidence(i) for i in range(20_000)]
    assert 39.0 <= sum(confidences) / len(confidences) <= 44.0     # production mean 41.52
    assert sum(1 for c in confidences if abs(c - 40.50) < 0.01) > 2_000   # modal 40.50
    suspicious = sum(stamped_suspicious(i) for i in range(20_000))
    assert 0.28 <= suspicious / 20_000 <= 0.35                     # production 31.4%
```

- [ ] **Step 2: Run it and watch it fail**

Run: `.venv/bin/python -m pytest tests/clone/test_entity.py -v`
Expected: FAIL — `No module named 'clone.loader.entity'`.

- [ ] **Step 3: Implement**

```python
# clone/loader/entity.py
"""entity_master / entity_links, built the way production builds them.

Two classes of field, and conflating them is a correctness bug:

  STAMPED   identity_confidence, is_suspicious, is_merged -- drawn to match
            production's measured distribution, because heuristics DISCOUNT by
            them and constant-0.99 confidence never exercises that logic.
  EMERGENT  record_count -- the true number of entity_links rows. Stamping it to
            production's 2.65 mean would make entity_master contradict its own
            links, and search_people orders by it.

Expect the emergent profile to differ from production (our payday persons average
~4.2 rows each, so record_count runs higher and singletons lower). That is a
consequence of the accepted anchor thinness, not a defect -- do not "fix" it.
"""
from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from typing import Any

MATCH_TYPE = "ssn"
LINK_CONFIDENCE = 0.99          # production sampled avg 0.99
_MODAL_CONFIDENCE = 40.50       # production modal value; 27.5% of rows sit here


def _hash(seed: str, salt: str) -> int:
    return int.from_bytes(
        hashlib.blake2b(f"{salt}|{seed}".encode(), digest_size=8).digest(), "big")


def stamped_confidence(person_id: int) -> float:
    """Modal at 40.50 with the rest spread across the 34-70 band."""
    bucket = _hash(str(person_id), "conf") % 1000
    if bucket < 275:                       # 27.5% sit exactly on the mode
        return _MODAL_CONFIDENCE
    return round(34.0 + (bucket % 360) / 10.0, 2)


def stamped_suspicious(person_id: int) -> bool:
    return _hash(str(person_id), "susp") % 1000 < 314      # 31.4%


def stamped_merged(person_id: int) -> bool:
    """Production computes merges and NEVER applies them: both sides stay
    resident, which is what exercises search_people's is_merged IS NOT TRUE."""
    return _hash(str(person_id), "merge") % 1000 < 20      # 2%


def build_entities(rows: Iterable[Mapping[str, Any]]
                   ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Block on ssn exactly as production does.

    Rows without an ssn are simply never linked -- which is why tax (0% ssn) is
    absent and records_legacy is near-absent, with no rule needed to say so.
    """
    by_ssn: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        ssn = row.get("ssn")
        if ssn:
            by_ssn.setdefault(ssn, []).append(row)

    masters: list[dict[str, Any]] = []
    links: list[dict[str, Any]] = []
    for ordinal, (ssn, members) in enumerate(sorted(by_ssn.items())):
        hal_id = f"HAL{ordinal:012d}"
        for member in members:
            links.append({
                "hal_id": hal_id, "source_table": member["source_table"],
                "record_id": member["record_id"], "match_type": MATCH_TYPE,
                "confidence": LINK_CONFIDENCE,
            })
        best = max(members, key=lambda m: sum(1 for v in m.values() if v))
        masters.append({
            "hal_id": hal_id, "canonical_ssn": ssn,
            "canonical_first_name": best.get("first_name"),
            "canonical_last_name": best.get("last_name"),
            "canonical_address_line1": best.get("address"),
            "canonical_city": best.get("city"), "canonical_state": best.get("state"),
            "canonical_zip": best.get("zip"),
            "canonical_source_table": best["source_table"],
            "canonical_record_id": best["record_id"],
            "record_count": len(members),                    # EMERGENT
            "identity_confidence": stamped_confidence(ordinal),
            "is_suspicious": stamped_suspicious(ordinal),
            "is_merged": stamped_merged(ordinal),
        })
    return masters, links
```

- [ ] **Step 4: Run the test**

Run: `.venv/bin/python -m pytest tests/clone/test_entity.py -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Wire it into `load.py`**

After `written = await batch.flush(conn)` and before the `ANALYZE` calls, add:

```python
        entity_rows = await conn.fetch("""
            SELECT record_id, 'records_legacy' AS source_table, ssn, first_name,
                   last_name, address, city, state, zip FROM public.records_legacy
            UNION ALL
            SELECT record_id, 'records_new', ssn, first_name, last_name,
                   address, city, state, zip FROM public.records_new""")
        masters, links = build_entities([dict(r) for r in entity_rows])
        await conn.copy_records_to_table(
            "entity_master", schema_name="silver",
            columns=list(masters[0]), records=[list(m.values()) for m in masters])
        await conn.copy_records_to_table(
            "entity_links", schema_name="silver",
            columns=list(links[0]), records=[list(l.values()) for l in links])
        print(f"entity graph: {len(masters):,} entities, {len(links):,} links")
```

and import it: `from clone.loader.entity import build_entities`.

- [ ] **Step 6: Reload and check the emergent profile**

```bash
docker compose -f clone/docker-compose.clone.yml down -v
docker compose -f clone/docker-compose.clone.yml up -d --wait
for f in ddl/*.sql; do docker compose -f clone/docker-compose.clone.yml exec -T \
  graph-clone-db psql -U clone -d partner_clone -v ON_ERROR_STOP=1 < "$f"; done
.venv/bin/python clone/load.py --csv-dir /home/aayan-alam/Work/Helcion/occupancy-engine/data/cleaned/lexington
```

Expected: an `entity graph: … entities, … links` line. Links should be overwhelmingly `records_new`.

- [ ] **Step 7: Commit**

```bash
git add clone/loader/entity.py clone/load.py tests/clone/test_entity.py
git commit -m "feat(clone): ssn-blocked entity graph reproducing production's mechanism"
```

---

### Task 13: Clone profile verification suite

**Files:**
- Create: `tests/clone/test_clone_profile.py`

- [ ] **Step 1: Write the tests**

These run only against a loaded clone, so they skip without `CLONE_DSN` — exactly like the existing live-smoke tests skip without `PARTNER_DSN`.

```python
# tests/clone/test_clone_profile.py
"""Assertions that the loaded clone is faithful. Skipped unless CLONE_DSN is set.

Test 2 is the round trip that would have caught `records_partitioned`: it proves
the service can find, and correctly classify, the rows the loader wrote.
"""
from __future__ import annotations

import os

import asyncpg
import pytest
import pytest_asyncio

from occupancy_graph.source.feeds import FEEDS, feed_clause
from occupancy_graph.source.manifest import SHAPES

CLONE_DSN = os.environ.get("CLONE_DSN")
pytestmark = pytest.mark.skipif(not CLONE_DSN, reason="CLONE_DSN is not set")

ZIP_SHAPES = ("utility", "trace", "base", "loan", "drive", "auto")


@pytest_asyncio.fixture
async def clone():
    conn = await asyncpg.connect(CLONE_DSN)
    try:
        yield conn
    finally:
        await conn.close()


@pytest.mark.parametrize("shape", [*ZIP_SHAPES, "tax"])
async def test_every_shape_is_reachable_through_its_own_feed_patterns(clone, shape):
    clause, params = feed_clause(shape, start_index=1)
    found = 0
    for table in FEEDS[shape].tables:
        found += await clone.fetchval(
            f"SELECT count(*) FROM public.{table} WHERE {clause}", *params)
    assert found > 0, f"{shape}: feeds.py patterns select nothing from the clone"


async def test_omitted_shapes_are_absent(clone):
    """voter/criminal/linkedin do not exist in production; loading them would let
    the local engine find evidence it can never find live."""
    for feed in ("voter%", "criminal%", "linkedin%", "realtor%"):
        for table in ("records_legacy", "records_new"):
            assert await clone.fetchval(
                f"SELECT count(*) FROM public.{table} WHERE source_file LIKE $1", feed) == 0


async def test_house_number_is_populated_only_where_production_populates_it(clone):
    """The rule that keeps the coverage experiment valid: our CSVs carry
    housenumber at ~100% on trace/auto/tax, where production has it NULL."""
    for feed, expect_any in (("Export Utility%", False), ("Trace Skipping%", False),
                             ("property_owner%", False), ("2026.1-USCRM%", True)):
        got = await clone.fetchval("""
            SELECT count(*) FROM public.records_legacy
            WHERE source_file LIKE $1 AND house_number IS NOT NULL""", feed) or 0
        got += await clone.fetchval("""
            SELECT count(*) FROM public.records_new
            WHERE source_file LIKE $1 AND house_number IS NOT NULL""", feed) or 0
        assert (got > 0) is expect_any, f"{feed}: house_number population is wrong"


async def test_entity_links_use_only_the_ssn_blocking_key(clone):
    rows = await clone.fetch("SELECT DISTINCT match_type FROM silver.entity_links")
    assert {r["match_type"] for r in rows} == {"ssn"}


async def test_tax_rows_have_no_entity_links(clone):
    orphaned = await clone.fetchval("""
        SELECT count(*) FROM silver.entity_links l
        JOIN public.records_new r ON r.record_id = l.record_id
        WHERE r.source_file LIKE 'property_owner%'""")
    assert orphaned == 0


async def test_links_skew_to_records_new_like_production(clone):
    new = await clone.fetchval(
        "SELECT count(*) FROM silver.entity_links WHERE source_table='records_new'")
    legacy = await clone.fetchval(
        "SELECT count(*) FROM silver.entity_links WHERE source_table='records_legacy'")
    assert new > 0
    assert legacy / max(new + legacy, 1) < 0.05     # production 2.5%; ours ~0 (spec §8.3)


async def test_record_count_equals_the_actual_link_count(clone):
    """EMERGENT, never stamped -- search_people orders by record_count."""
    mismatched = await clone.fetchval("""
        SELECT count(*) FROM silver.entity_master m
        WHERE m.record_count <> (
          SELECT count(*) FROM silver.entity_links l WHERE l.hal_id = m.hal_id)""")
    assert mismatched == 0


async def test_the_oracle_covers_every_loaded_record(clone):
    for table in ("records_legacy", "records_new"):
        records = await clone.fetchval(f"SELECT count(*) FROM public.{table}")
        oracled = await clone.fetchval(
            "SELECT count(*) FROM bench.true_person_record WHERE source_table=$1", table)
        assert records == oracled, f"{table}: oracle does not cover every record"
```

- [ ] **Step 2: Run them against the loaded clone**

Run:
```bash
CLONE_DSN=postgresql://clone:clone@127.0.0.1:55433/partner_clone \
  .venv/bin/python -m pytest tests/clone/test_clone_profile.py -v
```
Expected: PASS. `test_every_shape_is_reachable_through_its_own_feed_patterns` is parametrized over 7 shapes; `drive` passes only if the licence fold-in worked.

- [ ] **Step 3: Confirm they skip cleanly without the clone**

Run: `.venv/bin/python -m pytest tests/clone/test_clone_profile.py -q`
Expected: all skipped, no errors — CI without a clone stays green.

- [ ] **Step 4: Commit**

```bash
git add tests/clone/test_clone_profile.py
git commit -m "test(clone): fidelity suite -- feed round trip, house_number rule, ER mechanism, oracle coverage"
```

---

### Task 14: End-to-end against the recorded live baseline

**Files:**
- Create: `clone/compare_to_live.py`

- [ ] **Step 1: Implement the comparison**

```python
# clone/compare_to_live.py
"""Resolve the 12 mini.csv addresses against the clone and diff per-shape counts
against the recorded LIVE run.

Differences are EXPECTED (spec §8) -- anchor thinness, dl_number at 27%, near-zero
legacy links. The bar is that every difference is explainable, not that there are
none. Prints a table for the record; it deliberately does not assert.

    CLONE_DSN=... python clone/compare_to_live.py
"""
from __future__ import annotations

import asyncio
import os

import httpx

from occupancy_graph.service.app import create_app
from occupancy_graph.source.bundle import BundleCache
from occupancy_graph.source.pool import PartnerPool

# From docs/superpowers/specs/2026-08-03-mini-csv-partner-benchmark-results.md
LIVE = {
    "1104 SPRING RUN RD": ("40514", {"utility": 1, "trace": 3, "base": 1, "loan": 0, "drive": 0, "auto": 2, "tax": 1}),
    "1552 SAMARA GLEN WAY": ("40515", {"utility": 3, "trace": 20, "base": 3, "loan": 10, "drive": 3, "auto": 0, "tax": 1}),
    "548 RHODORA RDG": ("40517", {"utility": 6, "trace": 4, "base": 9, "loan": 7, "drive": 7, "auto": 0, "tax": 1}),
    "2812 RED LEAF DR": ("40509", {"utility": 0, "trace": 8, "base": 4, "loan": 0, "drive": 0, "auto": 2, "tax": 2}),
    "849 W MAXWELL ST": ("40508", {"utility": 6, "trace": 18, "base": 3, "loan": 3, "drive": 3, "auto": 3, "tax": 6}),
    "535 LONE OAK DR": ("40503", {"utility": 4, "trace": 2, "base": 2, "loan": 0, "drive": 0, "auto": 0, "tax": 1}),
    "1000 TURNBERRY LN": ("40515", {"utility": 0, "trace": 10, "base": 2, "loan": 12, "drive": 12, "auto": 0, "tax": 1}),
    "1004 SPRING RUN RD": ("40514", {"utility": 16, "trace": 22, "base": 2, "loan": 0, "drive": 0, "auto": 3, "tax": 1}),
    "1057 SPRING RUN RD": ("40514", {"utility": 8, "trace": 7, "base": 2, "loan": 0, "drive": 0, "auto": 1, "tax": 1}),
    "1101 WELDON CT": ("40515", {"utility": 2, "trace": 11, "base": 3, "loan": 1, "drive": 1, "auto": 0, "tax": 1}),
    "115 WABASH DR": ("40503", {"utility": 5, "trace": 13, "base": 2, "loan": 22, "drive": 4, "auto": 0, "tax": 1}),
    "1332 OX HILL DR": ("40517", {"utility": 9, "trace": 0, "base": 1, "loan": 0, "drive": 0, "auto": 0, "tax": 1}),
}
SHAPES = ("utility", "trace", "base", "loan", "drive", "auto", "tax")


async def main() -> None:
    os.environ["PARTNER_DSN"] = os.environ["CLONE_DSN"]
    pool = await PartnerPool.from_env()
    app = create_app(pool=pool, cache=BundleCache(pool))
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://clone", timeout=600.0) as http:
            header = "address".ljust(22) + "".join(s[:5].rjust(12) for s in SHAPES)
            print(header); print("-" * len(header))
            for address, (zip_code, live) in LIVE.items():
                response = await http.post("/v1/resolve",
                                           json={"address": address, "zip": zip_code})
                got = response.json().get("source_counts", {}) if response.status_code == 200 else {}
                cells = "".join(f"{got.get(s, 0):>5}/{live[s]:<6}" for s in SHAPES)
                print(f"{address:<22}{cells}")
            print("\nformat: clone/live. Differences are expected -- see spec §8.")
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 2: Run it**

Run:
```bash
CLONE_DSN=postgresql://clone:clone@127.0.0.1:55433/partner_clone \
  .venv/bin/python clone/compare_to_live.py
```
Expected: a 12-row table. Every address should resolve (non-zero counts); tax should be present on most, since `property_owner` is the one feed we load at full fidelity.

- [ ] **Step 3: Commit**

```bash
git add clone/compare_to_live.py
git commit -m "feat(clone): diff clone resolves against the recorded live baseline"
```

---

### Task 15: The coverage experiment — the measurement production cannot give us

**Files:**
- Create: `clone/coverage_experiment.py`

- [ ] **Step 1: Implement**

```python
# clone/coverage_experiment.py
"""Quantify what the resident hop misses. GOAL 2 -- and the reason the clone
exists at all: against production this is unmeasurable, because the full ZIP scan
never finishes (observed server-side ACTIVE at 14+ minutes).

Locally BOTH paths run, so we can diff them per address and put a real number on
the hop's coverage loss.

    CLONE_DSN=... python clone/coverage_experiment.py
"""
from __future__ import annotations

import asyncio
import os

from occupancy_graph.source.feeds import pattern_groups
from occupancy_graph.source.pool import PartnerPool
from occupancy_graph.source.resolve import (
    ZIP_SHAPES, AddressQuery, _scan_legacy_via_residents, _scan_table,
)

ADDRESSES = [
    ("1104 SPRING RUN RD", "40514"), ("1552 SAMARA GLEN WAY", "40515"),
    ("548 RHODORA RDG", "40517"), ("2812 RED LEAF DR", "40509"),
    ("849 W MAXWELL ST", "40508"), ("535 LONE OAK DR", "40503"),
    ("1000 TURNBERRY LN", "40515"), ("1004 SPRING RUN RD", "40514"),
    ("1057 SPRING RUN RD", "40514"), ("1101 WELDON CT", "40515"),
    ("115 WABASH DR", "40503"), ("1332 OX HILL DR", "40517"),
]


async def main() -> None:
    os.environ["PARTNER_DSN"] = os.environ["CLONE_DSN"]
    pool = await PartnerPool.from_env()
    try:
        print(f"{'address':<22}{'full scan':>11}{'hop':>7}{'missed':>9}{'recall':>9}")
        print("-" * 58)
        totals = [0, 0]
        for address, zip_code in ADDRESSES:
            query = AddressQuery.build(address, zip_code)
            groups = pattern_groups(ZIP_SHAPES, "records_legacy")
            full = {r["record_id"] for r in await _scan_table(pool, "records_legacy", groups, query)}
            hop = {r["record_id"] for r in await _scan_legacy_via_residents(pool, query)}
            missed = full - hop
            recall = len(hop & full) / len(full) if full else 1.0
            totals[0] += len(full); totals[1] += len(hop & full)
            print(f"{address:<22}{len(full):>11}{len(hop):>7}{len(missed):>9}{recall:>8.1%}")
        overall = totals[1] / totals[0] if totals[0] else 1.0
        print("-" * 58)
        print(f"{'OVERALL':<22}{totals[0]:>11}{totals[1]:>7}{totals[0]-totals[1]:>9}{overall:>8.1%}")
        print("\nLOWER BOUND on production recall: we hold only 1 of production's 4")
        print("house_number-bearing anchor feeds (spec §8.1), so production's hop")
        print("has MORE anchors to work from than this measures.")
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 2: Run it**

Run:
```bash
CLONE_DSN=postgresql://clone:clone@127.0.0.1:55433/partner_clone \
  .venv/bin/python clone/coverage_experiment.py
```
Expected: a per-address recall table and an overall figure — the number that has been unobtainable until now.

- [ ] **Step 3: Commit**

```bash
git add clone/coverage_experiment.py
git commit -m "feat(clone): measure resident-hop coverage loss, unmeasurable against production"
```

---

### Task 16: Wire-up docs

**Files:**
- Modify: `clone/README.md`
- Modify: `docs/harness/progress.md` (workspace repo)

- [ ] **Step 1: Add a results section to `clone/README.md`**

Append the actual numbers Tasks 13-15 produced (loaded row counts, entity/link counts, the coverage recall figure, the clone-vs-live table). Record what you measured, not what you expected.

- [ ] **Step 2: Update the workspace progress journal**

Add an entry under "Active work" in `../../../../docs/harness/progress.md` naming the clone, its port, how to load it, the coverage number, and the standing limits (no performance transfer, no ER-quality testing).

- [ ] **Step 3: Commit both repos separately**

```bash
git add clone/README.md && git commit -m "docs(clone): record measured load, entity and coverage figures"
cd ../../../.. && git add docs/harness/progress.md \
  && git commit -m "docs(harness): local partner clone landed; coverage experiment has a number"
```

---

## Self-Review

**Spec coverage** — §1 source data → Tasks 5, 11 (omissions enforced by test in 13); §2 schema →
Tasks 1-3; §3 housing → Task 4; §4 ETL mapping → Tasks 6, 7, 10; §5 drive → Task 9 (`DRIVE_JOIN_KEY`)
+ Task 11 fold-in; §6 entity graph → Tasks 9, 12; §7 population fidelity → Task 8, asserted in 13;
§8 limitations → documented in Task 16, asserted as expectations in 13; §9 verification → Tasks 1, 2,
13, 14, 15 (all six points); §10 defaults → Task 11 `--limit-per-shape`, Task 7 base split, Task 2
`tsv_name` GIN index (population deferred — see gap below).

**Known gap, deliberate:** §10 defaults `tsv_name` to being *populated* on load; this plan creates the
GIN index (Task 2) but does not populate the column. Nothing in `src/` reads it. If the SQL hatch
starts naming it, add a single `UPDATE … SET tsv_name = to_tsvector('simple', …)` after Task 11's
load. Called out rather than silently dropped.

**Placeholder scan** — no TBD/TODO; every code step carries complete code; every command carries its
expected output.

**Type consistency** — `partner_row_for` returns `(columns, raw_data)` and is consumed that way in
Task 11. `PersonIndex.add()` takes the same keyword set `_identity_fields` produces. `RecordBatch.add`
signature matches its call site. `build_entities(rows) -> (masters, links)` matches Task 12 Step 5.
`keep_value(shape, column, row_key, targets)` matches both its test and `load.py`.
