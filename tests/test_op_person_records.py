"""Operation 4: GET /v1/person/{id}/records.

Ids are discriminated: `addr:<addressId>:<n>` is served from the bundle,
`hal:<hal_id>` from entity_links. The hal: half lands in the next task.
"""
from __future__ import annotations

import pytest


@pytest.fixture
async def address_id(client) -> int:
    body = (await client.post("/v1/resolve", json={"address": "123 Main St", "zip": "40505"})).json()
    return body["address_id"]


@pytest.fixture
async def jane_addr_id(client, address_id) -> str:
    body = (await client.get(f"/v1/address/{address_id}/people")).json()
    return next(p["id"] for p in body["people"] if p["norm_name_key"] == "jane|doe")


async def test_an_addr_person_returns_only_that_persons_rows(client, jane_addr_id):
    response = await client.get(f"/v1/person/{jane_addr_id}/records?shapes=trace,base")
    assert response.status_code == 200
    body = response.json()
    assert body["records_by_source"]["trace"]["total_count"] == 1
    assert body["records_by_source"]["trace"]["records"][0]["firstname"] == "Jane"
    assert body["records_by_source"]["base"]["total_count"] == 1


async def test_an_addr_person_carries_null_er_metadata(client, jane_addr_id):
    body = (await client.get(f"/v1/person/{jane_addr_id}/records")).json()
    assert body["person"]["id"] == jane_addr_id
    assert body["person"]["firstname"] == "Jane"
    assert body["person"]["lastname"] == "Doe"
    # The bundle path has no ER graph behind it. The keys are present with null
    # values so the payload shape is identical for both id kinds.
    assert body["person"]["identity_confidence"] is None
    assert body["person"]["is_suspicious"] is None


async def test_unknown_shapes_are_reported(client, jane_addr_id):
    body = (await client.get(f"/v1/person/{jane_addr_id}/records?shapes=tax,voter")).json()
    assert body["unsupported_shapes"] == ["voter"]


async def test_an_unknown_addr_person_is_a_404(client, address_id):
    assert (await client.get(f"/v1/person/addr:{address_id}:99/records")).status_code == 404
    assert (await client.get("/v1/person/addr:987654:0/records")).status_code == 404


async def test_a_malformed_person_id_is_a_400(client):
    for bad in ("nonsense", "addr:x:y", "addr:1"):
        response = await client.get(f"/v1/person/{bad}/records")
        assert response.status_code == 400, bad
        assert "person id" in response.json()["error"]


async def test_records_from_the_bundle_carry_a_rowid(client, jane_addr_id):
    body = (await client.get(f"/v1/person/{jane_addr_id}/records?shapes=base")).json()
    assert body["records_by_source"]["base"]["records"][0]["__rowid"] == 0


# --- hal: traversal. entity_links -> the underlying partner rows, projected
# --- through the same manifest the address scan uses. Owner-elsewhere depends
# --- on this path existing.


async def test_a_hal_person_returns_the_linked_rows_projected_by_shape(client):
    response = await client.get("/v1/person/hal:HAL0001/records")
    assert response.status_code == 200
    body = response.json()
    assert body["records_by_source"]["trace"]["total_count"] == 1
    assert body["records_by_source"]["trace"]["records"][0]["firstname"] == "Jane"
    assert body["records_by_source"]["base"]["total_count"] == 1
    # Two loan rows: 2001 at the subject address and 2010 elsewhere. Ordered by
    # record_id, so 2001 (employer ACME) leads and 2010 (GLOBEX) follows.
    assert body["records_by_source"]["loan"]["total_count"] == 2
    assert [r["employer"] for r in body["records_by_source"]["loan"]["records"]] == [
        "ACME", "GLOBEX",
    ]


async def test_a_payday_row_reached_by_hal_id_is_both_loan_and_drive(client):
    body = (await client.get("/v1/person/hal:HAL0001/records")).json()
    assert body["records_by_source"]["drive"]["total_count"] == 2
    assert body["records_by_source"]["drive"]["records"][0]["dl_num"] == "A12345678"
    assert [r["dl_num"] for r in body["records_by_source"]["drive"]["records"]] == [
        "A12345678", "B98765432",
    ]


