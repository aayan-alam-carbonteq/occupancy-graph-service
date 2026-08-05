# The Local Partner Clone — What It Is, and What Building It Proved

**Date:** 2026-08-05 · **Branch:** `feat/partner-db-local-clone`
**Operational instructions:** `clone/README.md`. This document is the *findings* —
what we learned, what it cost, and what remains impossible.

---

## TL;DR

We built a local Postgres reproducing the partner corpus (`all_data`) from the
Lexington CSVs, so hypotheses can be tested without live credentials.

Along the way we discovered the CSVs are **a subset of the partner corpus
itself**, not an independent extraction. That single fact invalidated the
loader's central design assumption and forced a rewrite of how population is
handled — after which the clone's rows became byte-identical to production's.

Building it also surfaced **four bugs, one of them live in production.**

Measured accuracy:

| | |
|---|---|
| clone rows that exist **identically** in production | **83.3%** |
| production rows the clone reproduces | **46.9%** |
| retrieval fidelity, `records_new` shapes (loan/auto/tax) | **100%** |
| retrieval fidelity, `trace` | 89% |
| retrieval fidelity, `utility` | 15% (structural — see §5) |

---

## 1. The finding that changed the design

The loader was built assuming the Lexington CSVs were an *independent
extraction* whose field population had to be calibrated toward production's.
Nobody had tested that.

Testing it took one query against the indexed `(last_name, zip)` path — never a
scan. Every resident our CSVs place at 1104 SPRING RUN RD is present in
production **at the same address, in the same feeds**, and the rows match field
for field:

```
production  KENNETH WORTHINGTON  dob=1965-01-01  phone=6062240200
clone       KENNETH WORTHINGTON  dob=1965-01-01  phone=6062240200
production  KENNETH WORTHINGTON  dob=1965-01-01  phone=5152626434
clone       KENNETH WORTHINGTON  dob=1965-01-01  phone=5152626434
production  TAMIE   WORTHINGTON  dob=1968-01-01  phone=6062240200
clone       TAMIE   WORTHINGTON  dob=(DELETED)   phone=6062240200   <-- us
production  TAMIE   WORTHINGTON  dob=1970-09-01  phone=NULL
clone       TAMIE   WORTHINGTON  dob=1970-09-01  phone=NULL
```

Three of four already matched exactly. The one divergence was **a real `dob` our
own sampler had removed.**

**Consequence:** if a CSV value *is* production's value, sampling it can only
move the clone away from production. Worse, `feed_id_coverage` is a *national*
per-feed average, so calibrating a Lexington subset against it layered a second
error on the first. Down-sampling was removed; those rows are now byte-identical.

Two columns remain loader-controlled, both for reasons that survive the finding:

- **`ssn`** — our CSVs carry none, so it must be synthesised (900-999 area range,
  never issued by the SSA). Its *rate* is load-bearing: populating it only on the
  payday feeds is what makes `entity_links` skew to `records_new` and leaves tax
  unlinked — production's own mechanism, reproduced rather than imitated.
- **`house_number`** — our CSVs carry it on trace/auto/tax where production has it
  NULL. The old cleaning pipeline parsed it out of address text; production never
  stored it there. Gating it to base/USCRM restores production's reality, and it
  is the resident hop's only anchor.

---

## 2. Four bugs, one live in production

**The name hop filtered the address *after* its LIMIT.** `_scan_legacy_via_residents`
ran `WHERE last_name=$1 AND zip=$2 LIMIT 400`, then applied the address prefix in
Python. The LIMIT therefore bounded the *surname's rows across the whole ZIP*, so
the few at the subject address were frequently not in the slice fetched at all.
Measured in ZIP 40517: `COX` has 585 rows of which 5 are at the address; `MARTIN`
has 1,147 of which 0 are. **This happens live too**, and more often — production's
ZIPs are denser. Fixed by pushing the predicate into SQL; the index still drives
the scan, and `address ILIKE` becomes a heap filter over hundreds of rows instead
of ~273,000. Trace retrieval 78% → 89%.

