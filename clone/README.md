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

## Measured results (2026-08-04 load)

Every number below was produced by the scripts in this directory, not estimated.

### Load

```
2,355,742 records in 427s, byte-identical across repeated runs (deterministic)
  records_legacy 1,969,547   records_new 386,195
  utility 1,506,562 · trace 439,820 · base 185,319 · loan 81,302 · auto 64,296 · tax 78,443
oracle          705,842 persons / 2,355,742 true_person_record rows (exact coverage)
drive fold-in   21,927 / 81,302 loan rows carry a licence (27.0%)
entity graph    21,163 entities · 77,893 links · records_new 77,893 : records_legacy 0
anchors         22,702 rows carry house_number (base/USCRM only, as production does)
```

The entity graph's `records_new`-only skew is **emergent, not imposed**: ssn is populated only on the
payday feeds, exactly as production does it, so ssn-blocking naturally links almost nothing else and
leaves tax entirely unlinked. That is production's own mechanism, reproduced rather than imitated.

Fidelity suite: **19/19** (`CLONE_DSN=... pytest tests/clone/test_clone_profile.py`).

### It serves the real service

`POST /v1/resolve` for 1104 SPRING RUN RD returns **HTTP 200 in 0.2 s** with the AURORA, IL
absentee-owner signal intact — against **62 s cold** on the live corpus. That speed is the clearest
possible restatement of the warning above: **latency does not transfer.**

### Resident-hop coverage — the measurement production cannot give us

`python -m clone.coverage_experiment`. Against the real corpus the full ZIP scan never finishes
(observed server-side ACTIVE at 14+ minutes), so this loss has never been measurable. Locally both
paths run.

| group | addresses | recall |
|---|---|---|
| **≥1 surviving anchor** — the hop's true recall | 11 / 12 | **39.1%** (100/256) |
| **0 surviving anchors** — the anchor-coverage problem | 1 / 12 | 0.0% |
| blended | 12 | 36.4% |

Per-address recall ranges from **4.5%** (1332 OX HILL DR) to **84.2%** (2812 RED
LEAF DR). That spread *is* the finding: recall depends on whether the anchor's
resident happens to be the same person the utility/trace rows name, which varies
address by address. A single average would hide it.

An earlier load reported 31.6% across only 2 addresses, because the base split
was weighted 1:7 toward `records_new` to mirror production's feed VOLUME — which
left just 2 usable anchors across all 12 addresses. base is our only
anchor-bearing feed and the hop scans `records_legacy` only, so that ratio
starved the very thing being measured. See the note on the base FeedPlans in
`loader/feedplan.py`.

**Read the split, never the blend.** The blended 4.7% describes anchor availability, not the hop.
Where the hop has any resident to work from it recovers ~32% of what a full scan finds; where it has
none it cannot run at all. Those are different failures and averaging them hides both.

**This is a LOWER BOUND on production.** We hold 1 of production's 4 house_number-bearing anchor
feeds (USCRM; production also has SSNxDOB, the 2014 phonebook and Historic Data), so production's hop
has strictly more anchors. The 10 zero-anchor addresses are precisely where those feeds would matter.

### Clone vs. live counts

`python -m clone.compare_to_live` diffs all 12 mini.csv addresses against the recorded live run.
Totals `clone/live`: utility 9/60 · trace 1/118 · base 24/34 · loan 11/55 · drive 0/30 · auto 4/11 ·
tax 17/18.

Two **independent** causes, and both matter:

1. **Anchor thinness** (above) — dominates the legacy-only shapes, utility and trace.
2. **The Lexington CSVs are a statistically-calibrated sample, not a row-for-row mirror** of the live
   corpus. Verified directly: 1552 SAMARA GLEN WAY genuinely holds 4 payday rows in the clone against
   10 live. This is why `records_new` shapes — which bypass the resident hop entirely — still
   diverge, and it is why `tax` is the one column where the clone sometimes *exceeds* live.

Do not attribute every difference to the hop.

### Why retrieval is not identical to production — decomposed

`clone_shim / clone_content` is a **mechanism** number (what the shim recovers of
what is actually there). `clone_content vs live_shim` is a **data** number. Only
the first is something the loader or the shim can fix.

| shape | clone content | clone shim | retrieval | live shim | content vs live |
|---|---|---|---|---|---|
| loan | 11 | 11 | **100%** | 55 | 20% |
| auto | 4 | 4 | **100%** | 11 | 36% |
| tax | 17 | 17 | **100%** | 18 | 94% |
| base | 22 | 24 | 109%¹ | 34 | 65% |
| trace | 64 | 57 | 89% | 118 | 54% |
| utility | 189 | 29 | **15%** | 60 | 315% |
| drive | (= loan) | 0 | n/a | 30 | **0%** |

¹ >100% because base spans both roots and the shim counts both.

**Retrieval is exact wherever the hop is not involved.** Every `records_new`
shape recovers 100% of what the clone holds, so their gap against live is purely
that the Lexington CSVs are a different extraction — not a retrieval defect.

**`utility` at 15% is the anchor-diversity ceiling, and it is structural.** At
1057 SPRING RUN RD the clone holds 33 utility rows across **20 distinct
surnames** but only **1 anchor surname**. The hop can only reach residents it can
name, and with one anchor feed where production has four, it names 1-2 people
where 6-20 live there. `MAX_NAME_HOPS` (8) is nowhere near binding. No loader
change fixes this; only the missing anchor feeds would.

**`drive` at 0 is content absence, proven not inferred:** `drive.csv` contains
**zero rows** at any of the 12 benchmark addresses, so no join key can recover
licences that do not exist in the source.

**What WAS fixable has been fixed** — two real bugs found by this comparison:
the base split starving anchors (`loader/feedplan.py`), and the name hop applying
its address filter after the LIMIT rather than inside the query
(`src/occupancy_graph/source/resolve.py`), which cost trace 11 points of recall
and would silently lose rows in production too.

**Exact parity is not reachable from these CSVs.** It needs an extract taken
*from* the partner corpus — option §4.4 in `2026-08-04-partner-ask.md`.
