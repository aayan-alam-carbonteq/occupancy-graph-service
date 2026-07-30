# Verification — occupancy-graph-service

What this service's gates are, what the adversarial pass actually found, and — the part that
matters most to someone who was not here — **which of its claims are backed by measurement and
which are not**. Read the "Known limitations" section before trusting any number below it.

## Gates

    cd occupancy-engine-ts/services/graph
    .venv/bin/python -m pytest -q

Expected: **`548 passed, 13 skipped`**. The 13 skips are the `-m live` smoke tests, which need
`PARTNER_DSN`. The Postgres fixture starts and stops itself (`tests/conftest.py` +
`tests/docker-compose.fixture.yml`, port 55432). Docker must be running. Containers named
`mortgage-compliance-monitoring-graph-*` belong to another repo — leave them alone.

Use `.venv/bin/python` for everything. The venv is uv-managed and has **no `pip` binary**:

    VIRTUAL_ENV=.venv uv pip install --python .venv/bin/python <pkg>

Tests are plain module-level `def` / `async def` functions. Never write an `async def` method on a
`unittest.TestCase`: `asyncio_mode="auto"` does not wrap them, so they pass without running.

`pytest-timeout` caps every phase (setup, call, teardown) of every test at 120 s. The slowest real
phase is ~6.3 s (the session-scoped fixture DB coming up), so the ceiling exists to turn a wedged
run into a located failure rather than a silent hang.

### The plan's test-count ledger is superseded

`docs/superpowers/plans/2026-07-29-typed-data-service.md` carries a per-task ledger ending at
**`313 passed, 4 skipped`**, and its Definition of Done repeats that number. **It is obsolete.**
Review passes added substantial coverage at nearly every task, and the real figure is
**`548 passed, 13 skipped`** — 548 executed, up from a 181-test baseline, with 13 live tests
collected and skipped instead of the 4 the plan sketched.

This is recorded rather than reconciled. The plan's ledger has not been rewritten to match, because
the ledger is a record of what the plan predicted, and editing it would erase the fact that review
roughly doubled the suite. Where a checklist item below quotes 313, it is marked obsolete rather
than forced.

## The SQL hatch write guard — what exists, not what was planned

The plan describes a hand-written lexer: `strip_literals` plus a statement-keyword blocklist. **That
design was replaced.** `src/occupancy_graph/service/sql_guard.py` now parses with **`pglast`**, which
wraps **libpg_query — PostgreSQL's own parser sources**, not a reimplementation of them. Read that
module's docstring; it is the primary reference and it explains the replacement in the server's
terms.

The rules are now structural facts about the parse tree:

- exactly **one** statement;
- the top node is a **`SelectStmt`**;
- **no `*Stmt` node of any other kind anywhere in the tree** — an allowlist, not a DML denylist,
  which is what kills `INSERT`-inside-a-CTE at arbitrary depth without modelling CTEs at all;
- **`intoClause is None`** on every `SelectStmt` (`SELECT … INTO` creates a table);
- **no `LockingClause`** (`FOR UPDATE` / `FOR NO KEY UPDATE` / `FOR SHARE` / `FOR KEY SHARE`);
- every `FuncCall` name read **from the AST** and checked against a **family-prefix** denylist
  (`pg_read_*`, `pg_ls_*`, `lo_*`, `dblink*`, `pg_sleep*`, `pg_advisory*`, `txid_*`, …) plus a
  short list of exact names (`nextval`, `setval`, `pg_notify`, `set_config`, …).

**Version skew is deliberate and fails safe in both directions.** pglast 8.4 exposes PostgreSQL
18.4's grammar; the fixture and the partner corpus run 17. Syntax 18 accepts and 17 rejects is
passed through and dies as a server-side syntax error. Syntax 18 rejects becomes a `ParseError`
here, which is a refusal — a false positive, never a bypass.

