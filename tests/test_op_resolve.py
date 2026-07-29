"""Operation 1: POST /v1/resolve -- replaces PREFLIGHT_QUERY in one round trip."""
from __future__ import annotations


async def test_resolve_returns_a_candidate_with_the_resolved_address_fields(client):
    response = await client.post("/v1/resolve", json={"address": "123 Main St", "zip": "40505"})
    assert response.status_code == 200
    body = response.json()
    candidate = body["candidates"][0]
    assert candidate["address_id"] == body["address_id"]
    assert candidate["match_score"] == 1.0
    assert candidate["matched_fields"] == ["address"]
    assert candidate["norm_address"] == "123 MAIN ST"
    assert candidate["zip5"] == "40505"
    assert candidate["street_number"] == "123"
    assert candidate["street_name"] == "MAIN ST"
    assert candidate["unit"] is None
    assert candidate["city"] == "LEXINGTON"
    assert candidate["state"] == "KY"
    assert candidate["county"] == "FAYETTE"
    assert candidate["relation_count"] > 0


async def test_resolve_reports_every_shape_count_including_zeros(client):
    body = (await client.post("/v1/resolve", json={"address": "123 Main St", "zip": "40505"})).json()
    assert set(body["source_counts"]) == {
        "utility", "trace", "base", "loan", "drive", "auto", "tax"
    }
    assert body["source_counts"]["utility"] == 1
    assert body["source_counts"]["trace"] == 2
    assert body["source_counts"]["tax"] == 2


async def test_resolve_reports_quality_gate_drops_and_the_tax_timeout(client):
    body = (await client.post("/v1/resolve", json={"address": "123 Main St", "zip": "40505"})).json()
    assert body["dropped_counts"] == {"tax": 1}   # record 4003 is column-shifted
    assert body["tax_timed_out"] is False


async def test_resolve_returns_the_first_rows_per_shape_with_raw_vendor_keys(client):
    body = (await client.post("/v1/resolve", json={"address": "123 Main St", "zip": "40505"})).json()
    utility = body["records_by_source"]["utility"]
    assert utility["total_count"] == 1
    assert utility["has_more"] is False
    assert utility["records"][0]["first_name"] == "Pat"   # NOT firstName
    tax = body["records_by_source"]["tax"]["records"][0]
    assert tax["ownername"] == "DOE, JANE ANN"
    assert tax["ownerstate"] == "IL"


async def test_records_carry_a_rowid_so_provenance_is_reachable(client):
    body = (await client.post("/v1/resolve", json={"address": "123 Main St", "zip": "40505"})).json()
    assert body["records_by_source"]["tax"]["records"][0]["__rowid"] == 0


async def test_an_unresolvable_address_returns_null_with_diagnosable_counts(client):
    body = (await client.post("/v1/resolve", json={"address": "999 Nowhere Rd", "zip": "40505"})).json()
    assert body["address_id"] is None
    assert body["candidates"] == []
    assert body["source_counts"]["utility"] == 0
    assert body["tax_timed_out"] is False


async def test_resolving_the_same_address_twice_returns_the_same_id(client):
    first = (await client.post("/v1/resolve", json={"address": "123 Main St", "zip": "40505"})).json()
    second = (await client.post("/v1/resolve", json={"address": "123 MAIN STREET", "zip": "40505"})).json()
    assert second["address_id"] == first["address_id"]


async def test_a_missing_address_is_a_400_not_a_500(client):
    assert (await client.post("/v1/resolve", json={"zip": "40505"})).status_code == 400
    assert (await client.post("/v1/resolve", content=b"not json")).status_code == 400


async def test_an_unparseable_rows_value_is_a_400_naming_rows(client):
    response = await client.post(
        "/v1/resolve", json={"address": "123 Main St", "zip": "40505", "rows": "abc"}
    )
    assert response.status_code == 400
    assert "rows" in response.json()["error"]


async def test_an_out_of_range_rows_value_is_clamped_rather_than_refused(client):
    """Deliberately asymmetric with the test above: garbage is REFUSED, but an
    integer out of range is a coherent ask ("as few as possible") and is
    CLAMPED to one row. total_count still reports the full result."""
    body = (await client.post(
        "/v1/resolve", json={"address": "123 Main St", "zip": "40505", "rows": 0}
    )).json()
    tax = body["records_by_source"]["tax"]
    assert tax["total_count"] == 2
    assert len(tax["records"]) == 1
    assert tax["has_more"] is True

    negative = (await client.post(
        "/v1/resolve", json={"address": "123 Main St", "zip": "40505", "rows": -5}
    )).json()
    assert len(negative["records_by_source"]["tax"]["records"]) == 1


async def test_tax_records_come_back_in_record_id_order(client):
    """The order contract POST /v1/resolve owes the engine. The scan has no
    ORDER BY; source.bundle sorts by record_id after fetching. 4001 (DOE)
    precedes 4002 (ACME) regardless of what the plan emitted."""
    body = (await client.post("/v1/resolve", json={"address": "123 Main St", "zip": "40505"})).json()
    assert [row["ownername"] for row in body["records_by_source"]["tax"]["records"]] == [
        "DOE, JANE ANN", "ACME HOLDINGS LLC"
    ]


async def test_getting_the_resolve_route_returns_405_json(client):
    # Relocated from tests/test_app.py: the plan asserted this one task early,
    # when /v1/resolve was not yet routed and Starlette answered 404. Now that
    # the route exists with methods=["POST"], the 405 it was written for is
    # actually reachable.
    response = await client.get("/v1/resolve")
    assert response.status_code == 405
    assert response.headers["content-type"].startswith("application/json")
    assert "error" in response.json()
    # RFC 9110 requires Allow on a 405 so a client can discover the right
    # method. The router attaches it to the HTTPException; the JSON error
    # handler must forward it rather than build a bare response.
    assert "POST" in response.headers["allow"]
