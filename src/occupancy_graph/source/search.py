"""The partner's entity-resolution graph: silver.entity_master + entity_links.

people.py explains why this graph is NOT used for the address view. It is used
here because for name search and for owner-elsewhere traversal the alternative
is nothing at all -- and every row it returns carries identity_confidence and
is_suspicious so the consumer can discount it. The graph is 17.9% suspicious and
never applies its own computed merges.

identity_confidence is MODAL at 40.50 -- 27.5% of rows sit exactly there, with
the rest of the mass spread across the 34-70 band and live rows observed at
70.85. It is NOT a maximum. An earlier revision of this comment said the score
"peaks at 40.50", which would invite a reader to treat 40.50 as a ceiling and
read a row at it as the best confidence the graph can express. It is the most
common value, nothing more.

Measured: entity_links by hal_id 215 ms, by record_id 81 ms (both indexed).
The rows those links point at are fetched by record_id, which the partner's
index set does NOT cover -- see rows_for_links, and
tests/test_live_smoke.py::test_no_index_covers_record_id_on_the_records_tables
for the contradiction in our own specs that only credentials can settle.
"""
from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any, NamedTuple

import asyncpg

from occupancy_graph.source.pool import PartnerPool
from occupancy_graph.source.resolve import decode_raw_data

logger = logging.getLogger(__name__)

HAL_ID_PREFIX = "hal:"


class _PhysicalTable(NamedTuple):
    """relation: the qualified name interpolated into the query. It is a literal
    defined in this module -- never a caller-supplied string -- so the f-string
    below cannot be reached by partner text.

    storage_family: which underlying corpus the relation reads. `record_id` is
    unique only WITHIN a family, so it is the (family, record_id) pair that
    identifies a physical row. `records_new` and the alias `records_partitioned`
    both name the partitioned family; records_legacy is a separate corpus that
    can hold an unrelated row with the same record_id.
    """

    relation: str
    storage_family: str


# entity_links.source_table is partner-supplied text. It is validated against
# this map and NEVER interpolated unchecked -- it reaches a SQL identifier
# position, where a bind parameter cannot be used. What lands in the f-string is
# the mapping's VALUE (a literal defined here), not the caller's string, so even
# a str subclass that games the membership test cannot reach the query text.
PHYSICAL_TABLES = {
    "records_legacy": _PhysicalTable("public.records_legacy", "legacy"),
    "records_new": _PhysicalTable("public.records_new", "partitioned"),
    # `public.records_partitioned` DOES NOT EXIST on the live corpus (verified
    # 2026-08-03). records_new is the partitioned PARENT and the relations named
    # records_partitioned_* are its partitions. Accept the name as an
    # entity_links.source_table value -- the partner may well emit it -- but
    # route it to the real parent rather than to a relation that would raise.
    "records_partitioned": _PhysicalTable("public.records_new", "partitioned"),
}

# Ceiling on links followed per person. 200 rows of one shape is already the
# scan budget (resolve.MAX_ROWS_PER_SHAPE); a person with more links than this
# is an ER failure, not a subject worth fully enumerating.
MAX_LINKS = 200

_ENTITY_COLUMNS = """
    hal_id, canonical_first_name, canonical_last_name, canonical_address_line1,
    canonical_city, canonical_state, canonical_zip, record_count,
    identity_confidence, is_suspicious
"""


def _bpchar(value: str | None) -> str | None:
    """rstrip a bpchar (`char(n)`) column read.

    Postgres pads bpchar with trailing spaces on READ, not just on storage:
    `'HAL0001'::char(15)` comes back as `'HAL0001        '` (padded to the
    declared width). `WHERE hal_id = $1` still matches an unpadded bind
    parameter because bpchar comparison ignores trailing whitespace -- which is
    exactly why this stayed invisible for as long as it did: every WHERE-clause
    lookup kept working while the padding silently leaked into every value this
    module RETURNS (the search-result `id` and every `hal:`-prefixed citation
    handle built from it). It surfaced only once ddl/003_silver.sql replaced the
    fixture's `text`-typed hal_id (which does not pad) with production's real
    char(15), dumped from the live corpus on 2026-08-04 -- 8 tests broke on
    unexpected trailing whitespace the moment the fixture stopped hiding it.

    hal_id and canonical_state are the only bpchar columns this query selects,
    so they are the only ones that need it here. canonical_ssn (char(9)) and
    canonical_phone (char(10)) exist on entity_master but are NOT in
    _ENTITY_COLUMNS -- they never reach a row this function sees, so they
    cannot leak through it today; add the same treatment if a future caller
    starts selecting them.
    """
    return None if value is None else value.rstrip()


