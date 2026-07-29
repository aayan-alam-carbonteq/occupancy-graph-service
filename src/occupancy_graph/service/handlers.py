"""Request handlers for the typed surface and the SQL hatch.

Every handler reads its dependencies off request.app.state (`pool`, `cache`),
so the app is constructible with injected fakes and no environment.
"""
from __future__ import annotations

from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse

from occupancy_graph.service import records as records_mod
from occupancy_graph.service.jsonio import jsonable
from occupancy_graph.service.limits import PREFLIGHT_ROWS
from occupancy_graph.service.pagination import Page, page_params, paginate
from occupancy_graph.source.bundle import AddressBundle
from occupancy_graph.source.people import people_for_bundle

# The keys a person carries on the wire. `sources` is a set internally and
# `rows` is the internal row list -- neither may leak in that form.
_PERSON_KEYS = (
    "id", "firstname", "middlename", "lastname", "full_name",
    "norm_name_key", "sources", "primary_address_id",
)


def ok(payload: dict[str, Any]) -> JSONResponse:
    return JSONResponse(jsonable(payload))


def error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


async def healthz(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


def _candidate(bundle: AddressBundle) -> dict[str, Any]:
    return {
        "address_id": bundle.address_id,
        "match_score": 1.0,
        # Phase 1 predicates `zip` for index selection and MATCHES on the
        # address prefix; the discriminating field is the address, so that is
        # what is named. Pinned by Contract B.
        "matched_fields": ["address"],
        "relation_count": bundle.relation_count,
        "norm_address": bundle.norm_address,
        "zip5": bundle.zip5,
        "street_number": bundle.street_number,
        "street_name": bundle.street_name,
        "unit": bundle.unit,
        "city": bundle.city,
        "state": bundle.state,
        "county": bundle.county,
    }


async def resolve_address(request: Request) -> JSONResponse:
    try:
        # json.JSONDecodeError is a subclass of ValueError, so catching
        # ValueError alone covers a malformed body.
        body = await request.json()
    except ValueError:
        return error(400, "request body must be JSON")
    if not isinstance(body, dict) or not str(body.get("address") or "").strip():
        return error(400, "address is required")

    # Two different failure modes, deliberately handled differently. A value we
    # cannot parse is a client bug and is REFUSED by name -- `int("abc")` here
    # used to escape the try above and surface as a 500, which is exactly what
    # the sibling test names. An out-of-range integer is a coherent preference
    # ("as few as possible"), so it is CLAMPED, not refused.
    raw_rows = body.get("rows")
    if raw_rows is None:
        rows = PREFLIGHT_ROWS
    else:
        try:
            rows = int(raw_rows)
        except (TypeError, ValueError):
            return error(400, "rows must be an integer")

    bundle = await request.app.state.cache.resolve(body["address"], body.get("zip"))
    resolved = bundle.relation_count > 0
    page = Page(limit=max(1, rows), offset=0)
    return ok(
        {
            "candidates": [_candidate(bundle)] if resolved else [],
            "address_id": bundle.address_id if resolved else None,
            "source_counts": dict(bundle.source_counts),
            # Only `tax` has a quality gate; the other shapes are structurally
            # ungated, so reporting a constant 0 for them would be noise.
            "dropped_counts": {"tax": bundle.dropped_counts.get("tax", 0)},
            "tax_timed_out": bundle.tax_timed_out,
            "records_by_source": {
                shape: records_mod.records_block(bundle.rows_by_shape.get(shape, []), page)
                for shape in records_mod.ALL_SHAPES
            },
        }
    )


async def address_records(request: Request) -> JSONResponse:
    address_id = int(request.path_params["address_id"])
    bundle = await request.app.state.cache.get(address_id)
    if bundle is None:
        return error(404, f"unknown address_id {address_id}")
    try:
        page = page_params(request.query_params)
    except ValueError as exc:
        return error(400, str(exc))
    shapes, unsupported = records_mod.select_shapes(request.query_params.get("shapes"))
    return ok(
        {
            "records_by_source": {
                shape: records_mod.records_block(bundle.rows_by_shape.get(shape, []), page)
                for shape in shapes
            },
            "unsupported_shapes": unsupported,
        }
    )


def _public_person(person: dict[str, Any]) -> dict[str, Any]:
    out = {key: person.get(key) for key in _PERSON_KEYS}
    out["sources"] = sorted(person.get("sources") or ())
    return out


async def address_people(request: Request) -> JSONResponse:
    address_id = int(request.path_params["address_id"])
    bundle = await request.app.state.cache.get(address_id)
    if bundle is None:
        return error(404, f"unknown address_id {address_id}")
    try:
        page = page_params(request.query_params)
    except ValueError as exc:
        return error(400, str(exc))
    people = [_public_person(person) for person in people_for_bundle(bundle)]
    return ok(paginate(people, page, key="people"))