**Stage 1 is the primary write control, not defence in depth.**
`default_transaction_read_only` is a session default that raw `BEGIN READ WRITE` defeats; that is
pinned by `tests/test_pool.py::test_read_only_is_a_session_default_not_a_boundary`, where the
INSERT commits.

    .venv/bin/python -m pytest tests/test_sql_guard.py tests/test_sql_hatch.py -q

Expected: **`211 passed`** (the plan predicted 41).

## The adversarial pass

This was run, in two rounds. It is recorded here rather than re-run, because round 1 was run against
code that no longer exists.

### Round 1 — against the hand-written lexer: six bypasses, two Critical

Both Criticals were the same failure mode: code that looked careful but **disagreed with
PostgreSQL's real lexer**.

1. **Critical — a carriage return terminates a `--` comment in Postgres.** `scan.l` defines
   `non_newline` as `[^\n\r]`. The hand lexer scanned only for `\n`, so
   `SELECT 1 --x\r; DROP TABLE t` was **one** statement to the guard and **two** to the server.
   Pinned now by `test_a_carriage_return_ends_a_line_comment_just_like_a_newline`, with
   `test_a_carriage_return_really_does_end_a_comment_in_postgres` proving the server's behaviour
   against the fixture rather than asserting it from the docs.
2. **Critical — quoting a lowercase identifier is a no-op to Postgres.** `SELECT "pg_read_file"(…)`
   resolves to exactly that function. The guard erased quoted identifiers wholesale and therefore
   saw nothing to refuse. Pinned by `test_a_quoted_blocked_function_name_is_still_refused` and, at
   the server, `test_a_quoted_function_name_really_does_execute`.
3. `nextval` / `setval` — ordinary-looking, `SELECT`-able, and they **commit a write**.
4. `pg_notify` — the function spelling of the `NOTIFY` statement the guard already refused. Denying
   only the statement form was security theatre.
5. `txid_current` — **assigns a real transaction id**.
6. `FOR SHARE` / `FOR KEY SHARE` — row locks the guard's list did not carry.

### Round 2 — after the pglast rewrite: 125 attacks, 0 bypasses

The old 136-input corpus kept its verdicts, with two deliberate exceptions:

- **17 keyword-shaped identifiers are now correctly _accepted_.** They are PostgreSQL *unreserved*
  keywords, so `SELECT copy FROM t` is a legal read of a column named `copy`. The old guard's claim
  that such inputs "would not be valid SQL anyway" was simply wrong. A false positive was removed;
  nothing that can write became acceptable. Seven representative cases are retained as
  `test_a_column_or_table_named_like_a_keyword_is_now_accepted`, with
  `test_reserved_keywords_in_that_position_are_still_a_syntax_error` as the counterpart.
- **2 are now _more strictly_ refused.**

> **Caveat on those corpus-level numbers.** The 136-input corpus and the 125-attack round-2 battery
> were run during the review passes; the tree retains *representative* cases, not the full corpora.
> **The 136 / 125 / 17 / 2 figures are therefore not independently re-derivable from this
> repository today.** The figures that *are* re-derivable are the ones below.

### What is re-derivable today

`tests/test_sql_hatch.py::test_every_adversarial_query_in_the_guard_corpus_is_refused_through_http`
extracts every SQL string `tests/test_sql_guard.py` hands to the guard **from that file's own AST**
(the queries live in call arguments and `parametrize` lists, not in any importable value) and drives
each one through `POST /v1/sql`. Measured at the time of writing:

| | count |
|---|---|
| queries extracted from the guard test's AST | **210** |
| refused by the guard, and re-refused through HTTP with a **byte-identical `reason`** | **159** |
| accepted by the guard, and asserted *not* refused at stage `parse` by the endpoint | **50** |
| blank-after-stripping (a malformed request; answered `400` before the guard is consulted) | **1** |

