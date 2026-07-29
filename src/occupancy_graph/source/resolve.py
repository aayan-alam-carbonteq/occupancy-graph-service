"""Address resolution against the partner corpus.

Phase 1 uses the `zip` btree plus a prefix filter on the free-text `address`
column. This is THE access path: `house_number` is 0% populated on every feed the
adapter reads, so predicating on it produces a plan that scans.

Measured: 173 ms on records_partitioned (1.4 B rows), 1.30 s warm / 32 s cold on
records_legacy (6.24 B rows).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from occupancy_graph.normalize import normalize_address, zip5
from occupancy_graph.source.feeds import FEEDS, feed_clause
from occupancy_graph.source.pool import PartnerPool

# Shapes reachable by the zip index. `tax` is excluded: property_owner rows have
# a NULL zip, so they need the phase-2 city/state path.
ZIP_SHAPES = ("utility", "trace", "base", "loan", "drive", "auto")

# Per-shape materialization ceiling. Tool calls cap at 100 and preflight at 10,
# so 200 leaves headroom while bounding a dense apartment building.
MAX_ROWS_PER_SHAPE = 200


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
        # Prefix on house number + every token up to (but not including) the
        # last one, which is treated as the street suffix: selective enough to
        # filter a ZIP's worth of rows, loose enough to survive suffix spelling
        # drift ("RD" vs "ROAD"), which the free-text column does not
        # normalize. "1104 Spring Run Road" -> "1104 Spring Run" (drops
        # "Road"). Tokenizing (rather than slicing the raw string) also
        # collapses irregular internal whitespace instead of baking it into
        # the LIKE pattern. With at most one token after the house number, or
        # no leading house number at all, the whole raw string is used
        # unmodified.
        tokens = raw.split()
        if tokens and tokens[0].isdigit():
            house, rest = tokens[0], tokens[1:]
            if len(rest) >= 2:
                rest = rest[:-1]
            prefix = " ".join([house, *rest]) if rest else house
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
    for shape in ZIP_SHAPES:
        for table in FEEDS[shape].tables:
            rows = await _scan_one(pool, table, shape, query)
            result.rows_by_shape[shape].extend(rows)

    for rows in result.rows_by_shape.values():
        for row in rows:
            if result.city is None and row.get("city"):
                result.city = str(row["city"]).strip().upper()
            if result.state is None and row.get("state"):
                result.state = str(row["state"]).strip().upper()
        if result.city and result.state:
            break
    return result


def _decode(row: dict) -> dict:
    value = row.get("raw_data")
    if isinstance(value, str):
        try:
            row["raw_data"] = json.loads(value)
        except ValueError:
            row["raw_data"] = {}
    return row


async def _scan_one(
    pool: PartnerPool, table: str, shape: str, query: AddressQuery
) -> list[dict]:
    clause, patterns = feed_clause(shape, start_index=3)
    sql = f"""
        SELECT *
        FROM public.{table}
        WHERE zip = $1
          AND address ILIKE $2
          AND {clause}
        LIMIT {MAX_ROWS_PER_SHAPE}
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, query.zip5, query.like_prefix, *patterns)
    return [_decode(dict(row)) for row in rows]
