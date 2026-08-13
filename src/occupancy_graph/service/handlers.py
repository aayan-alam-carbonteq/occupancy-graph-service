"""Request handlers for the typed surface and the SQL hatch.

Every handler reads its dependencies off request.app.state (`pool`, `cache`),
so the app is constructible with injected fakes and no environment.
"""
from __future__ import annotations

from typing import Any

from occupancy_graph.service import records as records_mod
from occupancy_graph.service import sql_hatch
from occupancy_graph.service.jsonio import jsonable
from occupancy_graph.service.limits import DEFAULT_PAGE_LIMIT, PREFLIGHT_ROWS
from occupancy_graph.service.pagination import Page, page_params, paginate
from occupancy_graph.service.schema_doc import schema_document
from occupancy_graph.service.sql_guard import SqlRefused
from occupancy_graph.source import quality, search
from occupancy_graph.source.bundle import AddressBundle
from occupancy_graph.source.feeds import shapes_for_row
from occupancy_graph.source.manifest import SHAPES
from occupancy_graph.source.people import PERSON_ID_PREFIX, people_for_bundle
from occupancy_graph.source.project import project_row
from occupancy_graph.source.search import HAL_ID_PREFIX
from starlette.requests import Request
from starlette.responses import JSONResponse

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


async def describe_schema(request: Request) -> JSONResponse:
    """The curated corpus description. Touches no database: it is a constant
    document plus the CURRENT limits, so it answers while the pool is busy and
    reflects an env-var change without a restart."""
    return ok(schema_document())


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
        return await _hal_person_records(request, person_id, page, shapes, unsupported)
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


def _by_record_id(row: dict[str, Any]) -> tuple[bool, Any]:
    """Sort key mirroring bundle._stable_order: a NULL record_id sorts LAST
    rather than raising on a None/int comparison."""
    return (row.get("record_id") is None, row.get("record_id"))


async def _hal_person_records(
    request: Request,
    person_id: str,
    page: Page,
    shapes: tuple[str, ...],
    unsupported: list[str],
) -> JSONResponse:
    """Owner-elsewhere traversal: entity_links -> partner rows -> projections.

    Rows reached this way carry no shape label -- the forward scan knew the
    shape because it chose the predicate. shapes_for_row reverses the
    source_file predicates, and one payday row legitimately yields BOTH `loan`
    and `drive`, exactly as the address scan does.
    """
    pool = request.app.state.pool
    hal_id = person_id[len(HAL_ID_PREFIX):]

    person = await search.person_for_hal_id(pool, hal_id)
    if person is None:
        return error(404, f"unknown person id {person_id}")

    links = await search.records_for_hal_id(pool, hal_id)
    rows, timed_out = await search.rows_for_links(pool, links)

    by_shape: dict[str, list[dict[str, Any]]] = {shape: [] for shape in shapes}
    # rows_for_links groups by physical table and selects `WHERE record_id =
    # ANY(...)` with no ORDER BY, so the database returns them in whatever
    # order the plan emits. Ordered HERE for the same reason the address path
    # orders in bundle._stable_order: records[0] must not move between two
    # identical calls once a person has two rows of one shape.
    for row in sorted(rows, key=_by_record_id):
        for shape in shapes_for_row(row):
            if shape not in by_shape:
                continue
            # The same fail-closed gate the address scan applies: a
            # column-shifted property_owner row must never reach the model.
            # It reads raw_data, so it runs BEFORE project_row.
            if shape == "tax" and not quality.tax_row_is_usable(row):
                continue
            by_shape[shape].append(project_row(SHAPES[shape], row))

    return ok(
        {
            "person": {
                "id": person_id,
                "firstname": person["canonical_first_name"],
                "lastname": person["canonical_last_name"],
                # The partner ER graph is 17.9% suspicious and never applies its
                # computed merges. identity_confidence is MODAL at 40.50 (27.5%
                # of rows, the rest spread over the 34-70 band, live rows seen
                # at 70.85) -- NOT a maximum, so 40.50 must never be read as a
                # ceiling. These two fields are how the model discounts the
                # graph, so they are never omitted.
                "identity_confidence": person["identity_confidence"],
                "is_suspicious": person["is_suspicious"],
            },
            "records_by_source": {
                shape: records_mod.records_block(by_shape[shape], page, with_rowid=False)
                for shape in shapes
            },
            # record_id is not indexed on the partner records tables. An empty
            # result must never be read as "this person has no records" when it
            # actually means "the lookup ran out of time".
            "records_timed_out": timed_out,
            "unsupported_shapes": unsupported,
        }
    )


def _search_result(entity: dict[str, Any]) -> dict[str, Any]:
    first = entity["canonical_first_name"] or ""
    last = entity["canonical_last_name"] or ""
    return {
        "id": f"{HAL_ID_PREFIX}{entity['hal_id']}",
        "firstname": entity["canonical_first_name"],
        "lastname": entity["canonical_last_name"],
        "full_name": " ".join(part for part in (first, last) if part) or None,
        "match_score": entity["match_score"],
        # entity_master's own column, NOT a count of entity_links rows. The two
        # disagree in the partner corpus; this is the partner's number.
        "record_count": entity["record_count"],
        "identity_confidence": entity["identity_confidence"],
        "is_suspicious": entity["is_suspicious"],
        "address_line1": entity["canonical_address_line1"],
        "city": entity["canonical_city"],
        "state": entity["canonical_state"],
        "zip": entity["canonical_zip"],
    }


