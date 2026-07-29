"""Operation 2: GET /v1/address/{id}/records?shapes=&limit=&offset="""
from __future__ import annotations

import pytest


@pytest.fixture
async def address_id(client) -> int:
    body = (await client.post("/v1/resolve", json={"address": "123 Main St", "zip": "40505"})).json()
    return body["address_id"]


async def test_requested_shapes_only(client, address_id):
    response = await client.get(f"/v1/address/{address_id}/records?shapes=tax,base")
    assert response.status_code == 200
    body = response.json()
    assert set(body["records_by_source"]) == {"tax", "base"}
    assert body["unsupported_shapes"] == []
    assert body["records_by_source"]["tax"]["total_count"] == 2


async def test_no_shapes_parameter_returns_every_shape(client, address_id):
    body = (await client.get(f"/v1/address/{address_id}/records")).json()
    assert set(body["records_by_source"]) == {
        "utility", "trace", "base", "loan", "drive", "auto", "tax"
    }


async def test_an_unknown_shape_is_reported_not_ignored(client, address_id):
    body = (await client.get(f"/v1/address/{address_id}/records?shapes=tax,voter")).json()
    assert body["unsupported_shapes"] == ["voter"]
    assert set(body["records_by_source"]) == {"tax"}


async def test_limit_and_offset_page_within_a_shape(client, address_id):
    first = (await client.get(f"/v1/address/{address_id}/records?shapes=trace&limit=1")).json()
    assert first["records_by_source"]["trace"]["total_count"] == 2
    assert first["records_by_source"]["trace"]["has_more"] is True
    assert len(first["records_by_source"]["trace"]["records"]) == 1
    assert first["records_by_source"]["trace"]["records"][0]["__rowid"] == 0

    second = (await client.get(
        f"/v1/address/{address_id}/records?shapes=trace&limit=1&offset=1"
    )).json()
    assert second["records_by_source"]["trace"]["has_more"] is False
    assert second["records_by_source"]["trace"]["records"][0]["__rowid"] == 1


async def test_an_unknown_address_id_is_a_404(client):
    assert (await client.get("/v1/address/987654/records")).status_code == 404


async def test_a_malformed_limit_is_a_400(client, address_id):
    response = await client.get(f"/v1/address/{address_id}/records?limit=ten")
    assert response.status_code == 400
    assert "limit" in response.json()["error"]


async def test_records_survive_a_hot_cache_eviction(client, address_id, service_cache):
    """A hot miss with a live cold entry re-materializes rather than 404ing.

    `service_cache` is the very cache the app was built on (see conftest), so
    this drives the real two-tier path instead of reaching through httpx's
    private transport for it.

    The bundle identity check is what stops this passing vacuously: if
    evict_hot were a no-op, or the handler were served from a still-warm hot
    entry, `after` would be the SAME object `before` was, and no
    re-materialization would have happened at all.
    """
    before = await service_cache.get(address_id)
    service_cache.evict_hot(address_id)

    body = (await client.get(f"/v1/address/{address_id}/records?shapes=utility")).json()
    assert body["records_by_source"]["utility"]["total_count"] == 1

    after = await service_cache.get(address_id)
    assert after is not before
