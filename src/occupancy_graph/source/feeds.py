"""Which physical table and which `source_file` patterns back each shape.

`source_file` is NOT indexed in the partner DB. These clauses are only ever
applied as heap filters on top of an index-qualified predicate (`zip`, or
`upper(state)` + `upper(city)`), never as the driving condition.

PHYSICAL TABLE NAMES, verified against the live corpus on 2026-08-03. There are
exactly two roots:

  records_legacy   a plain table, 3749 GB.
  records_new      the PARTITIONED PARENT (relkind 'p'), RANGE (imported_at),
                   whose five partitions are named records_partitioned_p20251201,
                   _p20260101, _p20260201, _p20260301, records_partitioned_default.

`public.records_partitioned` DOES NOT EXIST. This module previously named it as
the table for base/loan/drive/auto/tax -- five of the seven shapes -- so every
one of them would raise `relation "public.records_partitioned" does not exist`
against production. The local fixture had the relationship inverted (it built
records_partitioned as the parent and records_new as a view over it), so the
entire suite validated a topology that does not exist. Query the parent and let
Postgres route to partitions; do not reintroduce the name.
"""
from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from occupancy_graph.source.manifest import SHAPES


# The partition boundaries of records_partitioned_p20260301, the ONLY partition
# holding property_owner rows. Literals, not caller input -- they are only ever
# interpolated into the tax feed clause below.
TAX_PARTITION_START = "2026-03-01"
TAX_PARTITION_END = "2026-04-01"


@dataclass(frozen=True)
class FeedSpec:
    shape: str
    tables: tuple[str, ...]
    patterns: tuple[str, ...]
    extra_sql: str = ""


FEEDS: dict[str, FeedSpec] = {
    "utility": FeedSpec("utility", ("records_legacy",),
                        ("Export Utility Stripped Down/%",)),
    "trace": FeedSpec("trace", ("records_legacy",),
                      ("Trace Skipping Oct 2025/%",)),
    "base": FeedSpec("base", ("records_legacy", "records_new"),
                     ("2026.1-USCRM/%", "2019.2_USA_Consumer_LF%", "%CoReg%")),
    "loan": FeedSpec("loan", ("records_new",),
                     ("Payday_Big_%", "PD loan_master/%", "24mm _july-2025-loan-txt%")),
    # Same physical rows as `loan`: there is no DMV feed, only a licence number
    # carried on payday-loan rows.
    "drive": FeedSpec("drive", ("records_new",),
                      ("Payday_Big_%", "PD loan_master/%", "24mm _july-2025-loan-txt%"),
                      extra_sql=" AND dl_number IS NOT NULL"),
    # NOTE: the plan's spec text (2026-07-28-postgres-graph-adapter.md lines 1956-2084)
    # lists a 4th pattern here, "auto_%", but its own test_multi_pattern_feeds_are_ored
    # asserts len(params) == 3 for this shape -- the spec is internally inconsistent.
    # "auto_%" is dropped rather than any other entry because `_` is a single-char
    # LIKE wildcard: "auto_%" already matches everything "auto-%" matches (the dash
    # satisfies `_`) plus any other single character in that position, making it both
    # redundant with "auto-%" for known feeds and the riskiest/most over-broad pattern
    # of the four against a 7.6B-row table. See tests/test_feeds.py for the count this
    # reconciles with.
    "auto": FeedSpec("auto", ("records_new",),
                     ("AvengerAuto%", "auto-%", "Auto Jan-Dec%")),
    # PARTITION PRUNING, measured 2026-08-03: the same tax predicate costs 241 s
    # cold against the parent (all five partitions) and 165 s against
    # records_partitioned_p20260301 alone. `source_file` is unindexed, so it
    # cannot prune; `imported_at` IS the partition key and can.
    #
    # This is the mechanism schema_doc.py already documents -- "records_new's
    # partitions are separate DATASETS, not time slices ... constraining
    # imported_at picks a feed, not a date range". property_owner is loaded
    # entirely into p20260301.
    #
    # THE RISK, stated plainly: if the partner loads property_owner into a LATER
    # partition, this bound silently drops those rows -- a wrong answer, not an
    # error. tests/test_live_smoke.py asserts the assumption against the live
    # corpus so it fails loudly instead. Re-check whenever a new partition
    # appears.
    "tax": FeedSpec("tax", ("records_new",),
                    ("property_owner%",),
                    extra_sql=f" AND imported_at >= '{TAX_PARTITION_START}'"
                              f" AND imported_at < '{TAX_PARTITION_END}'"),
}