**`hal_id` bpchar padding leaked into every search response.** `hal_id` is
`char(15)` in production; Postgres pads bpchar on *read*, so `'HAL0001'` returns
as `'HAL0001        '`. `WHERE hal_id = $1` still matched (bpchar comparison
ignores trailing whitespace), which is exactly why it hid — but the padding
reached the response `id` and every `hal:` citation handle. **Live in
production.** Invisible until the clone reproduced the real column width; the
fixture's `text` type had masked it.

**The DDL was not idempotent.** A second load aborted on
`DROP VIEW IF EXISTS public.records_new` — which does *not* no-op when the name
is a TABLE — leaving the tables undropped, which cascaded into duplicate-index
and duplicate-table errors. A routine re-load left a half-built schema.

**`pythonpath` omitted the repo root.** The suite passed under
`python -m pytest` (which prepends CWD itself) and died under the bare `pytest`
console script with `Interrupted: 2 errors during collection` — taking the
*entire* suite down, not just the new tests.

---

## 3. The base split: volume fidelity vs behavioural fidelity

base is our only `house_number`-bearing feed, so it is the only source of
resident-hop anchors — and the hop scans `records_legacy` exclusively.

It was originally split 1:7 toward `records_new` to mirror production's
`feed_id_coverage` *volume* proportions. Measured on the 12 benchmark addresses,
that left **20 of 22 base rows in `records_new` and 2 usable anchors**, so 10 of
12 addresses had none and the hop could not run.

The reasoning was wrong because production's anchor pool is not USCRM alone — it
also draws on SSNxDOB, the 2014 phonebook and Historic Data, none of which we
hold. Reproducing base's volume ratio while missing those three *compounds*
anchor scarcity instead of mirroring production.

**Volume proportion is cosmetic fidelity; anchor density is behavioural.** The
split now favours legacy:

| | before | after |
|---|---|---|
| addresses with a surviving anchor | 2 / 12 | **11 / 12** |
| hop recall where anchors exist | 31.6% (n=2) | **39.1%** (100/256, n=11) |

---

## 4. The date coercion is faithful, not lossy

The loader drops 99,540 unparseable `dob` values. It would be easy to read that
as data loss and "fix" it. It isn't: every rejected value has the form
`YYYY0000` — year-only, month and day zeroed, not a date at all.

We parse **93.4%** of utility dobs. Production's utility `dob` population is
**93.9%**. Within half a point — production discards the same garbage. Dropping
rather than guessing *is* production's behaviour.

---

## 5. What cannot be closed with this data

**Three anchor feeds are absent.** `2014 US Phonebook`, `Historic Data
17-Nov-2025` and `SSNxDOB` were never in our raw source, which is
`Lexington_11DB (Uc+Au+Tx+Tr+Ln+Dr+Cr+Li+Vo)`. This is what caps `utility`
retrieval at 15%: at 1057 SPRING RUN RD the clone holds 33 utility rows across
**20 distinct surnames** but only **1 anchor surname**. The hop reaches only
residents it can name. `MAX_NAME_HOPS` (8) is nowhere near binding — there is no
cap to raise.

**`drive.csv` has zero rows** at any of the 12 benchmark addresses, so `drive`
returns nothing there. No join key recovers licences absent from the source.

**This could be made to look better, and should not be.** Giving trace/auto a
`house_number` would hand the hop anchors production does not have, inflating
local recall into a flattering fiction and destroying the coverage measurement's
meaning.

Closing either gap for real needs an extract taken *from* the partner corpus —
option §4.4 in `docs/superpowers/specs/2026-08-04-partner-ask.md` (workspace repo).

---

## 6. What the clone is still not for

- **Performance.** 2.36 M rows fit in RAM. Production's pathology is ~195 ms cold
  random reads against 3,749 GB. An unindexed address scan that never finishes in
  production returns in milliseconds here. **Plan shape transfers; latency does
  not.**
- **Entity-resolution quality.** Production blocks on SSN; we synthesise ours. You
  can test *consumption* of the entity graph, never whether better ER would
  improve verdicts.

