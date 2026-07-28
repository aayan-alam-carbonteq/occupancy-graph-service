"""Which physical table and which `source_file` patterns back each shape.

`source_file` is NOT indexed in the partner DB. These clauses are only ever
applied as heap filters on top of an index-qualified predicate (`zip`, or
`upper(state)` + `upper(city)`), never as the driving condition.
"""
from __future__ import annotations

from dataclasses import dataclass


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
    "base": FeedSpec("base", ("records_legacy", "records_partitioned"),
                     ("2026.1-USCRM/%", "2019.2_USA_Consumer_LF%", "%CoReg%")),
    "loan": FeedSpec("loan", ("records_partitioned",),
                     ("Payday_Big_%", "PD loan_master/%", "24mm _july-2025-loan-txt%")),
    # Same physical rows as `loan`: there is no DMV feed, only a licence number
    # carried on payday-loan rows.
    "drive": FeedSpec("drive", ("records_partitioned",),
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
    "auto": FeedSpec("auto", ("records_partitioned",),
                     ("AvengerAuto%", "auto-%", "Auto Jan-Dec%")),
    "tax": FeedSpec("tax", ("records_partitioned",),
                    ("property_owner%",)),
}


def feed_clause(shape: str, start_index: int) -> tuple[str, list[str]]:
    """SQL fragment + params selecting one shape's rows.

    `start_index` is the first free asyncpg positional parameter number.
    """
    spec = FEEDS[shape]
    placeholders = [f"source_file LIKE ${start_index + i}" for i in range(len(spec.patterns))]
    clause = "(" + " OR ".join(placeholders) + ")" + spec.extra_sql
    return clause, list(spec.patterns)