def _entity(row: Mapping[str, Any]) -> dict[str, Any]:
    confidence = row["identity_confidence"]
    return {
        "hal_id": _bpchar(row["hal_id"]),
        "canonical_first_name": row["canonical_first_name"],
        "canonical_last_name": row["canonical_last_name"],
        "canonical_address_line1": row["canonical_address_line1"],
        "canonical_city": row["canonical_city"],
        # char(2): always exactly 2 characters for a real US state code, so the
        # padding is invisible in practice -- but rstrip costs nothing and a
        # short/malformed value (a 1-char state, say) would otherwise leak a
        # trailing space same as hal_id did, so it gets the same treatment.
        "canonical_state": _bpchar(row["canonical_state"]),
        "canonical_zip": row["canonical_zip"],
        "record_count": row["record_count"],
        # numeric -> Decimal over the wire; float here so it is JSON-ready and
        # comparable at every call site.
        "identity_confidence": None if confidence is None else float(confidence),
        "is_suspicious": bool(row["is_suspicious"]),
    }


def _name_parts(name: str) -> tuple[str, str]:
    """Split a free-text query into (first, last).

    TWO FORMS, and the comma one is not optional. Assessor rows spell owners
    "LAST, FIRST MIDDLE" -- `CORRELL, REBECCA CHRISTINE` is a verbatim value
    from the corpus -- and following an owner elsewhere feeds exactly those
    strings back in here (prompts.ts: "get their name from tax records, resolve
    them with search_people"). Taking the LAST token as the surname, which is
    right for "JANE DOE", turns "TURNER, BRIAN" into surname BRIAN and searches
    for a person who does not exist. Verified live: "TURNER, BRIAN" and
    "LINVILLE, ROBERT E" both returned nothing while their reversals each
    returned 200 candidates.

    This was invisible until now only because the query it feeds could not
    complete: every call was a 500 long before a wrong name could matter.

    Trailing middle names and initials are dropped -- key_value carries exactly
    one forename, so "GUSTAVE T" must query as "GUSTAVE" or it matches nothing.
    """
    raw = str(name or "").upper()
    if "," in raw:
        # "LAST, FIRST MIDDLE" -- everything before the first comma is the
        # surname, which may itself be multi-token ("VAN DYKE, ANNA").
        last_part, _, first_part = raw.partition(",")
        last = " ".join(last_part.split())
        first_tokens = first_part.split()
        return (first_tokens[0] if first_tokens else ""), last
    tokens = [token for token in raw.split() if token]
    if not tokens:
        return "", ""
    if len(tokens) == 1:
        return "", tokens[0]
    return tokens[0], tokens[-1]


# How many distinct hal_ids the key scan will consider before ranking.
#
# Every candidate costs one cold random read on a 1.2 B-row heap when
# entity_master is probed (~17 ms each, measured), so this is the knob that
# trades ranking breadth against latency: 200 candidates measured 3.4 s of the
# 3.7 s total. It bounds a very common surname; for a rare one the scan ends
# early and the cap never binds.
NAME_KEY_CANDIDATE_LIMIT = 200


def _prefix_upper_bound(prefix: str) -> str:
    """The least string greater than every string starting with `prefix`.

    Increments the final character, which turns a prefix match into a half-open
    RANGE the btree can seek: [prefix, bound). Every prefix built here ends with
    the '|' separator (0x7C), so the bound ends with '}' (0x7D).

    This is exact ONLY because the database is C.UTF-8 (verified:
    `SELECT datcollate FROM pg_database WHERE datname='all_data'`), so
    comparison is bytewise. Under a linguistic collation, punctuation can sort
    unpredictably and the bound would silently include or exclude the wrong
    keys. The split_part() predicates in the query below are the guard against
    exactly that -- they re-check the match on whatever the range returns, so a
    collation change degrades this to "slower", never to "wrong".
    """
    return prefix[:-1] + chr(ord(prefix[-1]) + 1)