async def test_identity_confidence_and_is_suspicious_are_surfaced(client):
    body = (await client.get("/v1/person/hal:HAL0001/records")).json()
    assert body["person"] == {
        "id": "hal:HAL0001",
        "firstname": "JANE",
        "lastname": "DOE",
        "identity_confidence": 40.5,
        "is_suspicious": False,
    }


async def test_a_suspicious_entity_is_flagged_not_suppressed(client):
    body = (await client.get("/v1/person/hal:HAL0002/records")).json()
    assert body["person"]["is_suspicious"] is True
    assert body["person"]["identity_confidence"] == 88.0
    assert body["records_by_source"]["trace"]["total_count"] == 1


async def test_hal_records_are_filtered_by_the_shapes_parameter(client):
    body = (await client.get("/v1/person/hal:HAL0001/records?shapes=loan,voter")).json()
    assert set(body["records_by_source"]) == {"loan"}
    assert body["unsupported_shapes"] == ["voter"]


async def test_hal_records_carry_no_rowid_because_they_are_not_bundle_scoped(client):
    body = (await client.get("/v1/person/hal:HAL0001/records")).json()
    assert "__rowid" not in body["records_by_source"]["loan"]["records"][0]


async def test_hal_records_report_the_timeout_flag(client):
    body = (await client.get("/v1/person/hal:HAL0001/records")).json()
    assert body["records_timed_out"] is False


async def test_an_unknown_hal_id_is_a_404(client):
    assert (await client.get("/v1/person/hal:HAL9999/records")).status_code == 404


async def test_a_hal_persons_rows_span_addresses_in_a_stable_record_id_order(client):
    """Owner-elsewhere, and the reason `_by_record_id` exists.

    HAL0001's links reach payday rows at TWO addresses: 2001 at the subject
    address and 2010 at 742 EVERGREEN TER in another ZIP, which the address scan
    can never see. That is the signal the hal: path is for.

    It also pins the ORDER. rows_for_links groups by physical table and selects
    `WHERE record_id = ANY(...)` with no ORDER BY, and it fetches those groups in
    LINK order -- 2010 is seeded at the highest confidence, so it comes back
    first. Sorting by record_id in the handler is what makes `records[0]`
    reproducible; drop the sort and this list inverts to 2010, 2001.
    """
    body = (await client.get("/v1/person/hal:HAL0001/records?shapes=loan")).json()
    block = body["records_by_source"]["loan"]
    assert block["total_count"] == 2
    assert [r["loan_id"] for r in block["records"]] == ["2001", "2010"]
    assert [r["address"] for r in block["records"]] == ["123 MAIN ST", "742 EVERGREEN TER"]


async def test_a_column_shifted_tax_row_reached_by_hal_id_is_dropped_not_served(client):
    """The fail-closed tax gate runs on the hal: path too.

    HAL0002 links record 4003 -- a property_owner row whose embedded GeoJSON was
    split on its commas upstream, sliding every later field. `shapes_for_row`
    still classifies it as `tax` (its source_file matches property_owner%), so
    without quality.tax_row_is_usable it would be projected and served, and
    "PENGROVE ST" would reach the model as an owner name. Removing the gate turns
    this count from 0 into 1.

    HONEST NOTE ON REACHABILITY -- read before deleting this branch as dead code:
    the partner-DB survey found property_owner rows are absent from entity_links
    ENTIRELY (0 of 200 sampled). They carry ssn, dob and house_number at 0%
    populated, so the ER graph has no blocking key to link them by. In production
    this branch is therefore expected to be unreachable. The link that makes this
    test work is SYNTHETIC and is NOT evidence that production links tax rows.
    The gate stays because it is the same fail-closed gate the address path
    applies, it costs one condition, and if the partner ever does begin linking
    property rows it is already proven rather than assumed.
    """
    body = (await client.get("/v1/person/hal:HAL0002/records")).json()
    assert body["records_by_source"]["tax"]["total_count"] == 0
    assert body["records_by_source"]["tax"]["records"] == []
    # The traversal itself ran and did reach records_new; the tax row was
    # fetched and shape-classified, then dropped. The trace row is the control.
    assert body["records_by_source"]["trace"]["total_count"] == 1
