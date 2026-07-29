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