async def search_people(
    pool: PartnerPool, name: str, *, limit: int
) -> tuple[int, list[dict[str, Any]]]:
    """Name search. Returns (total_matches_considered, page).

    WHY THIS DOES NOT QUERY entity_master BY NAME. It used to, and against the
    live corpus that query CANNOT COMPLETE: entity_master holds 1.215 B rows and
    carries exactly two indexes -- pk_entity_master(hal_id) and a partial
    idx_entity_master_merged(merged_into_hal_id) WHERE is_merged. Neither covers
    a name, so `upper(canonical_last_name) = $1` planned as a Parallel Seq Scan
    at cost 118,367,194 and died on the 20 s statement timeout every time. This
    surfaced as `GET /v1/people/search` returning HTTP 500 -- 34 of them across
    one 12-address benchmark, each one an evidence path the engine simply lost.

    THE INDEXED PATH. silver.unique_keys_name_dob stores one row per
    (surname, forename, dob) with a UNIQUE btree on key_value and the hal_id
    beside it. key_value is 'LAST|FIRST|YYYY-MM-DD', so a name becomes a prefix,
    and a prefix over a btree is a range scan. Resolve names to hal_ids there,
    then reach entity_master through its PRIMARY KEY, which is the one thing it
    is indexed for. Measured live 2026-08-11: 0.6 s for the key range, 3.4 s for
    200 PK probes, 3.7 s total -- versus a query that never returned.

    COVERAGE, stated honestly: a name_dob key requires a DOB, so a person whose
    records carry none has no key here and will not be found. That is a real
    narrowing versus the old query's intent -- but not versus its behaviour,
    which was to time out and return nothing at all. A future widening could
    union unique_keys_name_house_zip, whose keys are 'LAST|HOUSE|ZIP' and so
    reach DOB-less people; it is not used here because it carries no forename
    and cannot answer an exact (first, last) query.

    `total` is the number of distinct entities found WITHIN
    NAME_KEY_CANDIDATE_LIMIT, not the corpus-wide count the old `count(*) OVER
    ()` promised. That promise was only ever kept by a query that did not
    return; a bounded, honest count beats an exact one that never arrives. When
    the cap binds, the value is a floor and is logged as such.

    `is_merged IS NOT TRUE` is load-bearing: the graph records its computed
    merges but never applies them, so both sides of a merge remain in
    entity_master and a superseded duplicate would otherwise be offered as a
    distinct person.
    """
    first, last = _name_parts(name)
    if not last:
        return 0, []
    prefix = f"{last}|{first}|" if first else f"{last}|"
    sql = f"""
        WITH candidates AS (
            SELECT DISTINCT hal_id
            FROM silver.unique_keys_name_dob
            WHERE key_value >= $1 AND key_value < $2
              AND split_part(key_value, '|', 1) = $3
              AND ($4 = '' OR split_part(key_value, '|', 2) = $4)
              AND hal_id IS NOT NULL
            LIMIT {NAME_KEY_CANDIDATE_LIMIT}
        )
        SELECT {_ENTITY_COLUMNS}, count(*) OVER () AS total_count
        FROM silver.entity_master
        JOIN candidates USING (hal_id)
        WHERE is_merged IS NOT TRUE
        ORDER BY record_count DESC NULLS LAST, hal_id
        LIMIT $5
    """
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                sql, prefix, _prefix_upper_bound(prefix), last, first, int(limit)
            )
    except asyncpg.QueryCanceledError as exc:
        # Degrade to "no people found", never propagate. This mirrors
        # resolve.scan_tax_source: one slow evidence path must not become a 500
        # that the calling heuristic records as a hard error and retries into
        # its data-call budget. The engine already treats an empty people list
        # as absence of evidence rather than failure.
        logger.warning("people search timed out for %r; degrading to empty: %s", name, exc)
        return 0, []
    if not rows:
        return 0, []
    total = int(rows[0]["total_count"])
    if total >= NAME_KEY_CANDIDATE_LIMIT:
        logger.info(
            "people search for %r hit the %d-candidate cap; total is a floor",
            name, NAME_KEY_CANDIDATE_LIMIT,
        )
    score = 1.0 if first else 0.6
    return total, [{**_entity(row), "match_score": score} for row in rows]


async def person_for_hal_id(pool: PartnerPool, hal_id: str) -> dict[str, Any] | None:
    sql = f"SELECT {_ENTITY_COLUMNS} FROM silver.entity_master WHERE hal_id = $1"
    async with pool.acquire() as conn:
        row = await conn.fetchrow(sql, hal_id)
    return None if row is None else _entity(row)


