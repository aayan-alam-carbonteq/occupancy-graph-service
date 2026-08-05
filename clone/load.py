"""Load the Lexington CSVs into a local partner clone.

    .venv/bin/python -m clone.load --csv-dir /path/to/occupancy-engine/data/cleaned/lexington

RUN AS A MODULE, NEVER AS A SCRIPT PATH -- `clone` is not an installed package,
so `python clone/load.py` puts only `clone/` on sys.path, not the repo root, and
every `from clone...` import below raises `ModuleNotFoundError`. See
clone/loader/__init__.py and clone/README.md.

The CSV directory is CONFIGURATION, never a hardcoded cross-repo path: it lives
in another repo (occupancy-engine) and is DVC-tracked, so pinning a path here
would silently break the moment that repo's DVC cache moved.

Shapes voter / criminal / linkedin / realtor are deliberately NOT loaded. The
first three are absent from all 114 production feeds; realtor is external
evidence the backend injects per-run, not partner data. Loading them would let
the local engine find evidence it can never find live.

TWO PASSES over the CSVs, not one:

  Pass 1 (identity)  builds ONE union-find PersonIndex across every shape's
                      rows, in FEED_PLANS order. Nothing but the six identity
                      fields is kept in memory here -- no column mapping, no
                      raw row -- because a person's cluster can only be known
                      once every shape that might mention them has been seen,
                      and 2.36M full rows held in memory at once is the kind
                      of memory pressure worth avoiding by construction rather
                      than discovering under load.

  Pass 2 (write)     re-reads the SAME CSVs, in the SAME FEED_PLANS order, and
                      maps + populates + writes each plan's rows before moving
                      to the next -- flushing per plan, not once at the end,
                      for the same memory reason. This only works because
                      `_plan_rows` is a pure function of (plan, csv_dir,
                      limit): re-invoking it yields the identical row sequence
                      Pass 1 saw, so a plain running counter reproduces the
                      exact node index PersonIndex assigned each row without
                      re-running union-find. The `assert` after the write loop
                      is the tripwire if that ever stops being true.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime
import json
import os
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import asyncpg

from clone.loader.csvsource import read_shape_csv
from clone.loader.entity import build_entities
from clone.loader.feedplan import FEED_PLANS, FeedPlan
from clone.loader.identity import DRIVE_JOIN_KEY, PersonIndex, synthetic_ssn
from clone.loader.mapping import partner_row_for
from clone.loader.population import keep_value, load_targets
from clone.loader.writer import RecordBatch
from occupancy_graph.source.manifest import SHAPES

DEFAULT_DSN = "postgresql://clone:clone@127.0.0.1:55433/partner_clone"

# Per-shape CSV header names used for identity clustering and the drive join --
# these are the upstream vendor's OWN CSV headers, read directly off the raw
# row (never through partner_row_for/manifest.py, which maps to the PARTNER's
# column names, not the CSV's). "" means the shape's CSV carries no such field.
SHAPE_FIELDS: dict[str, tuple[str, str, str, str, str, str]] = {
    "utility": ("first_name", "last_name", "address", "zip", "dob", "phone"),
    "trace":   ("firstname", "lastname", "address", "zip", "", "phone"),
    "base":    ("firstname", "lastname", "primaryaddress", "zip", "dob", "phone"),
    "loan":    ("firstname", "lastname", "address", "zip", "", ""),
    "auto":    ("firstname", "lastname", "address", "zip", "", "phone"),
    "tax":     ("firstname", "lastname", "address", "zip", "", ""),
}

# Disjoint record_id ranges: record_id is unique only WITHIN a storage family,
# not globally -- see src/occupancy_graph/source/search.py::_PhysicalTable.
RECORD_ID_BASE = {"records_legacy": 1_000_000_000, "records_new": 7_000_000_000}

# base spans both roots. The split is deliberately weighted TOWARD legacy --
# see the long note on the base FeedPlans in loader/feedplan.py: base is our
# only anchor-bearing feed and the resident hop scans records_legacy only, so
# production's 1:7 volume ratio would starve the anchors the coverage
# experiment exists to measure. 1 row in 8 goes to records_new; the rest stay
# legacy. Split by position so the same row always lands on the same side.
BASE_LEGACY_MODULUS = 8

# DELIBERATELY EMPTY. Do not re-add dob/phone/email here.
#
# These columns are NOT down-sampled, because the Lexington CSVs are a
# SUBSET OF THE PARTNER CORPUS -- not an independent extraction. Verified
# 2026-08-05 by pulling the same people out of production over the indexed
# (last_name, zip) path: the residents our CSVs place at 1104 SPRING RUN RD
# are present in production at the same address, in the same feeds, and the
# rows match field for field:
#
#   production  KENNETH WORTHINGTON  dob=1965-01-01  phone=6062240200
#   clone       KENNETH WORTHINGTON  dob=1965-01-01  phone=6062240200
#   production  TAMIE   WORTHINGTON  dob=1968-01-01  phone=6062240200
#   clone       TAMIE   WORTHINGTON  dob=(DELETED)   phone=6062240200   <-- us
#
# The only divergence found was a real dob this sampler had removed. Its
# whole premise -- that our population had to be calibrated toward
# production's per-feed averages -- assumed a different extraction. Since the
# values ARE production's values, sampling them can only move the clone away
# from production, never toward it. feed_id_coverage is also a NATIONAL
# average, so calibrating a Lexington subset against it imported a second
# error on top of the first.
#
# Still synthesised/gated, for reasons that remain valid:
#   ssn          our CSVs carry NONE, so it must be synthesised (below)
#   house_number our CSVs carry it on trace/auto/tax where production has it
#                NULL -- an artifact of the old cleaning pipeline parsing it
#                out of the address, not something production holds. Gating
#                it to base/USCRM restores production's reality.
OPTIONAL_POPULATION_COLUMNS: tuple[str, ...] = ()

# house_number is LOADER-OWNED, not manifest-driven, and that distinction is
# load-bearing.
#
# It is an ACCESS PATH, not projected evidence. No shape reads it as a column --
# tax takes it from raw_data.streetNumber, and trace/auto/base declare it
# `derived()` because the READ path computes it from address text. But
# resolve.py::_scan_legacy_via_residents anchors with
# `WHERE zip = $1 AND house_number = $2`, querying the COLUMN directly, and that
# anchor is the only bridge from an address to the residents whose names then
# reach the utility/trace rows.
#
# Leaving it to mapping.py meant it was never written at all, so the clone had
# ZERO anchors: resolving 1104 SPRING RUN RD returned utility=0, trace=0 where
# the live corpus returns 1 and 3. That silently disables the resident hop --
# and with it both the correctness testing and the coverage measurement this
# clone exists for.
#
# Which feeds carry it is still decided by the recorded production profile
# (feed_population.json: base 100%, everything else 0%), NOT by which CSVs
# happen to have a housenumber column. Ours carry it at ~100% on trace/auto/tax
# where production has it NULL; honouring the CSV instead of the profile would
# hand the hop anchors production does not have and make the coverage number a
# flattering fiction.
HOUSE_NUMBER_CSV_FIELD = "housenumber"


CATALOG_PATH = Path(__file__).parent / "profiles" / "records_catalog.json"

# csv.DictReader hands every field back as a plain str (csvsource.py strips
# padding but does not type-convert), and partner_row_for carries that str
# straight through into `columns`. asyncpg's binary COPY needs the REAL Python
# type for anything that is not text/character/jsonb -- a bare str for a date
# or integer column fails with e.g. "'str' object has no attribute
# 'toordinal'" the moment a batch containing one reaches flush(). This coerces
# by SQL type, read from the same recorded catalog writer.py uses, so there is
# one source of truth for "what type is column X" rather than a second
# hand-kept list here.
_TRUE_STRINGS = {"1", "t", "true", "y", "yes"}
_FALSE_STRINGS = {"0", "f", "false", "n", "no"}
# Vendor dates are not uniformly formatted -- utility.csv's dob is YYYYMMDD
# ("19640714") but dod mixes YYYYMMDD, MMDDYYYY ("01222008") and outright
# garbage ("09001975", valid under neither reading). Try the formats in order;
# a parse that lands outside a sane birth/death year range is treated as the
# garbage it almost certainly is, same as an outright ValueError.
_DATE_FORMATS = ("%Y%m%d", "%m%d%Y")
_DATE_YEAR_BOUNDS = (1900, 2026)


def _coerce_boolean(value: str) -> bool | None:
    normalized = value.strip().lower()
    if normalized in _TRUE_STRINGS:
        return True
    if normalized in _FALSE_STRINGS:
        return False
    return None


def _coerce_date(value: str) -> datetime.date | None:
    stripped = value.strip()
    for fmt in _DATE_FORMATS:
        try:
            parsed = datetime.datetime.strptime(stripped, fmt).date()
        except ValueError:
            continue
        if _DATE_YEAR_BOUNDS[0] <= parsed.year <= _DATE_YEAR_BOUNDS[1]:
            return parsed
    return None


def _coerce_int(value: str) -> int | None:
    try:
        return int(value.strip().replace(",", ""))
    except ValueError:
        return None


def _coerce_float(value: str) -> float | None:
    try:
        return float(value.strip())
    except ValueError:
        return None


_COERCERS = {
    "date": _coerce_date,
    "integer": _coerce_int,
    "bigint": _coerce_int,
    "boolean": _coerce_boolean,
    "double precision": _coerce_float,
}


def _load_catalog_types() -> dict[str, str]:
    return {name: typ for name, typ in json.loads(CATALOG_PATH.read_text())}


def _coerce_columns(
    columns: dict[str, Any], catalog_types: dict[str, str], failures: dict[str, int]
) -> dict[str, Any]:
    """Convert raw CSV strings to the Python type asyncpg needs for each
    non-text column; DROP a value that will not parse rather than guess.

    A wrong guess would silently corrupt the clone. base.csv's businessowner
    is the clearest case: its values are opaque vendor codes ('A', '2', '9',
    ...), not a plain Y/N, and there is no principled way to map most of them
    to true/false -- so they are dropped, same as production would show NULL
    for a value it could not represent either. `failures` accumulates a count
    per column so the run's summary can report what -- and how much -- was
    dropped, instead of this happening silently.
    """
    coerced: dict[str, Any] = {}
    for key, value in columns.items():
        coercer = _COERCERS.get(catalog_types.get(key, "text"))
        if coercer is None or not isinstance(value, str):
            coerced[key] = value
            continue
        result = coercer(value)
        if result is None:
            failures[key] = failures.get(key, 0) + 1
        else:
            coerced[key] = result
    return coerced


def _identity_fields(shape: str, row: dict[str, str]) -> dict[str, str]:
    first, last, address, zip_, dob, phone = SHAPE_FIELDS[shape]
    return dict(
        first=row.get(first, ""), last=row.get(last, ""),
        address=row.get(address, ""), zip=row.get(zip_, ""),
        dob=row.get(dob, "") if dob else "",
        phone=row.get(phone, "") if phone else "",
    )


def _plan_rows(
    plan: FeedPlan, csv_dir: Path, limit_per_shape: int | None
) -> Iterator[dict[str, str]]:
    """Yield the rows THIS plan owns, in file order.

    A pure function of its arguments: re-calling it (Pass 2, after Pass 1
    already consumed one generator instance) re-opens the file and reproduces
    the identical sequence, which is what lets Pass 2 track node indices with
    a plain counter instead of re-running union-find.
    """
    path = csv_dir / f"{plan.shape}.csv"
    if not path.exists():
        raise SystemExit(f"missing {path} -- is --csv-dir correct?")
    kept = 0
    for position, row in enumerate(read_shape_csv(path)):
        if plan.shape == "base":
            to_legacy = position % BASE_LEGACY_MODULUS != 0
            if (plan.table == "records_legacy") != to_legacy:
                continue
        if limit_per_shape is not None and kept >= limit_per_shape:
            break
        kept += 1
        yield row


def _tsvector_encoder(value: object) -> bytes:
    if value is not None:
        # asyncpg only invokes a type encoder for NON-NULL values -- NULL is
        # encoded by the wire protocol directly, bypassing this function
        # entirely. RecordBatch (writer.py) never populates tsv_name, so this
        # is a loud guard against a reachable path that should not exist
        # today, not a real encoder: its "success" case is never exercised.
        raise NotImplementedError(
            "tsv_name codec is a NULL-only placeholder -- RecordBatch never "
            "populates tsv_name (see clone/loader/writer.py and the plan's "
            "'known gap, deliberate' note on it), so a non-null value here "
            "means that has changed and this needs a real tsvector encoder."
        )
    return b""


async def _register_tsvector_codec(conn: asyncpg.Connection) -> None:
    """asyncpg ships no binary codec for tsvector (OID 3614), so
    `RecordBatch.flush` -- which COPYs the full 144-column catalog including
    the always-NULL tsv_name -- fails with "no binary format encoder for type
    tsvector" the moment it is asked to encode a real row, even though every
    value it will ever see for that column is NULL. Registering a codec here,
    on the caller's connection, fixes that without touching writer.py, whose
    column list is (correctly) driven by the recorded catalog rather than by
    what asyncpg happens to support.
    """
    await conn.set_type_codec(
        "tsvector", schema="pg_catalog",
        encoder=_tsvector_encoder, decoder=lambda data: data, format="binary",
    )


def _load_licences(drive_csv: Path) -> dict[tuple[str, str, str, str], tuple[str, str]]:
    """DRIVE_JOIN_KEY -> (dl_number, dl_state), first match wins.

    Never emitted as its own record -- production has no drive feed; these are
    payday-loan rows that happen to carry a licence number, folded onto their
    matching loan row below.
    """
    if not drive_csv.exists():
        raise SystemExit(f"missing {drive_csv} -- is --csv-dir correct?")
    licences: dict[tuple[str, str, str, str], tuple[str, str]] = {}
    for row in read_shape_csv(drive_csv):
        licences.setdefault(DRIVE_JOIN_KEY(row), (row.get("dl_num", ""), row.get("dl_state", "")))
    return licences


def _population_source_fields(shape: str) -> dict[str, str]:
    """partner column -> the CSV field that feeds it, for the tracked columns.

    Resolved from the manifest ONCE per shape rather than per row, so measuring
    source population costs one dict lookup per column per row rather than a
    full re-mapping. house_number is added separately because it is
    loader-owned, not manifest-driven -- see HOUSE_NUMBER_CSV_FIELD.
    """
    fields = {
        origin.key: name
        for name, origin in SHAPES[shape].fields.items()
        if origin.kind == "col" and origin.key in OPTIONAL_POPULATION_COLUMNS
    }
    fields["house_number"] = HOUSE_NUMBER_CSV_FIELD
    return fields


async def _build_identity(
    csv_dir: Path, limit_per_shape: int | None
) -> tuple[PersonIndex, dict[str, dict[str, float]]]:
    """Pass 1: union-find identity, plus the SOURCE population rate per column.

    The rates are measured here rather than in a third read because this pass
    already visits every row, and Pass 2 needs them BEFORE it can decide what to
    keep: without them the down-sample multiplies against an already-sparse
    source instead of targeting production's rate. See population.keep_value.
    """
    index = PersonIndex()
    populated: dict[str, dict[str, int]] = {}
    totals: dict[str, int] = {}
    for plan in FEED_PLANS:
        source_fields = _population_source_fields(plan.shape)
        seen = populated.setdefault(plan.shape, dict.fromkeys(source_fields, 0))
        count = 0
        for row in _plan_rows(plan, csv_dir, limit_per_shape):
            index.add(**_identity_fields(plan.shape, row))
            for column, csv_field in source_fields.items():
                if row.get(csv_field, ""):
                    seen[column] += 1
            count += 1
        totals[plan.shape] = totals.get(plan.shape, 0) + count
        print(f"  identity: {plan.shape:8s} -> {plan.table:15s} {count:>9,} rows", flush=True)

    rates = {
        shape: {
            column: (n / totals[shape] if totals.get(shape) else 0.0)
            for column, n in columns.items()
        }
        for shape, columns in populated.items()
    }
    return index, rates


async def _write_records(
    conn: asyncpg.Connection,
    csv_dir: Path,
    limit_per_shape: int | None,
    persons: dict[int, int],
    licences: dict[tuple[str, str, str, str], tuple[str, str]],
    targets,
    source_rates: dict[str, dict[str, float]],
) -> tuple[int, int, int, int, dict[str, int], dict[str, int]]:
    """Pass 2: map, populate and COPY every plan's rows, flushing per plan.

    Returns (total_written, total_oracle_rows, loan_total, loan_with_licence,
    per_shape_counts, coercion_failures).
    """
    node_counter = 0
    record_id_counters = dict(RECORD_ID_BASE)
    total_written = 0
    total_oracle = 0
    loan_total = 0
    loan_with_licence = 0
    shape_totals: dict[str, int] = {}
    catalog_types = _load_catalog_types()
    coercion_failures: dict[str, int] = {}

    for plan in FEED_PLANS:
        batch = RecordBatch()
        oracle_rows: list[tuple[int, str, int, str]] = []
        start_id = record_id_counters[plan.table]
        plan_count = 0
        # FeedPlan.imported_at is a plain "YYYY-MM-DD" str (it is also read as
        # a LIKE-adjacent literal by feeds.py's partition pruning); the
        # physical column is timestamptz, so it needs the same str -> real
        # date conversion as every other typed column, just computed once per
        # plan rather than per row since it is constant across the plan's rows.
        imported_at = datetime.date.fromisoformat(plan.imported_at) if plan.imported_at else None

        for row in _plan_rows(plan, csv_dir, limit_per_shape):
            person_id = persons[node_counter]
            node_counter += 1
            record_id = record_id_counters[plan.table]
            record_id_counters[plan.table] += 1

            columns, raw_data = partner_row_for(plan.shape, row)
            columns = _coerce_columns(columns, catalog_types, coercion_failures)
            row_key = f"{plan.shape}:{record_id}"

            if plan.shape == "loan":
                loan_total += 1
                licence = licences.get(DRIVE_JOIN_KEY(row))
                if licence:
                    columns["dl_number"], columns["dl_state"] = licence
                    loan_with_licence += 1

            # ssn is STAMPED (added), never carried by the CSV itself -- see
            # module docstring and clone/loader/entity.py for why this single
            # decision is what makes the entity graph link almost only loan
            # rows, matching production's mechanism rather than its numbers.
            shape_rates = source_rates.get(plan.shape, {})
            # ssn is STAMPED, never carried by the CSV, so it has no source
            # rate to correct against -- the target IS the final rate.
            if keep_value(plan.shape, "ssn", row_key, targets):
                columns["ssn"] = synthetic_ssn(person_id)
            # ADDED, not dropped -- see HOUSE_NUMBER_CSV_FIELD. The profile gate
            # is what confines it to the feeds production populates.
            house_number = row.get(HOUSE_NUMBER_CSV_FIELD, "")
            if house_number and keep_value(plan.shape, "house_number", row_key, targets,
                                           shape_rates.get("house_number")):
                columns["house_number"] = house_number
            for optional in OPTIONAL_POPULATION_COLUMNS:
                if optional in columns and not keep_value(
                        plan.shape, optional, row_key, targets, shape_rates.get(optional)):
                    columns.pop(optional)

            batch.add(record_id=record_id, table=plan.table, source_file=plan.source_file,
                      imported_at=imported_at, columns=columns, raw_data=raw_data)
            oracle_rows.append((person_id, plan.table, record_id, plan.shape))
            plan_count += 1

        written = await batch.flush(conn)
        if oracle_rows:
            await conn.copy_records_to_table(
                "true_person_record", schema_name="bench",
                columns=["person_id", "source_table", "record_id", "shape"],
                records=oracle_rows,
            )
        total_written += written
        total_oracle += len(oracle_rows)
        shape_totals[plan.shape] = shape_totals.get(plan.shape, 0) + plan_count

        end_id = record_id_counters[plan.table] - 1
        span = f"{start_id:,}..{end_id:,}" if plan_count else "(none)"
        print(f"  wrote {plan_count:>9,} rows for {plan.shape:8s} -> {plan.table:15s} "
              f"record_id {span}", flush=True)

    if node_counter != len(persons):
        raise RuntimeError(
            f"identity/write pass diverged: identity pass saw {len(persons)} rows, "
            f"write pass saw {node_counter}. _plan_rows must yield identical "
            f"sequences across both passes for record_id/person_id assignment "
            f"to line up -- this is a bug, not a data issue."
        )

    if coercion_failures:
        dropped = ", ".join(f"{col}={count:,}" for col, count in sorted(coercion_failures.items()))
        print(f"  coercion: dropped unparseable typed values -- {dropped}", flush=True)

    return total_written, total_oracle, loan_total, loan_with_licence, shape_totals, coercion_failures


async def _write_true_person(conn: asyncpg.Connection) -> int:
    """bench.true_person (person_id, synthetic_ssn, record_count), derived from
    bench.true_person_record -- record_count here is a plain count of that
    person's rows, and synthetic_ssn is recomputed from identity.py's own
    function rather than duplicated as a second formula, so there is exactly
    one place that bijection can drift."""
    counts = await conn.fetch(
        "SELECT person_id, count(*) AS record_count FROM bench.true_person_record "
        "GROUP BY person_id"
    )
    rows = [(r["person_id"], synthetic_ssn(r["person_id"]), r["record_count"]) for r in counts]
    if rows:
        await conn.copy_records_to_table(
            "true_person", schema_name="bench",
            columns=["person_id", "synthetic_ssn", "record_count"], records=rows,
        )
    return len(rows)


async def _write_entity_graph(conn: asyncpg.Connection) -> tuple[int, int, int, int]:
    """silver.entity_master / entity_links, built by ssn-blocking over every
    loaded record. Returns (masters, links, new_links, legacy_links)."""
    entity_rows = await conn.fetch("""
        SELECT record_id, 'records_legacy'::text AS source_table, ssn, first_name,
               last_name, address, city, state, zip
        FROM public.records_legacy
        UNION ALL
        SELECT record_id, 'records_new'::text AS source_table, ssn, first_name,
               last_name, address, city, state, zip
        FROM public.records_new
    """)
    masters, links = build_entities([dict(r) for r in entity_rows])
    if masters:
        await conn.copy_records_to_table(
            "entity_master", schema_name="silver",
            columns=list(masters[0]), records=[list(m.values()) for m in masters],
        )
    if links:
        await conn.copy_records_to_table(
            "entity_links", schema_name="silver",
            columns=list(links[0]), records=[list(l.values()) for l in links],
        )
    new_links = sum(1 for l in links if l["source_table"] == "records_new")
    legacy_links = len(links) - new_links
    return len(masters), len(links), new_links, legacy_links


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--csv-dir", type=Path, required=True,
                        help="Directory holding the Lexington shape CSVs "
                             "(utility.csv, trace.csv, base.csv, loan.csv, "
                             "tax.csv, auto.csv, drive.csv).")
    parser.add_argument("--dsn", default=os.environ.get("CLONE_DSN", DEFAULT_DSN),
                        help="Clone Postgres DSN. Defaults to $CLONE_DSN, then "
                             f"{DEFAULT_DSN!r}.")
    parser.add_argument("--limit-per-shape", type=int, default=None,
                        help="Deterministic head-N rows per plan, for fast CI "
                             "cycles. NOT a full load -- see the warning printed "
                             "when set.")
    args = parser.parse_args()

    if not args.csv_dir.is_dir():
        raise SystemExit(f"--csv-dir {args.csv_dir} is not a directory")

    if args.limit_per_shape is not None:
        print(f"*** --limit-per-shape={args.limit_per_shape} is set: this is a "
              f"TRUNCATED load, not the full corpus. Do not mistake row counts "
              f"below for production-scale figures. ***\n", flush=True)

    started = time.monotonic()
    targets = load_targets()

    print("Pass 1/2: building the person identity graph across all shapes...", flush=True)
    index, source_rates = await _build_identity(args.csv_dir, args.limit_per_shape)
    persons = index.person_ids()
    distinct_persons = len(set(persons.values()))
    print(f"  {len(persons):,} rows clustered into {distinct_persons:,} persons\n", flush=True)
    # The source rates are what turn each target into a TARGET rather than a
    # discount applied on top of already-sparse data -- see keep_value.
    for shape in sorted(source_rates):
        measured = {c: f'{r:.1%}' for c, r in sorted(source_rates[shape].items()) if r}
        if measured:
            print(f"  source population {shape:8s} {measured}", flush=True)
    print(flush=True)

    print("Loading drive licences for the loan fold-in...", flush=True)
    licences = _load_licences(args.csv_dir / "drive.csv")
    print(f"  {len(licences):,} distinct drive join keys\n", flush=True)

    print("Pass 2/2: mapping, populating and writing records...", flush=True)
    conn = await asyncpg.connect(args.dsn)
    try:
        await _register_tsvector_codec(conn)
        written, oracle_rows, loan_total, loan_with_licence, shape_totals, coercion_failures = (
            await _write_records(conn, args.csv_dir, args.limit_per_shape, persons,
                                 licences, targets, source_rates)
        )

        print("\nPopulating bench.true_person from the oracle...", flush=True)
        true_person_count = await _write_true_person(conn)
        print(f"  {true_person_count:,} true persons")

        print("\nBuilding the entity graph (ssn blocking)...", flush=True)
        masters, links, new_links, legacy_links = await _write_entity_graph(conn)
        print(f"  {masters:,} entities, {links:,} links "
              f"(records_new {new_links:,} : records_legacy {legacy_links:,})")

        print("\nANALYZE public.records_legacy, public.records_new...", flush=True)
        await conn.execute("ANALYZE public.records_legacy")
        await conn.execute("ANALYZE public.records_new")
    finally:
        await conn.close()

    elapsed = time.monotonic() - started
    per_shape = ", ".join(f"{shape}={count:,}" for shape, count in shape_totals.items())
    print(f"\n{'=' * 72}")
    print(f"loaded {written:,} records in {elapsed:.1f}s  ({per_shape})")
    print(f"oracle: {oracle_rows:,} true_person_record rows, {distinct_persons:,} true persons")
    if loan_total:
        print(f"drive fold-in: {loan_with_licence:,}/{loan_total:,} loan rows "
              f"received a licence ({loan_with_licence / loan_total:.1%})")
    print(f"entity graph: {masters:,} entities, {links:,} links "
          f"(records_new {new_links:,} : records_legacy {legacy_links:,})")
    if args.limit_per_shape is not None:
        print(f"*** TRUNCATED LOAD (--limit-per-shape={args.limit_per_shape}) -- "
              f"not representative of the full corpus. ***")


if __name__ == "__main__":
    asyncio.run(main())
