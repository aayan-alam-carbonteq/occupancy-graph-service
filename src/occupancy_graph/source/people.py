"""Person entities synthesized from a bundle's own rows.

This is the old SQLite `person_entities` semantics generalized past `base`:
cluster the address's rows on the normalized name key, one person per cluster.

Deliberately NOT the partner's hal_id graph. That graph is 17.9% is_suspicious,
45% singletons, and never applies its computed merges — a live lookup returned
one person as three entities and another as two. Using it for the address view
would leak those failures into every investigation. It is used only by
search.py, where the alternative is nothing at all.
"""
from __future__ import annotations

from typing import Any

PERSON_ID_PREFIX = "addr:"

# Shapes carrying a person name, in the order they should win the display name.
_NAME_SHAPES = ("base", "trace", "loan", "drive", "auto", "utility", "tax")


def _names(row: dict[str, Any]) -> tuple[str, str, str]:
    first = row.get("firstname") or row.get("first_name") or ""
    middle = row.get("middlename") or row.get("middle_name") or ""
    last = row.get("lastname") or row.get("last_name") or ""
    return str(first), str(middle), str(last)


def people_for_bundle(bundle) -> list[dict[str, Any]]:
    """Cluster the bundle's rows into people. Deterministic ordering."""
    clusters: dict[str, dict[str, Any]] = {}
    for shape in _NAME_SHAPES:
        for row in bundle.rows_by_shape.get(shape, []):
            key = row.get("__norm_name_key") or ""
            if not key or key == "|":
                continue
            first, middle, last = _names(row)
            person = clusters.get(key)
            if person is None:
                person = {
                    "norm_name_key": key,
                    "firstname": first or None,
                    "middlename": middle or None,
                    "lastname": last or None,
                    "full_name": " ".join(p for p in (first, middle, last) if p) or None,
                    "sources": set(),
                    "rows": [],
                    "primary_address_id": bundle.address_id,
                }
                clusters[key] = person
            person["sources"].add(shape)
            person["rows"].append((shape, row))

    people = []
    for index, key in enumerate(sorted(clusters)):
        person = clusters[key]
        person["id"] = f"{PERSON_ID_PREFIX}{bundle.address_id}:{index}"
        people.append(person)
    return people