---

## 7. Reproducing the measurements

```bash
# row-level parity against production (needs PARTNER_DSN; indexed paths only)
PARTNER_DSN=... CLONE_DSN=... .venv/bin/python -m clone.parity_check

# resident-hop coverage loss -- unmeasurable against production, because the
# full ZIP scan never finishes there (observed ACTIVE at 14+ minutes)
CLONE_DSN=... .venv/bin/python -m clone.coverage_experiment

# per-shape diff against the recorded live benchmark run
CLONE_DSN=... .venv/bin/python -m clone.compare_to_live

# fidelity assertions (skipped without CLONE_DSN, so CI stays green)
CLONE_DSN=... .venv/bin/python -m pytest tests/clone/test_clone_profile.py
```

---

## 8. The 12-address benchmark, clone vs production (2026-08-05)

Same gold, same judge (`claude-sonnet-4-6`), same engine mode (`tools`), same
model (`haiku-4-5`). The only variable is which database answered.

**Gold set first.** Scoring against signals the partner corpus structurally
cannot provide penalises the engine for data absence rather than reasoning, so
signals grounded in absent feeds were removed —
`benchmarks/gold-labels/mini_packet_signal_survey.superset.partner_corpus.json`
in the `occupancy-engine` repo.

The correction is smaller than expected, and the measurement says so:
`criminal` and `linkedin` appear **nowhere** in the gold evidence; only `voter`
does, across 15 signals. Of those, just **1** was wholly voter-dependent — the
other 14 also cite feeds we hold, so dropping them outright would have credited
the engine for missing things it can genuinely find. Only the 1 unanswerable
signal was dropped; the other 14 kept, with their dead voter evidence stripped.
**349 → 348 signals.**

| family | signals | LIVE | CLONE | clone/live |
|---|---|---|---|---|
| property | 40 | 0.963 | 0.938 | **97%** |
| owner | 69 | 0.884 | 0.812 | **92%** |
| case | 146 | 0.473 | 0.356 | 75% |
| subject | 77 | 0.487 | 0.377 | 77% |
| portfolio | 8 | 0.625 | 0.500 | 80% |
| legal | 4 | 0.250 | 0.500 | 200%¹ |
| loan | 4 | 0.875 | 1.000 | 114%¹ |
| **OVERALL** | **348** | **0.619** | **0.530** | **86%** |

¹ n=4. Do not read these as the clone beating production; at that sample size a
single signal swings the ratio.

**The clone reproduces 86% of production's benchmark accuracy** — and the
distribution matches the retrieval decomposition in §5 exactly:

- **property 97%, owner 92%.** The assessor path retrieves at 100% (§5), and
  these are the families that decide absentee ownership. This is the evidence
  the product actually turns on, and it is very nearly production-grade locally.
- **case 75%, subject 77%.** 223 of 348 signals, and both lean on the
  utility/trace evidence the resident hop cannot fully reach — utility retrieval
  is 15%, capped by holding 1 of production's 4 anchor feeds.

### Cost of running it

| | live corpus | clone |
|---|---|---|
| wall clock, 12 addresses | 51 min | **9.5 min** |
| avg agent latency | 252 s | **47 s** |
| LLM calls | 356 | 310 |
| input tokens | 3.95 M | 2.95 M |
| agent cost | $4.10 | **$2.97** |
| credentials needed | partner DSN | none |

Retrieval is essentially free locally (0.2 s vs 62 s), and the LLM work shrinks
too because the hop surfaces less evidence to reason over — which is also why
the accuracy is 86% rather than 100%. The two are the same fact seen twice.

**Caveat on the run:** one packet (`subject_occupancy_surfaces` @ 1332 OX HILL
DR) failed to grade with a `JudgeError` and is excluded from the clone's
denominator. One of 84 packets, so the effect is small, but the clone figure is
very slightly optimistic for that reason.

**What the voter filter was worth:** almost nothing at the aggregate level
(live went 0.629 unfiltered → 0.619 filtered, within judge variance), but
`legal` moved 0.250 → 0.500 on the clone. The absent feeds were never the main
story; the anchor thinness is.

