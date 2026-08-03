"""Address resolution against the partner corpus.

Phase 1 uses the `zip` btree plus a prefix filter on the free-text `address`
column. This is THE access path: `house_number` is 0% populated on every feed the
adapter reads, so predicating on it produces a plan that scans.

Measured: 173 ms on records_new (1.4 B rows), 1.30 s warm / 32 s cold on
records_legacy (6.24 B rows).
"""
from __future__ import annotations

import json
import logging
from collections import Counter
from dataclasses import dataclass, field

import asyncpg

from occupancy_graph.normalize import normalize_address, zip5
from occupancy_graph.source import quality
from occupancy_graph.source.feeds import FEEDS, feed_clause, pattern_groups, shapes_for_row
from occupancy_graph.source.pool import PartnerPool

# Shapes reachable by the zip index. `tax` is excluded: property_owner rows have
# a NULL zip, so they need the phase-2 city/state path.
ZIP_SHAPES = ("utility", "trace", "base", "loan", "drive", "auto")

# Per-shape materialization ceiling. Tool calls cap at 100 and preflight at 10,
# so 200 leaves headroom while bounding a dense apartment building.
MAX_ROWS_PER_SHAPE = 200

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AddressQuery:
    raw: str
    norm_address: str
    zip5: str
    like_prefix: str

    @classmethod
    def build(cls, address: str, zip_code: str | None) -> "AddressQuery":
        raw = (address or "").strip()
        normalized = normalize_address(raw)
        # Prefix on house number + first street token ONLY: selective enough
        # to filter a ZIP's worth of rows, loose enough to survive suffix
        # spelling drift ("RD" vs "ROAD"), missing suffixes, and unit
        # designators, none of which the free-text column normalizes.
        # "1104 Spring Run Road" -> "1104 Spring" (not "...Run" or "...Run
        # Road" -- a longer prefix looks more selective but risks silently
        # losing rows on any suffix/unit variation, which is worse than the
        # extra heap-filter cost of a broader prefix). "123 Main St Apt 4" ->
        # "123 Main" -- the unit designator ("Apt 4") must never enter the
        # prefix, or it would fail to match a stored row that omits or
        # abbreviates it differently. Tokenizing (rather than slicing the raw
        # string) also collapses irregular internal whitespace instead of
        # baking it into the LIKE pattern. A leading token that merely
        # *starts* with a digit counts as a house number ("12A"), since
        # alphanumeric house numbers are real. With no house number at all,
        # the whole raw string is used unmodified.
        tokens = raw.split()
        if tokens and tokens[0][:1].isdigit():
            house = tokens[0]
            prefix = f"{house} {tokens[1]}" if len(tokens) > 1 else house
        else:
            prefix = raw
        return cls(
            raw=raw,
            norm_address=normalized,
            zip5=zip5(zip_code),
            like_prefix=f"{prefix}%",
        )


@dataclass
class ZipScanResult:
    rows_by_shape: dict[str, list[dict]] = field(default_factory=dict)
    city: str | None = None
    state: str | None = None