The second assertion earns its place: it is what would catch an endpoint that had quietly become
*stricter* than the control it wraps. The test asserts floors (`>= 150`, `>= 40`), not equalities,
so the corpus can grow — but it fails loudly if the extractor ever stops finding the attacks, which
would otherwise leave the test green and empty.

`test_a_refused_write_did_not_happen` counts `silver.entity_links` before and after both attack
shapes (DML-in-CTE and `;`-chaining) and asserts the count is unchanged. It is green.

## The pool Critical — the most important thing this work found

`PartnerPool` applied **both** safety settings — `statement_timeout` and
`default_transaction_read_only` — through asyncpg's `init=` callback, which runs **once per
connection creation**. asyncpg runs **`RESET ALL` on every _release_**
(`Connection.get_reset_query`). Measured against the fixture with `max_size=1` and a 20 000 ms
timeout:

| acquire | `default_transaction_read_only` | `statement_timeout` |
|---|---|---|
| #1 | `on` | `20s` |
| #2 | **`off`** | **`0`** |
| #3 | **`off`** | **`0`** |

and a `CREATE TEMP TABLE` on the next acquire **succeeded**. Postgres reads `statement_timeout = 0`
as **UNLIMITED**. So in a long-lived service, **acquire #1 was the only protected request**: from
the second release onward every typed operation and every hatch query ran against a 7.6 B-row,
3.7 TB third-party production database with no cost bound and no read-only default.

**Fixed** by moving both settings into `server_settings`, which travel in the connection's **startup
packet**. `RESET ALL` restores each GUC to its *session-start* value, and startup-packet options
**are** that value — which is exactly why `server_settings` survives the reset and a session `SET`
does not. `src/occupancy_graph/source/pool.py`'s module docstring is the canonical record; do not
"simplify" it back.

**Why it hid for so long, and the lesson.** The existing test used **four concurrent acquires**,
which force asyncpg to open **four distinct connections** — every one of them on its *first*
acquire, the only acquire that was ever safe. **The distinguishing condition is reuse, not
multiplicity.** `tests/test_pool.py` now drives sequential acquires against a `max_size=1` pool, and
`tests/test_live_smoke.py::test_the_pool_safety_settings_survive_connection_reuse` repeats that
against the real server.

The same bug was present in `tests/conftest.py::fixture_pool`, which meant the *test* pool was
read-only for one acquire and writable for the rest of the session. Also fixed, also pinned.

## Live smoke (needs credentials)

    PARTNER_DSN=… .venv/bin/python -m pytest -m live -q

13 tests in `tests/test_live_smoke.py`, collected and skipped without `PARTNER_DSN`. They are not
decorative: each asserts something the fixture **cannot** assert, and each is driven from data the
traversal itself produced, so none can pass on an empty result.

- `1104 Spring Run Rd` / `40514` must return the absentee-owner tax row with `ownerstate == "IL"`
  and `ownercity == "AURORA"` — the property is in Lexington KY. That single call exercises phase 1
  (`zip` + address prefix), phase 2 (`(upper(state), upper(city))` + prefix, the only path to tax),
  the column-shift quality gate and the projection.
- Provenance round-trips: the `__rowid` a record carried resolves back through
  `GET /v1/source-record/{shape}/{rowid}?address_id=…`, and the same call **without** `address_id`
  is a `400`.
- The `hal:` traversal is exercised against a `hal_id` obtained by the findings spec's own
  address → `entity_links` query, and the name search is driven with **that person's surname**, so
  it cannot pass on zero results.
- The EXPLAIN gate is checked in both directions: a documented access path must be **served** below
  the ceiling, and a real unindexed predicate (`employer`) must be **refused** with a hint naming
  the indexed paths.

**Three of the 13 are open questions rather than regression tests, and their failure is the news.**
Read their docstrings first:

| Test | What a red run means |
|---|---|
| `test_no_index_covers_record_id_on_the_records_tables` | An index **does** cover `record_id`; see the section below. Good news, plus edits to make. |
| `test_the_role_physically_cannot_write` | The write-revocation ask has **not** been actioned, and `sql_guard.py` is the only control in place. |
| `test_the_pool_safety_settings_survive_connection_reuse` | Either `RESET ALL` is clearing the settings again, or `PARTNER_DSN` carries its own `options=-c statement_timeout=…` that outranks `server_settings`. |

The plumbing of all 13 was smoke-tested by pointing `PARTNER_DSN` at the local fixture: 11 passed,
and the 2 failures were the correct signals (the 20-row fixture plans below the seq-scan cost gate,
and the fixture superuser owns everything). So the fixtures, the SQL and the response-key
expectations are known-good; only the corpus-specific values are unconfirmed.

## The `record_id` index contradiction — confirm this first

**Two of our own documents disagree, and the disagreement is load-bearing.**

| Source | Claim |
|---|---|
| `docs/superpowers/specs/2026-07-27-partner-records-db-findings.md` **§6, "Indexes present"** | `record_id` **is** indexed — listed among the indexes on `records_legacy` (14 idx) *and* on every `records_new` partition (18–19 idx). |
| `docs/superpowers/plans/2026-07-29-typed-data-service.md`, **"Contract B addenda", point 3** (pinned 2026-07-29) | **"no index covers `record_id`"** — and it raises the ask to add one. |

The **shipped code believes the second**: `source/search.py::rows_for_links` degrades to
`records_timed_out` on this hop, `service/handlers.py` reports that flag on
`GET /v1/person/{id}/records`, and `service/schema_doc.py` tells the agent *"record_id is not indexed
on the records tables; looking rows up by it scans."*

### What the repository can establish without credentials

- **The findings spec's claim is the one backed by introspection.** Its §6 table carries index
  *counts* and table-specific detail — `(zip, house_number)` on `records_legacy` only, `address_id`
  and the `tsv_name` GIN index on the partitions only — which reads as a real `pg_indexes` dump, not
  a summary. The `records_legacy` enumeration has exactly 14 entries including `record_id`, matching
  its own stated count of 14.
- **The newer claim was written during planning, with the corpus unreachable.**
  `.pgenv` credentials were absent for the whole of this build; that is stated in
  `docs/explain-cost-calibration.md` and in `service/limits.py`. So the newer claim did not
  supersede a measurement — nothing re-measured.
- **Neither document ever measured this hop.** This is the decisive point. Every timed chain in the
  specs *stops at* `entity_links`: the findings spec §7 ("the path that works today") ends at
  `entity_links (hal_id) 215 ms`, and its §11 working query for address → people → everything ends
  in a `GROUP BY` over `silver.entity_links` and **never joins back to `public.records_*`**. The
  surface spec's "complete access-path map — every path measured" lists `entity_links` by
  `record_id` (81 ms) and `entity_master` by `hal_id`, and lists **no records-table lookup by
  `record_id` at all**. `rows_for_links` performs exactly the hop that no spec timed.
- **One fact is true either way.** `records_partitioned` is partitioned by `imported_at`, so a
  `record_id = ANY(…)` probe **cannot prune partitions** and must touch every partition regardless
  of whether each carries a `record_id` index. The surface spec's measured
  `records_new` by `(zip, house_number)` at **28.8 s** — despite per-partition `zip` indexes
  existing — is direct evidence that partition fan-out on this corpus is expensive on its own.

### Verdict

**On the evidence available in the repository, the older findings-spec claim is more likely correct
and the pinned Contract B addendum is probably wrong as literally stated** — but it was never
contradicted by a measurement, because the hop it describes was never measured. **This is
unresolved, and it is the first thing to confirm when credentials arrive.**

`tests/test_live_smoke.py::test_no_index_covers_record_id_on_the_records_tables` is the arbiter. It
reads **both** `pg_index`/`pg_get_indexdef` (does a leading-`record_id` index exist?) **and** the
planner's chosen node types for the actual `record_id = ANY(…)` probe (does it seq-scan?), asserts
the two agree, and then pins the claim the shipped code rests on. **It may well fail — in the good
direction.**