---

## 9. The address index recovers the SQLite-era accuracy — and prices the partner ask

The SQLite graph scored **0.815** on these *exact same rows*. The clone with the
resident hop scored 0.530. Identical data, 0.285 apart — so the loss was never
data, it was the **access path**. SQLite modelled addresses as first-class keys
(`addresses` 354,992 rows, `address_edges` 2.4 M) and retrieved directly. The
partner schema has `address` as unindexed free text, forcing the lossy hop.

The clone is ours, so the index the partner ask requests can simply be created:

```sql
CREATE INDEX idx_records_legacy_zip_addr
  ON public.records_legacy (zip, upper(address) varchar_pattern_ops);
```

Then `OCCUPANCY_LEGACY_SCAN=direct` selects the collapsed zip+address scan
instead of the hop. Same gold, same judge, same engine mode, same model:

| family | signals | LIVE (hop) | clone (hop) | clone **+ index** | gain |
|---|---|---|---|---|---|
| case | 146 | 0.473 | 0.356 | **0.784** | +0.428 |
| subject | 77 | 0.487 | 0.377 | **0.955** | +0.578 |
| owner | 69 | 0.884 | 0.812 | **0.978** | +0.167 |
| property | 40 | 0.963 | 0.938 | **1.000** | +0.062 |
| portfolio | 8 | 0.625 | 0.500 | 0.500 | — |
| loan | 4 | 0.875 | 1.000 | 1.000 | — |
| legal | 4 | 0.250 | 0.500 | 0.500 | — |
| **OVERALL data** | **348** | **0.619** | **0.530** | **0.878** | **+0.348** |
| **reasoning** | | 0.598 | 0.552 | **0.760** | **+0.208** |

**0.878 against the SQLite era's 0.815 — the target is met and exceeded**, on
the same rows, through the partner schema.

### What this establishes

1. **The data was always sufficient for 0.8+.** Every point between 0.530 and
   0.878 was locked behind an access path, not missing evidence. The clone did
   not need more data; it needed a way to reach what it had.
2. **The partner ask now has a price tag.** An index on `(zip, address)` is
   worth **+0.348 data coverage** over the hop, and **+0.259 over the live
   corpus today**. The §4.1 ask stops being "our workaround is lossy, please
   help" and becomes "this index recovers a third of our benchmark accuracy."
3. **The resident hop is a workaround, and this is its cost.** It was built
   because the unindexed scan does not finish in production. It works, and it
   costs ~0.35 coverage — quantified rather than asserted.
4. **The gains land exactly where the hop was lossy.** `subject` +0.578 and
   `case` +0.428 are the families that lean on utility/trace, which the hop
   reached at 15%. `property` was already 0.938 and moves only to 1.000, because
   the assessor path never went through the hop at all. The accuracy table and
   the retrieval table in §5 are the same finding measured two ways.
5. **`portfolio`/`legal`/`loan` do not move** — they are not address-scan bound.
   Their remaining gap is content (`drive.csv` empty at these addresses) and
   absent feeds, which no index fixes.

### What it costs

| | clone + hop | clone + index |
|---|---|---|
| avg agent latency | 47.3 s | 61.7 s |
| LLM calls | 310 | 333 |
| input tokens | 2.95 M | 3.66 M |
| agent cost | $2.97 | **$3.72** |

+25% tokens and +25% cost to retrieve what the hop was missing. Cheap for
+0.348 coverage.

### The honest limit on this result

`OCCUPANCY_LEGACY_SCAN=direct` is **off by default** and must stay that way.
Against the live corpus that scan does not finish — `address` is unindexed
there, so it degenerates into a heap filter over the whole ZIP (~273k rows at
~195 ms a cold page, observed ACTIVE at 14+ minutes). This measures what
production *would* score **if the index existed**, which is exactly the question
the ask turns on. It is not a claim about production today.

Latency here is meaningless (2.36 M rows in RAM). **Plan shape and retrieval
completeness transfer; timings do not.**
