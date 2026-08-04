"""Invert manifest.py: shape CSV row -> (partner columns, raw_data).

manifest.py maps partner storage -> shape contract. The clone needs the reverse,
and DERIVES it by reading the manifest rather than restating it, so the two can
never disagree.

  col(X)      -> write the value into partner column X
  raw(K)      -> write the value into raw_data[K]
  derived(fn) -> write NOTHING; the read path computes it
  absent()    -> write NOTHING; declared unavailable in the corpus

`derived` writing nothing is load-bearing, not an optimisation: trace, auto and
base all declare `housenumber` as derived, and production genuinely has
house_number NULL on those feeds. Materialising it would hand the resident-hop
anchor rows production does not have, and the coverage experiment this clone
exists to run would report the hop is fine when live it is not.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from occupancy_graph.source.manifest import SHAPES


def partner_row_for(shape: str, csv_row: Mapping[str, str]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return (columns, raw_data) for one CSV row of `shape`."""
    spec = SHAPES[shape]
    columns: dict[str, Any] = {}
    raw_data: dict[str, Any] = {}
    for field_name, origin in spec.fields.items():
        value = csv_row.get(field_name)
        if value is None or value == "":
            continue
        if origin.kind == "col":
            columns[origin.key] = value
        elif origin.kind == "raw":
            raw_data[origin.key] = value
        # "derived" and "absent" intentionally write nothing.
    return columns, raw_data
