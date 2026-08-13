"""Address resolution against the partner corpus.

Phase 1 uses the composite `(zip, silver.s5_street_norm(address))` index: an
equality on the ZIP and a PREFIX RANGE on the normalized address, both index
conditions. `house_number` is 0% populated on every feed this adapter reads, so
predicating on it produces a plan that scans.

THE PREDICATE IS LOAD-BEARING AND MUST NOT BE "SIMPLIFIED" BACK TO ILIKE.
An expression index is only usable when the query repeats the expression
verbatim, and `ILIKE` cannot use a btree at all. `address ILIKE $2` -- what this
module emitted until the partner built the indexes -- degenerates into a heap
filter over every row in the ZIP (~273k scattered reads at ~195 ms a cold page
on a 3.7 TB heap). That query was observed server-side ACTIVE at 14+ minutes
without completing, and cancelled at our own 120 s ceiling on 2026-08-10.

Measured live 2026-08-11 at the real query shapes:
  phase 1  records_legacy, ZIP 02816 + source_file filters   554 ms, 15 rows
           (Index Scan using idx_records_legacy_zip_normaddr)
  phase 2  records_new parent, imported_at-pruned            26 ms
           (Index Scan using idx_p20260301_property_owner_addr)
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

# The normalizing function the partner's expression indexes are built on. Every
# address predicate must wrap BOTH sides in it -- the column so the index is
# usable at all, the parameter so the two sides agree on suffix spelling.
STREET_NORM = "silver.s5_street_norm"

# LIKE metacharacters, stripped from address input rather than escaped.
#
# The parameter is normalized SERVER-side and then concatenated with '%' to form
# a LIKE pattern, so a `%` or `_` arriving in an address would be interpreted as
# a wildcard: "10%" would match every house number in the ZIP beginning "10".
# Escaping is the usual answer and is wrong here -- the escape character would
# itself pass through s5_street_norm (which upper-cases and rewrites
# punctuation), so the escaped pattern and the indexed value would no longer
# correspond. None of these characters occurs in a real US address, so removing
# them is both safe and simpler than an ESCAPE clause the normalizer can break.
_LIKE_METACHARS = str.maketrans({"%": None, "_": None, "\\": None})


@dataclass(frozen=True)
class AddressQuery:
    raw: str
    norm_address: str
    zip5: str
    # The bare address prefix, WITHOUT a trailing '%'. SQL wraps it in
    # s5_street_norm and appends the wildcard; see build().
    address_prefix: str

    @classmethod
    def build(cls, address: str, zip_code: str | None) -> "AddressQuery":
        raw = (address or "").strip().translate(_LIKE_METACHARS).strip()
        normalized = normalize_address(raw)
        # Prefix on house number + first street token ONLY, and the index does
        # NOT change that. s5_street_norm unifies suffix SPELLING ("ROAD" ->
        # "RD"), so it removes one of the three reasons this prefix is short --
        # but not the other two, and those are the ones that lose rows:
        #   * a stored row with NO suffix at all ("101 PEMBROKE") is not
        #     reachable from a longer prefix ("101 PEMBROKE LN"), and
        #     normalization cannot invent the missing token;
        #   * a unit designator ("APT 4") must never enter the prefix, or a
        #     stored row that omits it -- or writes it differently -- is missed.
        # A longer prefix therefore still looks more selective while silently
        # losing rows, which is the worse failure. Selectivity is no longer the
        # scarce resource anyway: the prefix range is an INDEX condition now,
        # not a heap filter, so a broader prefix costs index entries rather than
        # ~195 ms cold pages.
        #
        # "1104 Spring Run Road" -> "1104 Spring". "123 Main St Apt 4" ->
        # "123 Main". Tokenizing (rather than slicing the raw string) also
        # collapses irregular internal whitespace instead of baking it into the
        # pattern. A leading token that merely *starts* with a digit counts as a
        # house number ("12A"), since alphanumeric house numbers are real. With
        # no house number at all, the whole raw string is used unmodified.
        tokens = raw.split()
        if tokens and tokens[0][:1].isdigit():
            prefix = f"{tokens[0]} {tokens[1]}" if len(tokens) > 1 else tokens[0]
        else:
            prefix = raw
        return cls(
            raw=raw,
            norm_address=normalized,
            zip5=zip5(zip_code),
            # BARE value, no trailing '%'. The wildcard is appended in SQL,
            # AFTER s5_street_norm has run on this parameter -- a '%' carried in
            # here would be normalized as data and then followed by the real
            # wildcard, matching a prefix one character shorter than intended.
            address_prefix=prefix,
        )


@dataclass
class ZipScanResult:
    rows_by_shape: dict[str, list[dict]] = field(default_factory=dict)
    city: str | None = None
    state: str | None = None


async def scan_zip_sources(pool: PartnerPool, query: AddressQuery) -> ZipScanResult:
    result = ZipScanResult(rows_by_shape={shape: [] for shape in ZIP_SHAPES})
    if not query.raw:
        # An empty address has no prefix of its own: the pattern would collapse
        # to a bare "%", matching every row in the ZIP (~270k on the real
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
        # Every table takes the same path now, records_legacy included. It used
        # to divert through a resident hop because the address filter could not
        # finish here; that hop cost ~0.35 measured accuracy and is deleted.
        rows = await _scan_table(pool, table, groups, query)
        for row in rows:
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

    WHY THIS IS ONE QUERY AND NOT N. `zip` and the normalized address prefix are
    both INDEX conditions on `(zip, s5_street_norm(address))`, but `source_file`
    is not indexed and remains a heap filter over whatever survives the index
    range. That residual cost is identical for every shape on the table -- same
    ZIP, same address prefix, same heap rows -- so running one scan per shape
    re-read the same rows once per shape to return disjoint handfuls. This reads
    them once.

    The argument was originally much starker: before the partner built the
    indexes, `address ILIKE $2` forced EVERY row in the ZIP off disk (~273k
    scattered reads), so N scans meant N full-ZIP reads. That is fixed, and the
    collapse is still correct for the smaller reason.

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
              AND {STREET_NORM}(address) LIKE {STREET_NORM}($2) || '%'
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
        rows = await conn.fetch(sql, query.zip5, query.address_prefix, *patterns)
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
    """Phase 2: property_owner rows via the assessor index
    `(upper(state), upper(city), s5_street_norm(address)) WHERE source_file LIKE
    'property_owner%'`.

    property_owner rows have `zip` and `house_number` 0% populated, so the zip
    index cannot see them at all. City/state comes from the phase-1 rows.

    All three leading columns are index conditions, and the partial predicate
    matches the literal `feed_clause("tax")` emits, so the planner can prove the
    index applies. `imported_at` prunes to the one partition that holds the feed
    (see feeds.py) before the index is even consulted.

    Measured live 2026-08-11: 26 ms, versus 19 s warm / 241 s cold before the
    index existed -- this was the only query class that ever hit our statement
    timeout. The timeout remains the guard; on expiry we report tax as absent
    rather than failing the investigation, and the engine degrades correctly on
    its own — case_quality_and_synthesis flips to run_for_absence and the tax
    packets skip on their field gate.

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
          AND {STREET_NORM}(address) LIKE {STREET_NORM}($3) || '%'
          AND {clause}
        LIMIT {MAX_ROWS_PER_SHAPE}
    """
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                sql, state_upper, city_upper, query.address_prefix, *patterns
            )
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
