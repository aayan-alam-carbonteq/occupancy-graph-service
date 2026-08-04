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


def load_targets() -> Mapping[str, Mapping[str, float | None]]:
    """Shape -> column -> target %, or None where the column is not
    representable from our CSVs at all (see the profile's own `_comment`).

    Underscore-prefixed keys are documentation, not shapes, and are stripped
    here so no consumer iterating the mapping mistakes `_comment` for a feed.
    """
    raw = json.loads(PROFILE.read_text())
    return {key: value for key, value in raw.items() if not key.startswith("_")}


def keep_value(shape: str, column: str, row_key: str,
               targets: Mapping[str, Mapping[str, float]],
               source_rate: float | None = None) -> bool:
    """True if this row should carry `column`.

    A column with no recorded target -- absent, or explicitly null -- is kept
    unchanged. Absent means "we never measured this"; null means "our CSVs carry
    no such field, so production's rate is unreachable by construction" (see the
    _comment in feed_population.json). Neither is a licence to invent data.

    SOURCE_RATE IS WHAT MAKES THE TARGET A TARGET RATHER THAN A DISCOUNT.
    This filter can only ever REMOVE values, so applying a target of 41.9% to a
    source that is itself only 49% populated yields 0.49 * 0.419 = 20.5%, not
    41.9%. Measured on the first full load, that multiplication was visible in
    every partially-populated column:

        utility.phone   source ~49%  target 41.9%  ->  landed 20.5%
        trace.phone     source ~65%  target 76.0%  ->  landed 49.5%
        base.dob        source ~66%  target 86.0%  ->  landed 56.4%

    Given the source rate, the keep probability becomes target/source, so the
    FINAL population lands on the target. When the source is already at or below
    the target the value is always kept -- the target is simply unreachable from
    this data, and silently under-shooting it would be worse than falling short
    loudly (the caller logs the shortfall).

    UNITS, because getting this wrong the other way is exactly as broken: `pct`
    here is already a PERCENT (0..100) and `source_rate` is a FRACTION (0..1),
    so `pct / source_rate` alone converts straight back to a percent --
    target=41.9, source=0.49 -> 41.9/0.49 = 85.5, i.e. keep 85.5% of the
    already-present rows so 0.49 * 0.855 lands back on 41.9. Multiplying that by
    a further 100 (an earlier version of this function did) blows every
    realistic ratio past 100 and the `min(100.0, ...)` clamp then silently
    keeps EVERYTHING no matter the source rate -- which is how trace.phone's
    82.4%-populated source sailed straight through undropped instead of landing
    on its 76.0% target.

    Population is behaviour, not decoration: packet_gates.ts gates six of seven
    evidence packets on FIELD PRESENCE, so a column at half its production rate
    silently changes which packets run.
    """
    pct = targets.get(shape, {}).get(column)
    if pct is None:
        return True
    if pct <= 0.0:
        return False
    if source_rate is not None and source_rate > 0.0:
        # pct (percent) / source_rate (fraction) IS the effective keep-percent
        # -- see the UNITS note above. Do not multiply by 100 again.
        pct = min(100.0, pct / source_rate)
    if pct >= 100.0:
        return True
    digest = hashlib.blake2b(f"{shape}|{column}|{row_key}".encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big") % 10_000 < int(pct * 100)
