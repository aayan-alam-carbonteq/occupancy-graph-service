"""Operation 6: GET /v1/source-record/{shape}/{rowid}?address_id= -- provenance.

`rowid` is a positional index inside a bundle (Contract B pins rowid 0 next to
record_id "4001"), and a position is only meaningful relative to an address, so
address_id is a required query parameter. The response body is exactly as
pinned.
"""
from __future__ import annotations

import pytest


@pytest.fixture
async def address_id(client) -> int:
    body = (await client.post("/v1/resolve", json={"address": "123 Main St", "zip": "40505"})).json()
    return body["address_id"]


async def test_provenance_for_a_tax_row(client, address_id):
    response = await client.get(f"/v1/source-record/tax/0?address_id={address_id}")
    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "tax"
    assert body["table"] == "tax"
    assert body["rowid"] == 0
    assert body["record_id"] == "4001"
    assert body["summary"].startswith("tax; ownername=DOE, JANE ANN;")
    assert body["data"]["ownerstate"] == "IL"


async def test_the_summary_leads_with_the_field_that_matters_for_the_shape(client, address_id):
    body = (await client.get(f"/v1/source-record/utility/0?address_id={address_id}")).json()
    assert body["summary"].startswith("utility; first_name=Pat; last_name=Tenant;")


async def test_a_rowid_past_the_end_is_a_404(client, address_id):
    assert (await client.get(f"/v1/source-record/tax/99?address_id={address_id}")).status_code == 404


async def test_an_unknown_shape_is_a_404(client, address_id):
    response = await client.get(f"/v1/source-record/voter/0?address_id={address_id}")
    assert response.status_code == 404
    assert "voter" in response.json()["error"]


async def test_a_missing_address_id_is_a_400_naming_the_parameter(client):
    response = await client.get("/v1/source-record/tax/0")
    assert response.status_code == 400
    assert "address_id" in response.json()["error"]


async def test_an_unknown_address_id_is_a_404(client):
    assert (await client.get("/v1/source-record/tax/0?address_id=987654")).status_code == 404


# --- Each guard must be reachable -------------------------------------------
#
# The handler checks address_id BEFORE shape, so the four refusal tests above
# could all be tripping the same first branch. These pin which one each hits.


async def test_an_unknown_shape_is_refused_by_the_shape_check_not_an_earlier_one(client, address_id):
    """A valid address_id and a valid rowid, so nothing but the shape can be
    wrong. The message names the shape and the shapes this corpus does serve."""
    body = (await client.get(f"/v1/source-record/voter/0?address_id={address_id}")).json()
    assert body["error"].startswith("unknown shape 'voter'")
    assert "utility" in body["error"]


async def test_a_malformed_address_id_is_refused_by_name(client):
    response = await client.get("/v1/source-record/tax/0?address_id=abc")
    assert response.status_code == 400
    assert response.json()["error"] == "address_id must be an integer, got 'abc'"


async def test_an_unknown_shape_with_a_missing_address_id_reports_the_address_id(client):
    """Ordering pinned: address_id is checked first, so this is a 400 about
    address_id rather than a 404 about the shape. Both are defensible; which
    one the client gets must not drift silently."""
    response = await client.get("/v1/source-record/voter/0")
    assert response.status_code == 400
    assert "address_id" in response.json()["error"]


async def test_the_unknown_address_id_404_names_the_address_not_the_shape(client):
    body = (await client.get("/v1/source-record/tax/0?address_id=987654")).json()
    assert body["error"] == "unknown address_id 987654"


async def test_the_past_the_end_404_names_the_shape_and_the_rowid(client, address_id):
    body = (await client.get(f"/v1/source-record/tax/99?address_id={address_id}")).json()
    assert body["error"] == f"no tax row at rowid 99 for address {address_id}"


# --- What actually goes out on the wire -------------------------------------


async def test_record_id_is_null_for_a_shape_that_carries_no_id(client, address_id):
    """The utility manifest maps neither `id` nor `utility_id` -- the feed
    carries no identifier in the contract. `null` is the honest answer and is
    pinned here so it reads as deliberate rather than as a lookup that failed.
    Every OTHER shape derives `id` from record_id, which is corpus-unique."""
    body = (await client.get(f"/v1/source-record/utility/0?address_id={address_id}")).json()
    assert body["record_id"] is None
    assert "id" not in body["data"]
    assert "utility_id" not in body["data"]


async def test_an_id_linked_shape_carries_the_same_id_under_both_manifest_keys(client, address_id):
    """`id` and `<shape>_id` are BOTH derive.synthetic_id in the manifest, so
    for every id-linked shape they are the same value and the handler's
    `row.get("id") or row.get(f"{shape}_id")` fallback can never be reached.
    Pinned so that if a shape ever maps them differently, this says so."""
    body = (await client.get(f"/v1/source-record/tax/0?address_id={address_id}")).json()
    assert body["data"]["id"] == "4001"
    assert body["data"]["tax_id"] == "4001"
    assert body["record_id"] == body["data"]["id"]


async def test_data_is_the_whole_projected_row_including_the_norm_helpers(client, address_id):
    """`data` is dict(row) on the PROJECTED row, so it carries the `__norm_*`
    clustering helpers alongside the contract columns. That is the existing
    contract -- /v1/address/{id}/records already ships them -- and provenance
    means "everything we hold for this row", so it is pinned, not filtered."""
    body = (await client.get(f"/v1/source-record/tax/0?address_id={address_id}")).json()
    data = body["data"]
    assert [key for key in data if key.startswith("__")] == [
        "__norm_firstname",
        "__norm_lastname",
        "__norm_name_key",
        "__norm_address",
        "__norm_address_zip_key",
        "__norm_owneraddressline1",
    ]
    assert data["__norm_name_key"] == "jane|doe"
    assert data["__norm_address_zip_key"] == "123 MAIN ST|40505"
    # No __rowid: that stamp belongs to the paged record blocks, and this
    # endpoint reports the position in its own `rowid` field.
    assert "__rowid" not in data


async def test_the_full_summary_line_for_a_tax_row(client, address_id):
    """SUMMARY_FIELDS["tax"] order, with empty fields skipped."""
    body = (await client.get(f"/v1/source-record/tax/0?address_id={address_id}")).json()
    assert body["summary"] == (
        "tax; ownername=DOE, JANE ANN; address=123 MAIN ST; city=LEXINGTON; "
        "state=KY; ownercity=AURORA; ownerstate=IL"
    )


async def test_the_full_summary_line_for_a_utility_row(client, address_id):
    body = (await client.get(f"/v1/source-record/utility/0?address_id={address_id}")).json()
    assert body["summary"] == (
        "utility; first_name=Pat; last_name=Tenant; address=123 MAIN ST; "
        "city=LEXINGTON; state=KY; zip=40505"
    )


async def test_the_rowid_a_record_block_hands_out_resolves_here(client, address_id):
    """The two halves of the handle. `__rowid` on a paged record is the exact
    value this endpoint takes, so a consumer can round-trip from a listing to
    the provenance of any row without guessing."""
    records = (await client.get(f"/v1/address/{address_id}/records?shapes=tax")).json()
    listed = records["records_by_source"]["tax"]["records"][1]
    body = (
        await client.get(f"/v1/source-record/tax/{listed['__rowid']}?address_id={address_id}")
    ).json()
    assert body["rowid"] == listed["__rowid"] == 1
    assert body["record_id"] == listed["id"] == "4002"
