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