**If an index does exist, this is what it lets us simplify:**

1. Drop partner ask 3 (below) entirely for `records_legacy`.
2. Correct Contract B addendum 3, the `rows_for_links` docstring, and the `schema_doc` caveat that
   currently tells the agent `record_id` scans — an agent acting on that caveat is avoiding a path
   that is actually fast.
3. `records_timed_out` can stop being an expected degradation on the `records_legacy` half of the
   hop and become a genuine anomaly signal. **It must stay on the `records_partitioned` half**: no
   `record_id` index prunes partitions, so that fan-out is real either way.
4. The `hal:` traversal's latency budget improves, which is what governs whether the engine can
   afford owner-elsewhere lookups inside a run.

## Known limitations — what is *not* trustworthy

1. **The fixture's index set is narrower than production, and its own comment says otherwise.**
   `tests/fixtures/schema.sql` line 68 reads *"The real index set. Tests must exercise the same
   access paths as production."* It does not. It omits `ssn`, `ssn2`, `phone`, `mobile`, `email`,
   `dob`, `record_id`, `address_id`, the `tsv_name` GIN index and the trigram indexes. Consequently
   **several access paths documented in `GET /v1/schema` are not exercised against any index
   locally** — on a 20-row table they plan as sequential scans, and the suite proves only that the
   SQL is well-formed and returns the right rows, never that the path is fast or that the index
   exists. The `-m live` smoke tests are what would actually confirm them. (The schema file is
   deliberately not edited here; correcting it is its own change with its own EXPLAIN-cost
   consequences for the hatch tests.)
2. **Selection above `MAX_ROWS_PER_SHAPE` is non-deterministic.** The scans are
   `LIMIT MAX_ROWS_PER_SHAPE` (200) with **no `ORDER BY`**, which is what makes the early stop cheap
   (a `zip` + prefix match measures 173 ms). `source/bundle.py::_stable_order` sorts the fetched
   rows in Python afterwards, so the **order** is deterministic — but **which 200 rows the database
   hands back is not**. Two identical `POST /v1/resolve` calls on a dense ZIP can therefore show the
   model a **different sample**, in a stable order. This is a deliberate trade (an `ORDER BY` applies
   before the `LIMIT` and would force a full sort of every matching row) and it is recorded, not
   fixed.
3. **Whether `server_settings` wins over a DSN that carries its own options is untested.** If
   `PARTNER_DSN` contains `?options=-c statement_timeout=…`, we do not know from here which value
   the connection ends up with. Nothing local can answer it.
   `test_the_pool_safety_settings_survive_connection_reuse` answers it on the first live run — and
   fails on acquire #1 if the DSN wins.
4. **`SET LOCAL` → `SET` is an equivalent mutant.** Stage 4 of the hatch issues
   `SET LOCAL statement_timeout = …` inside its transaction. A plain session `SET` would behave
   identically today, because nothing else runs on that connection before release and asyncpg's
   `RESET ALL` restores the startup-packet value. **A mutation swapping `LOCAL` out survives the
   suite.** `LOCAL` is still the right form: the session form's correctness would depend on
   asyncpg's release behaviour, which is the exact dependency that produced the pool Critical above.
   Recorded here rather than papered over with a test that only asserts asyncpg's internals.
5. **The `record_id` index question is open** — see the section above.
6. **The EXPLAIN cost ceilings were derived, never measured against live plans.** 5 000 000 sits
   between a *computed* lower bound (~1.8e6, the phase-2 path with 3× headroom) and a *quoted* upper
   bound (1.84e8, a `records_legacy` seq scan). The gap spans three orders of magnitude, so the
   placement inside it is not load-bearing — but no live plan has ever been compared to it.
   `scripts/explain_cost_probe.py` is the instrument; `docs/explain-cost-calibration.md` is the
   procedure; `test_the_measured_access_path_costs_still_hold` is the live check.
