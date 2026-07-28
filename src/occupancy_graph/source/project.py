"""Project one partner row into one contract-shaped dict.

Output keys are exactly the shape's contract columns plus the `__norm_*` helpers.
Every contract field is present; unavailable ones are None rather than missing,
so consumers can distinguish "no value" from "field does not exist".

Every value is stringified: every record field in the GraphQL contract is
`String`, and the old SQLite path stored everything as TEXT.
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import Any

from occupancy_graph.normalize import (
    name_key,
    normalize_address,
    normalize_phone,
    normalize_text,
    zip5,
)
from occupancy_graph.source import quality
from occupancy_graph.source.manifest import ShapeSpec

_PHONE_NORM = {"phone", "mobile", "cellphone"}


def _stringify(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    return text or None


def _raw(row: Mapping[str, Any]) -> Mapping[str, Any]:
    value = row.get("raw_data")
    return value if isinstance(value, Mapping) else {}


def project_row(shape: ShapeSpec, row: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    raw = _raw(row)
    for name, origin in shape.fields.items():
        if origin.kind == "absent":
            out[name] = None
            continue
        if origin.kind == "col":
            value = row.get(origin.key)
        elif origin.kind == "raw":
            value = quality.coerce_trace_field(origin.key, raw.get(origin.key)) \
                if shape.name == "trace" else raw.get(origin.key)
        else:
            value = origin.fn(row)
        out[name] = _stringify(value)

    if "own_rent" in out:
        out["own_rent"] = quality.normalize_own_rent(out["own_rent"])

    out.update(_norm_helpers(shape, out))
    return out


def _norm_helpers(shape: ShapeSpec, projected: Mapping[str, Any]) -> dict[str, str]:
    first = projected.get("firstname") or projected.get("first_name") or ""
    last = projected.get("lastname") or projected.get("last_name") or ""
    address = (
        projected.get("primaryaddress")
        or projected.get("address")
        or projected.get("street")
        or ""
    )
    zip_code = zip5(projected.get("zip") or projected.get("ownerzipcode"))
    helpers: dict[str, str] = {
        "__norm_firstname": normalize_text(first),
        "__norm_lastname": normalize_text(last),
        "__norm_name_key": name_key(first, last),
        "__norm_address": normalize_address(address),
        "__norm_address_zip_key": f"{normalize_address(address)}|{zip_code}",
    }
    for column in shape.norm_fields:
        key = f"__norm_{column}"
        if key in helpers:
            continue
        value = projected.get(column) or ""
        if column in _PHONE_NORM:
            helpers[key] = normalize_phone(value)
        elif column.startswith("email"):
            helpers[key] = normalize_text(value)
        else:
            helpers[key] = normalize_address(value)
    return helpers