async def scan_zip_sources(pool: PartnerPool, query: AddressQuery) -> ZipScanResult:
    result = ZipScanResult(rows_by_shape={shape: [] for shape in ZIP_SHAPES})
    if not query.raw:
        # An empty address has no prefix worth of its own: `like_prefix` would
        # be a bare "%", which matches every row in the ZIP (~270k on the real
        # corpus) truncated to MAX_ROWS_PER_SHAPE -- silently arbitrary rows
        # presented as a match. Refuse to query at all instead.
        return result
    # ONE scan per TABLE, not one per (shape, table). Every shape on a table
    # shares the identical indexed predicate and therefore the identical heap
    # read; the only difference is an unindexed source_file filter. Scanning
    # per shape re-read the same pages once per shape -- three times over on
    # records_legacy, which is where essentially all the time goes.
    tables: list[str] = []
    for shape in ZIP_SHAPES:
        for table in FEEDS[shape].tables:
            if table not in tables:
                tables.append(table)

    for table in tables:
        groups = pattern_groups(ZIP_SHAPES, table)
        if not groups:
            continue
        for row in await _scan_table(pool, table, groups, query):
            # A row can belong to more than one shape (loan/drive are the same
            # physical payday row), so this assigns rather than partitions.
            for shape in shapes_for_row(row):
                if shape in result.rows_by_shape:
                    result.rows_by_shape[shape].append(row)

    # MAX_ROWS_PER_SHAPE is a per-SHAPE budget and the window function applies
    # it per (table, group). A shape spanning two tables -- `base` -- can still
    # arrive with double, so the ceiling is re-applied here exactly as before.
    for shape in ZIP_SHAPES:
        del result.rows_by_shape[shape][MAX_ROWS_PER_SHAPE:]

    cities = Counter(
        str(row["city"]).strip().upper()
        for rows in result.rows_by_shape.values()
        for row in rows
        if row.get("city")
    )
    states = Counter(
        str(row["state"]).strip().upper()
        for rows in result.rows_by_shape.values()
        for row in rows
        if row.get("state")
    )
    # Majority vote, not first-seen: fetch() has no ORDER BY, and the loose
    # address prefix can admit a different street in the same ZIP. Task 13's
    # property_owner scan is driven by these, so a wrong city silently yields
    # no tax rows. Ties broken by name for determinism.
    result.city = min(cities, key=lambda c: (-cities[c], c)) if cities else None
    result.state = min(states, key=lambda s: (-states[s], s)) if states else None
    return result


def decode_raw_data(row: dict) -> dict:
    """asyncpg hands jsonb back as a str on some connections and a dict on
    others. Normalize to a dict, and to {} on malformed JSON -- a projection
    crash would take down an entire investigation.

    Public because search.py fetches rows by record_id rather than through
    _scan_one, and both paths must deliver raw_data in the same shape."""
    value = row.get("raw_data")
    if isinstance(value, str):
        try:
            row["raw_data"] = json.loads(value)
        except ValueError:
            row["raw_data"] = {}
    return row


def _collapsed_scan_sql(
    table: str, groups: list[tuple[str, tuple[str, ...]]]
) -> tuple[str, list[str]]:
    """ONE query covering every shape on `table`, with a per-group row budget.

    WHY THIS IS ONE QUERY AND NOT N. The predicate is
    `zip = $1 AND address ILIKE $2 AND source_file LIKE ...`, and only `zip` is
    indexed. `address` and `source_file` are heap filters, so EVERY row in the
    ZIP must be read off disk to evaluate them. Measured on the live corpus:
    the backend sits at 100% `IO / DataFileRead` and the planner expects
    `rows=10` to survive -- so `LIMIT` never short-circuits either, because the
    scan cannot know it is done until it has read everything.

    That cost is paid PER SCAN, and it is identical for every shape on the
    table: same ZIP, same address prefix, same pages. Running one scan per
    shape re-read the same heap three times over on records_legacy to return
    three disjoint handfuls of rows. This reads it once.

    The per-group budget is preserved exactly rather than approximated. A bare
    `LIMIT MAX_ROWS_PER_SHAPE * len(groups)` would let one dense shape consume
    the whole allowance and starve the others -- silently, and only at the
    addresses (large apartment buildings) where the cap matters at all. The
    window function gives each group its own MAX_ROWS_PER_SHAPE, which is what
    the per-shape scans guaranteed.

    `drive` is deliberately absent from `groups` -- it shares `loan`'s patterns
    and is re-derived by shapes_for_row via `dl_number`. See feeds.pattern_groups.
    """
    params: list[str] = []
    case_arms: list[str] = []
    all_predicates: list[str] = []
    for label, patterns in groups:
        placeholders = []
        for pattern in patterns:
            params.append(pattern)
            # +3: $1 is zip, $2 is the address prefix.
            placeholders.append(f"source_file LIKE ${len(params) + 2}")
        predicate = " OR ".join(placeholders)
        all_predicates.append(predicate)
        # Label is a literal from FEEDS, never caller text.
        case_arms.append(f"WHEN {predicate} THEN '{label}'")
    case_sql = "CASE " + " ".join(case_arms) + " END"
    where_sql = " OR ".join(f"({predicate})" for predicate in all_predicates)
    sql = f"""
        SELECT * FROM (
            SELECT *, row_number() OVER (
                       PARTITION BY {case_sql} ORDER BY record_id
                     ) AS _feed_rank
            FROM public.{table}
            WHERE zip = $1
              AND address ILIKE $2
              AND ({where_sql})
        ) ranked
        WHERE _feed_rank <= {MAX_ROWS_PER_SHAPE}
    """
    return sql, params


