"""Shape selection, paged record blocks, and the provenance summary line."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from occupancy_graph.service.pagination import Page
from occupancy_graph.source.manifest import SHAPES

ALL_SHAPES = tuple(SHAPES)

# Curated per-shape summary field order for GET /v1/source-record. The manifest
# order leads with ids and address parts; for a tax row the owner identity is
# the point, so it leads. Contract B pins "tax; ownername=DOE, JANE ANN; ...".
SUMMARY_FIELDS: dict[str, tuple[str, ...]] = {
    "tax": ("ownername", "address", "city", "state", "ownercity", "ownerstate"),
    "base": ("firstname", "lastname", "primaryaddress", "city", "state", "zip"),
    "utility": ("first_name", "last_name", "address", "city", "state", "zip"),
    "trace": ("firstname", "lastname", "address", "city", "state", "zip"),
    "loan": ("firstname", "lastname", "address", "zip", "own_rent", "employer"),
    "drive": ("firstname", "lastname", "address", "zip", "dl_num", "dl_state"),
    "auto": ("firstname", "lastname", "address", "zip", "make", "model"),
}


def select_shapes(raw: str | None) -> tuple[tuple[str, ...], list[str]]:
    """Parse a `shapes=a,b` parameter into (requested, unsupported).

    Absent means every shape. An unknown name is REPORTED, not ignored: the
    engine must be able to tell "no rows of that kind" from "that kind does not
    exist here" (voter/criminal/linkedin are gone from this corpus).
    """
    if raw is None or raw.strip() == "":
        return ALL_SHAPES, []
    requested, unsupported = [], []
    for token in raw.split(","):
        name = token.strip()
        if not name:
            continue
        (requested if name in SHAPES else unsupported).append(name)
    return tuple(dict.fromkeys(requested)), unsupported


def records_block(
    rows: Sequence[Mapping[str, Any]], page: Page, *, with_rowid: bool = True
) -> dict[str, Any]:
    """One shape's paged rows. `__rowid` is the row's index within the bundle's
    full list for that shape -- the handle GET /v1/source-record takes. Rows
    reached through entity_links have no bundle position, so they carry none.

    The {total_count, has_more, records} arithmetic is deliberately identical to
    service.pagination.paginate; this is that block plus the `__rowid` stamp.
    """
    total = len(rows)
    window = list(rows[page.offset : page.offset + page.limit])
    records = [
        ({**row, "__rowid": page.offset + index} if with_rowid else dict(row))
        for index, row in enumerate(window)
    ]
    return {"total_count": total, "has_more": page.offset + len(records) < total, "records": records}


def summarize(shape: str, row: Mapping[str, Any]) -> str:
    """One human-readable provenance line, e.g.
    "tax; ownername=DOE, JANE ANN; address=123 MAIN ST; ...". Empty fields are
    skipped so the line never advertises absence as a value."""
    parts = [
        f"{field}={row[field]}"
        for field in SUMMARY_FIELDS.get(shape, ())
        if row.get(field)
    ]
    return "; ".join([shape, *parts])
