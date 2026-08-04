"""Down-sample columns to production's per-feed population rate.

Hash-based, never RNG: the same row must make the same decision on every run, or
the clone is not reproducible and no measurement taken against it is comparable
to the last one.

Targets are real feed_id_coverage percentages read from the live corpus
2026-08-04. They matter because the engine's packet_gates gate six of seven
evidence packets on FIELD PRESENCE -- a clone with phone at 90% where production
has 41.9% runs packets production would skip.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path

PROFILE = Path(__file__).parents[1] / "profiles" / "feed_population.json"


def load_targets() -> Mapping[str, Mapping[str, float]]:
    return json.loads(PROFILE.read_text())


def keep_value(shape: str, column: str, row_key: str,
               targets: Mapping[str, Mapping[str, float]]) -> bool:
    """True if this row should carry `column`.

    A column with no recorded target is kept unchanged -- absence of a target
    means "we never measured this", not "production has none".
    """
    pct = targets.get(shape, {}).get(column)
    if pct is None:
        return True
    if pct <= 0.0:
        return False
    if pct >= 100.0:
        return True
    digest = hashlib.blake2b(f"{shape}|{column}|{row_key}".encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big") % 10_000 < int(pct * 100)
