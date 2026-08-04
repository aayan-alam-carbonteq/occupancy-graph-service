"""Which physical feed each shape's rows are written as.

source_file strings are REAL production directory names (verified against the
live corpus 2026-08-03/04), because feeds.py selects on them with LIKE. The
imported_at date routes a row into the production partition that holds that feed.

There is deliberately NO drive plan: production has no drive feed. drive rows are
payday rows carrying dl_number, folded into loan by the loader.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FeedPlan:
    shape: str
    table: str                # "records_legacy" | "records_new"
    source_file: str
    imported_at: str | None   # None for records_legacy (not partitioned)
    weight: float = 1.0       # share of the shape's rows routed to this plan


FEED_PLANS: tuple[FeedPlan, ...] = (
    FeedPlan("utility", "records_legacy",
             "Export Utility Stripped Down/Utility_ky/Utility_ky.csv", None),
    FeedPlan("trace", "records_legacy",
             "Trace Skipping Oct 2025/2025_Historical_database_1/2025_Historical_database_1.csv", None),
    # base spans both roots in production. feed_id_coverage proportions are
    # ~1:7 toward records_new (USCRM + CoReg 18.6k sampled vs 2019.2_LF 130.6k).
    FeedPlan("base", "records_legacy", "2026.1-USCRM/uscrm_ky.csv", None, weight=0.125),
    FeedPlan("base", "records_new", "2019.2_USA_Consumer_LF/lf_ky.csv", "2026-01-15", weight=0.875),
    FeedPlan("loan", "records_new", "Payday_Big_2026/payday_ky.csv", "2026-02-15"),
    FeedPlan("auto", "records_new", "auto-verified/auto_ky.csv", "2026-03-15"),
    # MUST be inside [2026-03-01, 2026-04-01): feeds.py prunes the tax scan to
    # that partition, so a row outside it is silently unreachable.
    FeedPlan("tax", "records_new", "property_owner_49/property_owner_ky.csv", "2026-03-15"),
)


def plan_for(shape: str) -> tuple[FeedPlan, ...]:
    return tuple(p for p in FEED_PLANS if p.shape == shape)
