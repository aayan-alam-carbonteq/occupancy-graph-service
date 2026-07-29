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
from occupancy_graph.source.people import PERSON_ID_PREFIX, people_for_bundle
from occupancy_graph.source.search import HAL_ID_PREFIX

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


def _parse_addr_person_id(person_id: str) -> tuple[int, int] | None:
    """`addr:<addressId>:<n>` -> (address_id, index), or None if malformed."""
    parts = person_id.split(":")
    if len(parts) != 3:
        return None
    try:
        return int(parts[1]), int(parts[2])
    except ValueError:
        return None


def _malformed_person_id(person_id: str) -> JSONResponse:
    return error(400, f"malformed person id {person_id!r}; expected addr:<n>:<n> or hal:<id>")


async def person_records(request: Request) -> JSONResponse:
    person_id = str(request.path_params["person_id"])
    try:
        page = page_params(request.query_params)
    except ValueError as exc:
        return error(400, str(exc))
    shapes, unsupported = records_mod.select_shapes(request.query_params.get("shapes"))

    if person_id.startswith(PERSON_ID_PREFIX):
        return await _addr_person_records(request, person_id, page, shapes, unsupported)
    if person_id.startswith(HAL_ID_PREFIX):
        return error(501, "hal: traversal not implemented")   # replaced in Task 13
    return _malformed_person_id(person_id)


async def _addr_person_records(
    request: Request,
    person_id: str,
    page: Page,
    shapes: tuple[str, ...],
    unsupported: list[str],
) -> JSONResponse:
    parsed = _parse_addr_person_id(person_id)
    if parsed is None:
        return _malformed_person_id(person_id)
    address_id, index = parsed

    bundle = await request.app.state.cache.get(address_id)
    if bundle is None:
        return error(404, f"unknown address_id {address_id}")
    people = people_for_bundle(bundle)
    if not 0 <= index < len(people):
        return error(404, f"unknown person id {person_id}")
    person = people[index]

    # `rows` on a clustered person is [(shape, row)]. The row objects are the
    # SAME dicts as bundle.rows_by_shape[shape] -- people_for_bundle iterates
    # those already-projected lists and stores the references -- so an IDENTITY
    # lookup recovers the bundle position that GET /v1/source-record takes.
    # Identity, not equality: two identical rows at one address stay
    # distinguishable. The position map is built once per shape rather than
    # rescanned per row, which a linear `index()`-style search would make
    # quadratic (a 200-row shape = 40 000 comparisons).
    by_shape: dict[str, list[dict[str, Any]]] = {shape: [] for shape in shapes}
    for shape, row in person["rows"]:
        if shape in by_shape:
            by_shape[shape].append(row)

    blocks = {}
    for shape in shapes:
        # id() is safe as a key here: `full` holds a strong reference to every
        # row for the whole lifetime of this map, so no id can be recycled.
        positions = {id(row): i for i, row in enumerate(bundle.rows_by_shape.get(shape, []))}
        rows = [{**row, "__rowid": positions.get(id(row))} for row in by_shape[shape]]
        blocks[shape] = records_mod.records_block(rows, page, with_rowid=False)

    return ok(
        {
            "person": {
                "id": person_id,
                "firstname": person.get("firstname"),
                "lastname": person.get("lastname"),
                "identity_confidence": None,
                "is_suspicious": None,
            },
            "records_by_source": blocks,
            "records_timed_out": False,
            "unsupported_shapes": unsupported,
        }
    )