async def _scan_table(
    pool: PartnerPool, table: str, groups: list[tuple[str, tuple[str, ...]]],
    query: AddressQuery,
) -> list[dict]:
    """Run the collapsed scan for one table. Shape assignment happens in Python."""
    sql, patterns = _collapsed_scan_sql(table, groups)
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, query.zip5, query.like_prefix, *patterns)
    out = []
    for row in rows:
        record = dict(row)
        # Scan bookkeeping, not partner data -- must not reach the projection.
        record.pop("_feed_rank", None)
        out.append(decode_raw_data(record))
    return out


@dataclass
class TaxScanResult:
    rows: list[dict] = field(default_factory=list)
    dropped: int = 0
    timed_out: bool = False
    # What this scan actually searched. An empty `rows` with timed_out=False is
    # ambiguous -- the property may genuinely have no assessor record, OR
    # phase 1's majority-vote city may have been wrong for this address (the
    # address prefix is loose enough to admit a neighbouring street).
    # Recording the parameters makes that distinguishable downstream instead
    # of silent.
    queried_city: str | None = None
    queried_state: str | None = None


async def scan_tax_source(
    pool: PartnerPool, query: AddressQuery, *, city: str | None, state: str | None
) -> TaxScanResult:
    """Phase 2: property_owner rows via the (upper(state), upper(city)) index.

    property_owner rows have `zip` and `house_number` 0% populated, so the zip
    index cannot see them. City/state comes from the phase-1 rows.

    Measured: 613 ms warm / 53 s cold. The statement timeout is the guard; on
    expiry we report tax as absent rather than failing the investigation. The
    engine degrades correctly on its own — case_quality_and_synthesis flips to
    run_for_absence and the tax packets skip on their field gate.

    `dropped` counts quality-gate rejections among the rows actually fetched:
    if MAX_ROWS_PER_SHAPE truncates the candidate set, a column-shifted row
    past the cutoff is never fetched and never counted, so `dropped` can
    under-report the true corruption rate in the corpus.
    """
    if not city or not state:
        return TaxScanResult(queried_city=city, queried_state=state)

    city_upper, state_upper = city.upper(), state.upper()
    clause, patterns = feed_clause("tax", start_index=4)
    sql = f"""
        SELECT *
        FROM public.records_new
        WHERE upper(state) = $1
          AND upper(city) = $2
          AND address ILIKE $3
          AND {clause}
        LIMIT {MAX_ROWS_PER_SHAPE}
    """
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(sql, state_upper, city_upper, query.like_prefix, *patterns)
    except asyncpg.QueryCanceledError as exc:
        # QueryCanceledError also fires for an admin pg_cancel_backend, not
        # just a statement_timeout expiry -- same sqlstate 57014, only the
        # message differs. Both degrade identically (tax absent), so no
        # behaviour change; the log line just gives on-call a way to tell
        # which one happened.
        logger.warning(
            "tax scan cancelled (city=%s, state=%s): %s", city_upper, state_upper, exc
        )
        return TaxScanResult(timed_out=True, queried_city=city_upper, queried_state=state_upper)

    kept: list[dict] = []
    dropped = 0
    for row in rows:
        decoded = decode_raw_data(dict(row))
        if quality.tax_row_is_usable(decoded):
            kept.append(decoded)
        else:
            dropped += 1
    return TaxScanResult(
        rows=kept, dropped=dropped, queried_city=city_upper, queried_state=state_upper
    )