async def people_search(request: Request) -> JSONResponse:
    name = request.query_params.get("name") or ""
    if not name.strip():
        return error(400, "name is required")
    try:
        page = page_params(request.query_params, default_limit=DEFAULT_PAGE_LIMIT)
    except ValueError as exc:
        return error(400, str(exc))

    total, entities = await search.search_people(request.app.state.pool, name, limit=page.limit)
    results = [_search_result(entity) for entity in entities]
    return ok(
        {
            # `total` is a FLOOR, not a corpus-wide count, and this comment said
            # the opposite until 2026-08-11. search_people resolves names through
            # a bounded scan of silver.unique_keys (entity_master has no name
            # index and scanning it does not complete), so `total` counts the
            # entities found within that window -- capped at
            # search.NAME_KEY_CANDIDATE_LIMIT. A name with 5,000 corpus matches
            # reports 200.
            #
            # The old promise was only ever kept by a query that timed out, so
            # this is a bounded honest number replacing an exact one that never
            # arrived -- but consumers must read it as "at least N". `has_more`
            # is correspondingly a statement about the page versus that window,
            # not about the corpus. The service logs when the cap binds.
            "total_count": total,
            "has_more": len(results) < total,
            "results": results,
        }
    )


async def source_record(request: Request) -> JSONResponse:
    shape = str(request.path_params["shape"])
    rowid = int(request.path_params["rowid"])

    raw_address_id = request.query_params.get("address_id")
    if not raw_address_id:
        return error(
            400,
            "address_id is required: rowid is a position within one address's "
            "rows for this shape, so it cannot be resolved without the address",
        )
    try:
        address_id = int(raw_address_id)
    except ValueError:
        return error(400, f"address_id must be an integer, got {raw_address_id!r}")

    if shape not in SHAPES:
        return error(404, f"unknown shape {shape!r}; this corpus serves {sorted(SHAPES)}")

    bundle = await request.app.state.cache.get(address_id)
    if bundle is None:
        return error(404, f"unknown address_id {address_id}")

    rows = bundle.rows_by_shape.get(shape, [])
    if not 0 <= rowid < len(rows):
        return error(404, f"no {shape} row at rowid {rowid} for address {address_id}")

    row = rows[rowid]
    return ok(
        {
            "source": shape,
            # The partner corpus is one physical table; `table` names the SHAPE,
            # which is what provenance means to the consumer.
            "table": shape,
            "rowid": rowid,
            # Every id-linked shape derives its id from record_id, which is
            # unique across the corpus.
            "record_id": row.get("id") or row.get(f"{shape}_id"),
            "summary": records_mod.summarize(shape, row),
            "data": dict(row),
        }
    )


async def sql_source_record(request: Request) -> JSONResponse:
    """Hydrate one SQL discovery into concise, physical-schema provenance."""
    source_table = str(request.path_params["source_table"])
    raw_record_id = str(request.path_params["record_id"])
    if source_table not in {"records_new", "records_legacy"}:
        return error(
            404,
            f"unknown physical source table {source_table!r}; expected "
            "records_new or records_legacy",
        )
    try:
        record_id = int(raw_record_id)
    except ValueError:
        return error(400, f"record_id must be an integer, got {raw_record_id!r}")

    rows = await search.physical_record(request.app.state.pool, source_table, record_id)
    if not rows:
        return error(404, f"no {source_table} row with record_id {record_id}")
    if len(rows) != 1:
        return error(
            409,
            f"record_id {record_id} is not unique within {source_table}",
        )

    row = rows[0]
    data = {key: value for key, value in row.items() if value is not None}
    summary_fields = [
        f"{key}={data[key]}"
        for key in ("first_name", "last_name", "address", "city", "state", "zip")
        if data.get(key) not in (None, "")
    ]
    return ok(
        {
            "source": "partner_sql",
            "table": source_table,
            "rowid": None,
            "record_id": str(record_id),
            "source_file": str(row.get("source_file") or ""),
            "summary": "; ".join(summary_fields)[:500],
            "data": data,
        }
    )


async def run_sql(request: Request) -> JSONResponse:
    """The SQL hatch. Four guarded stages; a refusal is a 422, not a 500.

    The refusal body is the agent's whole feedback loop, so it carries `stage`
    (which control fired) and `hint` (what the corpus can actually serve)
    alongside `reason`, rather than a bare message.
    """
    try:
        # json.JSONDecodeError subclasses ValueError, so this covers a malformed
        # body as well as a non-JSON one.
        body = await request.json()
    except ValueError:
        return error(400, "request body must be JSON")
    if not isinstance(body, dict) or not str(body.get("query") or "").strip():
        return error(400, "query is required")

    # The same split resolve_address applies to `rows`, and for the same reason:
    # `int(max_rows)` inline in the call below would sit outside every try, so
    # {"max_rows": "abc"} would surface as a 500. A value we cannot parse is a
    # client bug and is REFUSED BY NAME; an out-of-range integer is a coherent
    # preference and is CLAMPED downstream in sql_hatch._cap_for.
    raw_max_rows = body.get("max_rows")
    if raw_max_rows is None:
        max_rows = None
    else:
        try:
            max_rows = int(raw_max_rows)
        except (TypeError, ValueError):
            return error(400, "max_rows must be an integer")

    try:
        result = await sql_hatch.run_query(
            request.app.state.pool, body["query"], max_rows=max_rows
        )
    except SqlRefused as refusal:
        return JSONResponse(
            {
                "refused": True,
                "stage": refusal.stage,
                "reason": refusal.reason,
                "hint": refusal.hint,
            },
            status_code=422,
        )
    return ok(result.as_payload())
