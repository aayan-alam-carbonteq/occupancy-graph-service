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
    # `first_seen`/`last_seen` (X-083) are DERIVED scalars — two YYYYMM months folded from the person's
    # trace rows — not row payload. The guard's purpose is unchanged: `rows` must never cross the wire.
    assert set(body["people"][0]) == {
        "id", "firstname", "middlename", "lastname", "full_name",
        "norm_name_key", "sources", "primary_address_id",
        "first_seen", "last_seen",
    }
    assert "rows" not in body["people"][0]
    assert body["total_count"] == len(body["people"])
    assert body["has_more"] is False


async def test_an_unknown_address_id_is_a_404(client):
    assert (await client.get("/v1/address/987654/people")).status_code == 404


# X-083 — the sighting span. Unit-level on purpose: the fixture corpus's trace rows are not
# guaranteed to carry Record_Date, and the logic under test is the fold, not the database.
from occupancy_graph.service.handlers import _public_person  # noqa: E402


def _person(rows):
    return {"id": "addr:1:0", "full_name": "BRENT MUSIC", "sources": {"trace"}, "rows": rows}


def test_sighting_span_is_first_and_last_trace_month():
    span = _public_person(_person([
        ("trace", {"record_date": "20250401"}),
        ("trace", {"record_date": "200101"}),
        ("trace", {"record_date": "20130701"}),
    ]))
    # YYYYMM regardless of whether the stored value was 6 or 8 digits, so the two formats sort together.
    assert (span["first_seen"], span["last_seen"]) == ("200101", "202504")


def test_sighting_span_ignores_non_trace_and_undated_rows():
    span = _public_person(_person([
        ("utility", {"record_date": "19990101"}),  # no other shape has a sighting date
        ("trace", {"record_date": None}),           # coerced-out malformed value
        ("trace", {"record_date": "20180701"}),
    ]))
    assert (span["first_seen"], span["last_seen"]) == ("201807", "201807")


def test_undated_person_gets_nulls_never_a_guess():
    span = _public_person(_person([("utility", {}), ("trace", {})]))
    assert (span["first_seen"], span["last_seen"]) == (None, None)
