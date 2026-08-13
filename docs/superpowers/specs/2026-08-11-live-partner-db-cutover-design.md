# Live Partner DB Cutover — Design

**Date:** 2026-08-11 · **Branch:** `feat/live-partner-db` (off `feat/partner-db-local-clone`)
**Status:** approved, implementing

## Why now

The partner built the address indexes we asked for (2026-08-10 approval, verified live
2026-08-11). Address-first access — the thing this database structurally could not do — now
works. This change points the typed data service at the live corpus through those indexes.

Deployed indexes, all `indisvalid`, verified against `20.42.94.87/all_data`:

| Index | Relation | Definition |
|---|---|---|
| `idx_records_legacy_zip_normaddr` | `records_legacy` | `(zip, silver.s5_street_norm(address) text_pattern_ops) WHERE zip IS NOT NULL` |
| `idx_records_new_zip_normaddr` | `records_new` + 5 partitions | same |
| `idx_p20260301_property_owner_addr` | `records_partitioned_p20260301` | `(upper(state), upper(city), silver.s5_street_norm(address) text_pattern_ops) WHERE source_file LIKE 'property_owner%'` |

## The core change: the predicate, not a flag

Every scan emits `address ILIKE $2`. **`ILIKE` can never use a btree**, and the indexes are on
`silver.s5_street_norm(address)` — a different expression. An expression index is only usable
when the query repeats the expression exactly. So the predicate becomes:

```sql
silver.s5_street_norm(address) LIKE silver.s5_street_norm($n) || '%'
```

Verified live at the real query shapes (2026-08-11):

- collapsed phase-1 scan, `records_legacy`, ZIP 02816 + source_file filters →
  `Index Scan using idx_records_legacy_zip_normaddr`, **554 ms, 15 rows** (utility + trace at
  the address — exactly what the resident hop existed to work around).
- phase-2 tax, `records_new` parent + `imported_at` pruning →
  `Index Scan using idx_p20260301_property_owner_addr` on the single partition, **26 ms**
  (was 19 s warm / 241 s cold).

## Decisions

**Prefix strategy unchanged.** Phase 1 keeps the loose `house number + first street token`
prefix. Normalization fixes `LN` vs `LANE`, but *not* a stored row that omits the suffix
entirely — a longer prefix would silently lose those. The original reasoning survives the
index; only the expression wrapping it changes.

**LIKE metacharacters are stripped, not escaped.** Input flows into a `LIKE` pattern through
the function, so `%` or `_` in an address would become wildcards. Escaping interacts badly
with server-side normalization (the escape char would itself be normalized), and `%`, `_`,
`\` do not occur in real US addresses. `AddressQuery.build` strips them.

**Hard cutover.** `_scan_legacy_via_residents`, its three bounds constants,
`_direct_legacy_scan_enabled` and the `OCCUPANCY_LEGACY_SCAN` env var are deleted.
`records_legacy` becomes an ordinary `_scan_table` call. The hop cost ~0.35 measured
accuracy and only ever existed because the scan could not finish.

**Index preflight refuses to serve.** Hard cutover means no fallback, against a database we
do not control. On startup the service asserts all three indexes exist and are valid; if any
is missing it raises in the lifespan, so the container never passes its healthcheck. The
failure mode this replaces is silent: a dropped index degrades into multi-minute hangs.

**`schema_doc.py` must teach the new predicate.** `GET /v1/schema` is how the agent learns to
write hatch SQL. Left stale, it would keep describing `address` as unindexed free text and
LLM-authored SQL would emit `ILIKE`, get seq-scan-refused by the guard, and burn repair turns.

## Credentials and prod wiring

`compose.yaml` in the engine repo already reads `${PARTNER_DSN}` and passes it to the graph
service; the engine reaches the service at `DATA_URL=http://graph:8000`. This change adds:

- `.env.prod.example` — a committed template. The real `.env` stays gitignored.
- Fail-fast DSN validation naming the missing/malformed variable.
- Startup logging of the DSN with the password redacted.

No Key Vault, no secrets driver, no new dependency.

## Test fixtures

`ddl/` gains `silver.s5_street_norm` and the three indexes, or the fixture-backed suite runs
against a topology production no longer has.
`tests/clone/test_ddl_matches_production.py` is the existing parity guardrail.

## Out of scope

Key Vault; any parallel/comparison mode; the graded 12-address benchmark; any change to
heuristics, prompts, scoring or report shape.

## Verification — results (2026-08-11)

**Python suite: 621 passed, 32 skipped.** Two pinned contracts fired and were amended
deliberately, each with the reason recorded at the assertion:

- `test_schema_doc.py` Contract C — the refusal hint's leading token changed from `zip` to
  `(zip, s5_street_norm(address))`. The hint token *is* the repair instruction handed to the
  agent on a 422; leaving it as `zip` would keep agents writing `address ILIKE` forever.
- `test_ddl_matches_production.py` — production's `records_legacy` index set gained
  `idx_records_legacy_zip_normaddr`. A new test also pins that the clone's address indexes are
  expression indexes carrying `text_pattern_ops` and the partial `property_owner%` predicate,
  since a same-named index on the bare column would satisfy the old assertion and be unusable.

**Engine repo: no regressions.** 379 pass / 9 fail / 5 errors, byte-identical with and without
this change (verified by stashing). Those failures are pre-existing on trunk: 29 of the 44
typecheck errors are in GraphQL-era test files (`fingerprint_endpoint`, `fingerprint_probe`,
`address_resolution`) that import modules deleted in the X-016 migration, and the E2E-1 failure
is the one AGENTS.md already documents as known-failing. **The `bun run verify` gate cannot go
green until those dead test files are dealt with — separately from this work.**

**Live smoke, container built from this branch against the real corpus.** Startup logs the
redacted DSN and the preflight result. Five addresses, all HTTP 200, no tax timeouts:

| Address | Latency | Rows | Breakdown |
|---|---|---|---|
| 101 Pembroke Ln, 02816 | 2.87 s | 18 | utility 6, trace 9, base 3 |
| 45 Bates Ave, 02816 | 2.96 s | 70 | utility 24, trace 43, base 3 |
| 2 Raymond St, 02816 | 3.08 s | 91 | utility 20, trace 65, base 6 |
| 106 Read Ave, 02816 | 3.33 s | 130 | utility 50, trace 77, base 3 |
| 1104 Spring Run Rd, 40514 | 2.37 s | 40 | utility 15, trace 21, base 1, auto 2, **tax 1** |

The utility and trace rows are the point: they are the shapes the resident hop reached at 15%,
and at 101 Pembroke Ln the hop had no anchor at all — the same address returns 0 rows through
`silver.entity_links`. The benchmark address returning a live `tax` row exercises the assessor
partial index end-to-end.

**Preflight failure path, proven end-to-end** by pointing a container at a Postgres without the
function: it exits **code 3**, never serves a request, and logs
`MissingIndexError: silver.s5_street_norm(text) does not exist ... raise it with them before
restarting`.

**One defect found and fixed during verification.** The first live container started, served
correctly, and printed neither the DSN nor the preflight result: uvicorn configures only its own
`uvicorn.*` loggers and leaves the root logger unhandled, so every `occupancy_graph` log line was
being discarded. `serve.py` now calls `logging.basicConfig` before `create_app()`. Both lines are
confirmed present in the rebuilt image.
