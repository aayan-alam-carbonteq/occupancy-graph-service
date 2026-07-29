"""The partner's entity-resolution graph: silver.entity_master + entity_links.

people.py explains why this graph is NOT used for the address view. It is used
here because for name search and for owner-elsewhere traversal the alternative
is nothing at all -- and every row it returns carries identity_confidence and
is_suspicious so the consumer can discount it. The graph is 17.9% suspicious,
peaks at confidence 40.50, and never applies its own computed merges.

Measured: entity_links by hal_id 215 ms, by record_id 81 ms (both indexed).
The rows those links point at are fetched by record_id, which the partner's
index set does NOT cover -- see rows_for_links.
"""
from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any

import asyncpg

from occupancy_graph.source.pool import PartnerPool
from occupancy_graph.source.resolve import decode_raw_data

logger = logging.getLogger(__name__)

HAL_ID_PREFIX = "hal:"

# entity_links.source_table is partner-supplied text. It is validated against
# this map and NEVER interpolated unchecked -- it reaches a SQL identifier
# position, where a bind parameter cannot be used. Note that what lands in the
# f-string is the mapping's VALUE (a literal defined here), not the caller's
# string, so even a str subclass that games the membership test cannot reach
# the query text.
PHYSICAL_TABLES = {
    "records_legacy": "public.records_legacy",
    "records_new": "public.records_new",
    "records_partitioned": "public.records_partitioned",
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


def _entity(row: Mapping[str, Any]) -> dict[str, Any]:
    confidence = row["identity_confidence"]
    return {
        "hal_id": row["hal_id"],
        "canonical_first_name": row["canonical_first_name"],
        "canonical_last_name": row["canonical_last_name"],
        "canonical_address_line1": row["canonical_address_line1"],
        "canonical_city": row["canonical_city"],
        "canonical_state": row["canonical_state"],
        "canonical_zip": row["canonical_zip"],
        "record_count": row["record_count"],
        # numeric -> Decimal over the wire; float here so it is JSON-ready and
        # comparable at every call site.
        "identity_confidence": None if confidence is None else float(confidence),
        "is_suspicious": bool(row["is_suspicious"]),
    }


def _name_parts(name: str) -> tuple[str, str]:
    """Split a free-text query into (first, last). The last token is the
    surname -- the only field entity_master indexes usefully and the only one
    100% populated. A single token is treated as a surname."""
    tokens = [token for token in str(name or "").upper().split() if token]
    if not tokens:
        return "", ""
    if len(tokens) == 1:
        return "", tokens[0]
    return tokens[0], tokens[-1]


async def search_people(
    pool: PartnerPool, name: str, *, limit: int
) -> tuple[int, list[dict[str, Any]]]:
    """Name search over entity_master. Returns (total_matches, page).

    count(*) OVER () is evaluated before LIMIT, so total is the true match
    count in one round trip rather than a second query or a lie.

    `is_merged IS NOT TRUE` is load-bearing: the graph records its computed
    merges but never applies them, so both sides of a merge remain in
    entity_master and a superseded duplicate would otherwise be offered as a
    distinct person.
    """
    first, last = _name_parts(name)
    if not last:
        return 0, []
    sql = f"""
        SELECT {_ENTITY_COLUMNS}, count(*) OVER () AS total_count
        FROM silver.entity_master
        WHERE upper(canonical_last_name) = $1
          AND ($2 = '' OR upper(canonical_first_name) = $2)
          AND is_merged IS NOT TRUE
        ORDER BY record_count DESC NULLS LAST, hal_id
        LIMIT $3
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, last, first, int(limit))
    if not rows:
        return 0, []
    score = 1.0 if first else 0.6
    return int(rows[0]["total_count"]), [{**_entity(row), "match_score": score} for row in rows]


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

    Returns (rows, timed_out). THIS IS THE ONE UNINDEXED HOP in the typed
    surface: entity_links is indexed both ways, but `record_id` on
    records_legacy / records_partitioned is not covered by the partner's index
    set, and records_partitioned cannot prune partitions on it. It therefore
    runs under the pool's statement_timeout and DEGRADES rather than raising --
    the caller reports records_timed_out=true so an empty result is never
    mistaken for "this person has no records". An index on records_*(record_id)
    is on the partner ask list.

    Degradation is all-or-nothing on purpose: a cancellation on the second
    physical table discards what the first already returned, because a partial
    set carries no marker distinguishing it from a complete one downstream.
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
    for table, record_ids in by_table.items():
        sql = f"SELECT * FROM {PHYSICAL_TABLES[table]} WHERE record_id = ANY($1::bigint[])"
        try:
            async with pool.acquire() as conn:
                rows = await conn.fetch(sql, record_ids)
        except asyncpg.QueryCanceledError as exc:
            logger.warning("entity row fetch cancelled on %s: %s", table, exc)
            return [], True
        fetched.extend(decode_raw_data(dict(row)) for row in rows)
    return fetched, False
