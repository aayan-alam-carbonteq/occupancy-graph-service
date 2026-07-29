"""Operation 3: GET /v1/address/{id}/people -- name-key clustering over the bundle.

Deliberately NOT the partner hal_id graph: see source/people.py. Company/trust
owners are excluded, so a mailing-elsewhere owner is never manufactured into a
resident.
"""
from __future__ import annotations

import pytest


@pytest.fixture
async def address_id(client) -> int:
    body = (await client.post("/v1/resolve", json={"address": "123 Main St", "zip": "40505"})).json()
    return body["address_id"]


async def test_people_are_clustered_by_name_with_their_sources(client, address_id):
    response = await client.get(f"/v1/address/{address_id}/people")
    assert response.status_code == 200
    body = response.json()
    jane = next(p for p in body["people"] if p["norm_name_key"] == "jane|doe")
    assert jane["firstname"] == "Jane"
    assert jane["lastname"] == "Doe"
    assert jane["full_name"] == "Jane A Doe"
    assert jane["primary_address_id"] == address_id
    assert set(jane["sources"]) >= {"trace", "base", "loan", "auto", "tax"}


async def test_person_ids_are_address_scoped_and_prefixed(client, address_id):
    body = (await client.get(f"/v1/address/{address_id}/people")).json()
    assert all(person["id"].startswith(f"addr:{address_id}:") for person in body["people"])


async def test_company_owners_are_not_people_at_the_address(client, address_id):
    """Not vacuous: fixture record 4002 is a tax row at THIS address whose
    ownerName is "ACME HOLDINGS LLC" and whose last_name column is "ACME". It
    reaches the bundle (tax total_count is 2) with a non-empty derived
    `ownercompany` and a clusterable name key of "|acme". Only the exclusion in
    source/people.py keeps it out -- without it a person surnamed ACME appears,
    and the engine would read an absentee corporate owner as a resident."""
    body = (await client.get(f"/v1/address/{address_id}/people")).json()
    assert all(person["lastname"] != "ACME" for person in body["people"])


async def test_the_response_carries_no_internal_row_payload(client, address_id):
    body = (await client.get(f"/v1/address/{address_id}/people")).json()
    assert set(body["people"][0]) == {
        "id", "firstname", "middlename", "lastname", "full_name",
        "norm_name_key", "sources", "primary_address_id",
    }
    assert body["total_count"] == len(body["people"])
    assert body["has_more"] is False


async def test_an_unknown_address_id_is_a_404(client):
    assert (await client.get("/v1/address/987654/people")).status_code == 404
