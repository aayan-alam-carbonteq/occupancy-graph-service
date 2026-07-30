# EXPLAIN cost ceiling — calibration and re-tuning

## The two ceilings

| Env var | Default | Meaning |
|---|---|---|
| `SQL_HATCH_MAX_PLAN_COST` | `5000000` | Refuse if the root plan node's `Total Cost` exceeds this. |
| `SQL_HATCH_MAX_RECORDS_SEQSCAN_COST` | `50000` | Refuse if any `Seq Scan` node on a `records_*` relation exceeds this. |

Both accept any finite value `>= 0`. `0` is the *strictest* setting, not a broken one — it
refuses everything, which makes a useful kill switch. `inf` and `nan` are **rejected at read
time**: the gate is `if cost > ceiling: refuse`, and both `cost > inf` and `cost > nan` are
False for every cost, so either value would silently turn the ceiling into an unconditional
allow. Reach for `0`, never `inf`, when you want to change the gate's behaviour wholesale.

Two further knobs bound the hatch operationally, and both require `>= 1`:

| Env var | Default | Meaning |
|---|---|---|
| `SQL_HATCH_MAX_ROWS` | `500` | Rows returned by one hatch query. |
| `SQL_HATCH_TIMEOUT_MS` | `20000` | Per-statement timeout. **`0` is refused** — Postgres reads it as UNLIMITED, so it would let one LLM-authored query run without bound against a 3.7 TB third-party production database. The row cap is no substitute: it bounds rows *returned*, not work done, and `count(*)` returns one row after a full scan. |

## How 5 000 000 was derived

The live corpus was **not** reachable when this shipped (`.pgenv` credentials absent), so the
number is derived from the measured access paths in
`docs/superpowers/specs/2026-07-28-engine-partner-db-interface-coverage.md` §2 and bracketed
by two bounds three orders of magnitude apart.

**Lower bound — the most expensive plan we must serve.** Phase 2, `(upper(state), upper(city))`
plus an address prefix on `records_partitioned`, examines 151 507 rows (613 ms warm, 53 s cold).
At default planner constants that is roughly

    151 507 x (random_page_cost 4.0 + cpu_tuple_cost 0.01 + 3 x cpu_operator_cost 0.0025)
    ~= 6.1e5

Tripled for a denser city than Lexington: **~1.8e6**.

**Upper bound — the cheapest plan we must refuse.** A `Seq Scan` on `records_legacy`
(6.24 billion rows). Contract C's own worked refusal quotes `cost=0.00..184000000.00`, i.e.
**1.84e8**. Independently: ~2.3e8 pages x seq_page_cost 1.0 + 6.24e9 x cpu_tuple_cost 0.01
~= 2.9e8, the same order.

**5e6** is 2.8x above the lower bound and 37x below the upper bound. The gap spans three
orders of magnitude, so the exact placement inside it is not load-bearing — what matters is
that it is above every measured servable path and below every measured full scan.

## Why the seq-scan rule is cost-gated

The design spec says "refuse if the plan contains a sequential scan on a records table",
unqualified. Implemented literally that refuses every hatch query in this repo's own test
suite: the fixture tables hold ~20 rows each and Postgres correctly plans a seq scan for all
of them, so the hatch would be unreachable in tests. `50 000` (~40 k pages, ~300 MB) is above
every fixture scan (< 25) and below every real partition, making the rule unconditional in
production while keeping it testable here.

## Re-tuning against the live corpus

    .venv/bin/python scripts/explain_cost_probe.py --dsn "$PARTNER_DSN"

Take the largest `MUST SERVE` cost, multiply by 3, confirm the result is at least 10x below
the smallest `MUST REFUSE` cost, and set:

    SQL_HATCH_MAX_PLAN_COST=<that number>
    SQL_HATCH_MAX_RECORDS_SEQSCAN_COST=<smallest MUST REFUSE cost / 100>

If the two bounds ever overlap, the ceiling is not the right control and the refusal must
move to an explicit access-path allowlist. Record the run in this file.

---

## Partner asks this work created

In priority order. Ask 1 is a **ship blocker**; ask 2 is a **question**, and it is cheap, and it
must be answered before ask 3 is worth raising at all.

### 1. A role that physically cannot write — REQUIRED BEFORE THE HATCH SHIPS

Not tidy. Not a nice-to-have. **Required.**

The parse guard in `src/occupancy_graph/service/sql_guard.py` is our primary write control, and
since the `pglast` rewrite it is built on **PostgreSQL's own parser** rather than a hand-written
lexer — so it no longer has to re-derive the server's grammar to be correct. That fixed the
category of bug that produced its two Critical bypasses.

**It does not, and cannot, fix effects.**

    SELECT nextval('some_sequence')

is **one statement**, its head node is a **`SelectStmt`**, it has no `intoClause`, no
`LockingClause`, and **no DML node anywhere in its parse tree**. Every structural rule the guard
enforces passes it. And it **commits a write**. `setval` is the same shape. So is any function a
future PostgreSQL release names in a family we did not think to list.

The consequence is not "the denylist has a gap we should close". It is stronger:

> **The parser fixed query _shape_. It cannot fix _effects_. Therefore no function denylist can
> ever be proven complete, and a role that physically cannot write is the only durable control.**