async def records_for_hal_id(
    pool: PartnerPool, hal_id: str, *, limit: int = MAX_LINKS
) -> list[dict[str, Any]]:
    """Every (source_table, record_id) the ER graph attributes to this person.

    Indexed on entity_links(hal_id); measured 215 ms on the live corpus.
    """
    sql = """
        SELECT source_table, record_id, match_type, confidence
        FROM silver.entity_links
        WHERE hal_id = $1
        ORDER BY confidence DESC NULLS LAST, source_table, record_id
        LIMIT $2
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, hal_id, int(min(limit, MAX_LINKS)))
    return [
        {
            "source_table": row["source_table"],
            "record_id": row["record_id"],
            "match_type": row["match_type"],
            "confidence": None if row["confidence"] is None else float(row["confidence"]),
        }
        for row in rows
    ]


async def rows_for_links(
    pool: PartnerPool, links: Sequence[Mapping[str, Any]]
) -> tuple[list[dict[str, Any]], bool]:
    """Fetch the partner rows a set of entity_links points at.

    Returns (rows, timed_out).

    THE SLOW HOP -- but NOT for the reason this docstring gave until 2026-08-03.

    It previously claimed record_id was "the one UNINDEXED hop ... not covered
    by the partner's index set". THAT IS FALSE, and the partner ask it justified
    was for an index that already exists. Catalog-verified on the live corpus:

        records_legacy                 records_pkey, UNIQUE btree (record_id)
        records_partitioned_p20251201  idx_..._record_id btree
        records_partitioned_p20260101  idx_..._record_id btree
        records_partitioned_p20260201  idx_..._record_id btree
        records_partitioned_p20260301  idx_..._record_id btree

    The cost is HEAP I/O, not the index, and it splits the two roots apart --
    in the OPPOSITE direction to what the old text predicted. `EXPLAIN (ANALYZE,
    BUFFERS) SELECT *` over record_ids sampled from real entity_links rows:

        n ids  records_new (partitioned)   records_legacy
            5                    100 ms           1 571 ms
           50                     95 ms          27 012 ms
          200                     90 ms          92 265 ms

    records_new is FLAT at ~90 ms for 200 rows. Failing to prune partitions is
    irrelevant: it visits all five, but each visit is an index seek into a
    relation small enough to stay cached.

    records_legacy is ~460 ms PER ROW and scales linearly, because 3749 GB of
    heap means every row is a random page read that misses cache. Re-running the
    SAME 50 ids proves it is I/O, not planning: 26 865 ms cold (138 pages read)
    -> 878 ms warm (21 pages read), i.e. ~195 ms per cold random page.

    So the degradation path is RIGHT and must stay, but it is a records_legacy
    problem exclusively. With MAX_LINKS = 200, a legacy-heavy person exceeds any
    sane statement_timeout, and cold is the honest number -- arbitrary US
    addresses never arrive warm. The real partner ask is not an index on
    record_id; it is either an address index (so this hop is not needed) or
    storage that does not cost 195 ms a page.

    Degradation is all-or-nothing on purpose: a cancellation on the second
    physical table discards what the first already returned, because a partial
    set carries no marker distinguishing it from a complete one downstream.

    Results are deduplicated on (storage_family, record_id). `records_new` and
    the alias `records_partitioned` name the same family, so entity_links could
    name ONE physical row under two source_table values; returning it twice
    would not raise, it would inflate a per-person record count that feeds a
    downstream score. Measured: production entity_links emits `records_legacy`
    and `records_new` only -- 0 of 200 sampled rows used `records_partitioned`
    -- so this dedup is defensive, not observed. The dedup runs over accumulated
    RESULTS rather than by merging the buckets into one query, so that a future
    divergence between the two names stays visible in the warning below.
    """
    by_table: dict[str, list[int]] = {}
    for link in links:
        table = str(link["source_table"])
        if table not in PHYSICAL_TABLES:
            raise ValueError(
                f"unknown entity_links.source_table {table!r}; "
                f"expected one of {sorted(PHYSICAL_TABLES)}"
            )
        by_table.setdefault(table, []).append(int(link["record_id"]))

    fetched: list[dict[str, Any]] = []
    # (storage_family, record_id) -> the source_table whose copy we kept.
    seen: dict[tuple[str, Any], str] = {}
    for table, record_ids in by_table.items():
        physical = PHYSICAL_TABLES[table]
        sql = f"SELECT * FROM {physical.relation} WHERE record_id = ANY($1::bigint[])"
        try:
            async with pool.acquire() as conn:
                rows = await conn.fetch(sql, record_ids)
        except asyncpg.QueryCanceledError as exc:
            logger.warning("entity row fetch cancelled on %s: %s", table, exc)
            return [], True
        for row in rows:
            decoded = decode_raw_data(dict(row))
            key = (physical.storage_family, decoded["record_id"])
            kept_from = seen.get(key)
            if kept_from is not None:
                # Not merely noise-suppression: this firing in production is the
                # only way we learn the partner emits both link kinds for one
                # row, which nothing in the corpus currently tells us.
                logger.warning(
                    "entity_links names record_id %s under both %r and %r; these "
                    "resolve to the same physical row (storage family %r) and the "
                    "duplicate is dropped",
                    decoded["record_id"], kept_from, table, physical.storage_family,
                )
                continue
            seen[key] = table
            fetched.append(decoded)
    return fetched, False
