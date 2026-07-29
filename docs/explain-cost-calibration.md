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