7. **A factual error was propagated through this codebase and is now corrected.** Several comments
   said `identity_confidence` *"peaks at 40.50"*. It does not. **40.50 is the _modal_ value** — 27.5 %
   of rows sit exactly there — with the rest of the mass across the **34–70** band and live rows
   observed at **70.85**. It is not a maximum, and an LLM reading the old phrasing would infer a
   ceiling that does not exist and treat a 40.50 row as best-available confidence.
   `service/schema_doc.py` was corrected earlier; `source/search.py` and `service/handlers.py` are
   corrected in the same commit as this document.

## Definition of Done — the plan's checklist, item by item

| # | Item | Result | Evidence |
|---|---|---|---|
| 1 | `pytest -q` → `313 passed, 4 skipped` | **PASS, count obsolete** | Actual **`548 passed, 13 skipped`**. The 313 ledger is superseded; see the section above. |
| 2 | `pytest tests/test_sql_guard.py tests/test_sql_hatch.py -q` → `41 passed`, every adversarial shape refused, `silver.entity_links` count unchanged | **PASS, count obsolete** | Actual **`211 passed`**. `test_a_refused_write_did_not_happen` green in isolation (`1 passed, 23 deselected`). 159 refusals relayed through HTTP with byte-identical reasons. |
| 3 | `grep -rn "strawberry\|occupancy_graph.graphql\|occupancy_graph.graphdb" --include=*.py --include=*.toml .` → no output | **PASS with one noted hit** | One hit: `tests/test_serve.py:36`, inside a docstring explaining why the *installed* console script had to be refreshed (it used to import `occupancy_graph.graphql.serve`). Prose, not an import or a dependency. |
| 4 | `.venv/bin/python -c "import strawberry"` → `ModuleNotFoundError` | **PASS** | `ModuleNotFoundError: No module named 'strawberry'`. |
| 5 | `docker build -t occupancy-graph-service:x016 .` | **PASS** | Built and tagged; `naming to docker.io/library/occupancy-graph-service:x016 done`. |
| 6 | `GET /v1/person/hal:HAL0001/records` returns real linked rows with `identity_confidence` and `is_suspicious` — **not a stub** | **PASS** | `200`; person `{"id":"hal:HAL0001","firstname":"JANE","lastname":"DOE","identity_confidence":40.5,"is_suspicious":false}`; `records_timed_out: false`; real rows across four shapes — `base` 1, `drive` 2, `loan` 2, `trace` 1 — each a projected partner row (`trace` row `id=1002`, `phone=5551112222`; `loan` row `id=2001`, `employer=ACME`). |
| 7 | `git status --short` clean; still on `feat/postgres-adapter`; `origin/main` untouched | **PASS** | Two commits on `feat/postgres-adapter`, clean tree after each, nothing pushed. |
| 8 | Umbrella `docs/harness/progress.md` (+ `feature_list.json`) updated by the coordinator | **OUTSTANDING — coordinator scope** | The last umbrella progress entry is *"2026-07-29 — X-016 planned"*. The completion entry has not been written. Explicitly the coordinator's item, not this repo's. |
| 9 | Contract notes 1–3 handed to the engine plan before it starts | **PASS** | All three present in `occupancy-engine-ts/docs/superpowers/plans/2026-07-29-typed-data-service.md`: required `?address_id=` (line 177), `__rowid` on bundle-sourced records (line 182), `records_timed_out` on operation 4 (line 187). |

## Container

    docker build -t occupancy-graph-service:x016 .

No volume and no mounted database: the corpus is remote and the image carries only code.

## Re-tuning the EXPLAIN ceilings

See `docs/explain-cost-calibration.md`, which also carries the partner asks this work produced.
`scripts/explain_cost_probe.py` is the instrument; point it at `PARTNER_DSN` and no code change is
needed.