Secondary evidence, which was the original form of this ask and is still true:
`default_transaction_read_only` is a session **default**, not a boundary — raw `BEGIN READ WRITE`
defeats it, pinned by
`tests/test_pool.py::test_read_only_is_a_session_default_not_a_boundary`, where the INSERT commits.

What we are asking for: a guest role with **no `INSERT` / `UPDATE` / `DELETE` / `TRUNCATE`** on any
relation and **no `CREATE`** on any schema or on the database.
`tests/test_live_smoke.py::test_the_role_physically_cannot_write` checks exactly that, using
`has_table_privilege` / `has_schema_privilege` / `has_database_privilege` — it reads catalogue
functions only and attempts no write. A green run is the confirmation this ask was actioned.

### 2. Confirm whether an index covers `record_id` on the records tables — CONFIRM THIS FIRST

**Two of our own documents contradict each other, and the shipped code depends on the answer.**

| Source | Claim |
|---|---|
| `docs/superpowers/specs/2026-07-27-partner-records-db-findings.md` **§6, "Indexes present"** | `record_id` **is** indexed — listed among the indexes on `records_legacy` (14 idx) and on every `records_new` partition (18–19 idx). |
| `docs/superpowers/plans/2026-07-29-typed-data-service.md`, **"Contract B addenda" point 3** (pinned 2026-07-29) | **"no index covers `record_id`"**. |

The whole `records_timed_out` degradation in `src/occupancy_graph/source/search.py::rows_for_links`
— and the honesty of the `hal:` traversal about empty results — rests on the **newer** claim. So
does the `GET /v1/schema` caveat that currently tells the agent *"record_id is not indexed on the
records tables; looking rows up by it scans."*

**From the repository alone, the older claim looks more likely to be right.** §6 carries index
counts and table-specific detail that reads like a real `pg_indexes` dump; the newer claim was
written during planning with the corpus unreachable, so it superseded nothing. Critically,
**neither document ever measured this hop** — every timed chain in the specs stops at
`entity_links` (findings §7 and §11; the surface spec's "every path measured" table lists no
records-table lookup by `record_id` at all), and `rows_for_links` performs precisely the hop nobody
timed.

**Ask:** run `\d public.records_legacy` and `\d` on one `records_partitioned` partition, or send us
`pg_indexes` for both, so we can settle it in one round trip.
`tests/test_live_smoke.py::test_no_index_covers_record_id_on_the_records_tables` does it
automatically the first time anyone runs `-m live`: it reads the catalog **and** the planner's
chosen node types and asserts they agree. See `docs/verification.md` for what a confirmed index
would let us simplify.

### 3. An index on `records_legacy(record_id)` and `records_partitioned(record_id)` — IF ask 2 says none exists

`silver.entity_links` is indexed both ways (`(source_table, record_id)` UNIQUE at 81 ms, `(hal_id)`
at 215 ms), but the rows those links point at are fetched by `record_id`. If nothing covers it,
that is the one unindexed hop in the typed surface: `GET /v1/person/hal:…/records` runs it under
the statement timeout and reports `records_timed_out`.

**Note this holds partially even if ask 2 finds an index.** `records_partitioned` is partitioned by
`imported_at`, so a `record_id = ANY(…)` probe **cannot prune partitions** and must touch every
partition regardless. The measured 28.8 s for `records_new` by `(zip, house_number)` — despite
per-partition `zip` indexes existing — is what that fan-out costs in practice.

### 4. Backfill `zip` and `house_number` on `property_owner` rows

From `raw_data.zipCodePlusFour` / `raw_data.streetNumber`, plus the matching index. This turns a
53 s city-wide scan into a sub-100 ms lookup, and it remains the **single highest-leverage** ask for
data quality (coverage spec §7; findings spec §10.1).

It is not primarily about our queries. Those rows carry no `ssn`, no `dob` and no `house_number`, so
**no blocking key of any kind can be built for them** and they are excluded from
`silver.entity_links` entirely — the partner's *own* entity resolution cannot see them. Populating
the column makes them findable by `(zip, house_number)` *and* eligible for the `name_house_zip` key,
which pulls them into the entity graph. One change, both paths. Scope is ~233 M rows in one
partition, not the whole corpus.

### 5. Index `source_file`, or supply a definitive feed inventory

`source_file` is the **only** feed identity in the corpus and it is unindexed, so feeds can be
sampled but never enumerated. `GET /v1/schema` currently has to warn the agent never to use it as a
driving predicate.

### 6. Confirm the refresh model and where the entity-resolution migration stands

Does a re-import rewrite rows or append, and is `imported_at` the only signal? Separately,
`silver.processing_state` shows 15 `backfill_canonical` workers **paused** since 2026-06-21 with the
note *"killed — switching to bulk sequential Path 1 rewrite"*, while `flag_suspicious` is still
`running`. `entity_master.canonical_*` may therefore be incomplete, and we surface those fields to
the model.

---

## A correction this work had to make

Several comments in this codebase said `identity_confidence` **"peaks at 40.50"**. That is wrong.
**40.50 is the _modal_ value** — 27.5 % of rows sit exactly there — with the rest of the mass across
the **34–70** band and live rows observed at **70.85**. It is not a maximum. The old phrasing would
lead an LLM to infer a ceiling that does not exist and read a 40.50 row as best-available
confidence. Corrected in `service/schema_doc.py`, `source/search.py` and `service/handlers.py`.