def feed_clause(shape: str, start_index: int) -> tuple[str, list[str]]:
    """SQL fragment + params selecting one shape's rows.

    `start_index` is the first free asyncpg positional parameter number.
    """
    spec = FEEDS[shape]
    placeholders = [f"source_file LIKE ${start_index + i}" for i in range(len(spec.patterns))]
    clause = "(" + " OR ".join(placeholders) + ")" + spec.extra_sql
    return clause, list(spec.patterns)


def pattern_groups(shapes: Sequence[str], table: str) -> list[tuple[str, tuple[str, ...]]]:
    """The DISTINCT (label, patterns) groups to scan for one table.

    Collapsing the per-shape scans into one pass per table needs to know which
    shapes actually live on that table, and which of them are the same rows.

    Shapes with an IDENTICAL pattern tuple are one group: `drive` is the very
    same physical payday row as `loan`, separated only by `dl_number IS NOT
    NULL`, which shapes_for_row applies in Python. Scanning them as two groups
    would read the same heap pages twice to return the same rows -- the exact
    waste this collapse exists to remove. The FIRST shape in `shapes` order
    wins the label; the caller re-derives the rest with shapes_for_row.

    Returned in `shapes` order so the emitted SQL is deterministic, which is
    what lets the tests pin the parameter positions.
    """
    groups: list[tuple[str, tuple[str, ...]]] = []
    seen: set[tuple[str, ...]] = set()
    for shape in shapes:
        spec = FEEDS[shape]
        if table not in spec.tables or spec.patterns in seen:
            continue
        seen.add(spec.patterns)
        groups.append((shape, spec.patterns))
    return groups


def _like_to_regex(pattern: str) -> re.Pattern[str]:
    """Compile one SQL LIKE pattern. `%` is any run, `_` is exactly one char;
    every other character is literal, so regex metacharacters in feed names
    ("2026.1-USCRM/%") are escaped rather than interpreted."""
    parts = []
    for char in pattern:
        if char == "%":
            parts.append(".*")
        elif char == "_":
            parts.append(".")
        else:
            parts.append(re.escape(char))
    return re.compile("^" + "".join(parts) + "$")


# Reverse lookup table, in manifest order -- NOT FEEDS order, which differs
# (utility, trace, base, loan, drive, auto, tax) and would emit ("loan", "drive").
# Keying off SHAPES rather than restating the order means a shape added to the
# manifest but not to FEEDS raises KeyError HERE, at import, instead of being
# silently dropped from every reverse lookup. test_manifest_and_feeds_cover_
# exactly_the_same_shapes pins the two key sets together for the other direction.
#
# LIKE is case-sensitive (feed_clause emits LIKE, not ILIKE), so no re.I here.
_REVERSE_FEEDS: tuple[tuple[str, tuple[re.Pattern[str], ...]], ...] = tuple(
    (shape, tuple(_like_to_regex(pattern) for pattern in FEEDS[shape].patterns))
    for shape in SHAPES
)


def shapes_for_row(row: Mapping[str, Any]) -> tuple[str, ...]:
    """Every shape a fetched partner row belongs to, in manifest order.

    The forward scan knows the shape because it chose the predicate. Rows
    reached by record_id (the `hal:` traversal) do not, so the source_file
    predicates run backwards here.

    A row can be TWO shapes: `drive` is the same physical payday row as `loan`,
    distinguished only by `dl_number IS NOT NULL` -- FeedSpec.extra_sql in the
    forward direction, an explicit check here.

    CALLER CONTRACT: `dl_number` must be in the projection. A row fetched with a
    column list that omits it is indistinguishable from one where the licence is
    NULL, and will come back `loan` without `drive`. Mapping.get cannot tell
    "not selected" from "selected and null", so this is a constraint on the
    SELECT, not something checkable here.
    """
    source_file = row.get("source_file")
    if not source_file:
        return ()
    text = str(source_file)
    matched = []
    for shape, patterns in _REVERSE_FEEDS:
        if not any(pattern.match(text) for pattern in patterns):
            continue
        if shape == "drive" and row.get("dl_number") in (None, ""):
            continue
        matched.append(shape)
    return tuple(matched)
