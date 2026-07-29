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
