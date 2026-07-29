"""Operation 5: GET /v1/people/search?name=&limit= over silver.entity_master."""
from __future__ import annotations

import httpx


async def test_search_returns_a_hal_scoped_result_with_its_address(client):
    response = await client.get("/v1/people/search?name=Jane%20Doe")
    assert response.status_code == 200
    body = response.json()
    assert body["total_count"] == 1
    assert body["has_more"] is False
    result = body["results"][0]
    assert result["id"] == "hal:HAL0001"
    assert result["firstname"] == "JANE"
    assert result["lastname"] == "DOE"
    assert result["full_name"] == "JANE DOE"
    assert result["match_score"] == 1.0
    # record_count is a STATIC seeded column on entity_master, not a count of
    # entity_links rows -- HAL0001 has four links (1002, 1004, 2001, and the
    # owner-elsewhere 2010) while the column still says 3. That inconsistency is
    # the partner's, faithfully reproduced; this endpoint reports the column.
    assert result["record_count"] == 3
    assert result["address_line1"] == "123 MAIN ST"
    assert result["city"] == "LEXINGTON"
    assert result["state"] == "KY"
    assert result["zip"] == "40505"


async def test_er_metadata_is_on_every_result(client):
    body = (await client.get("/v1/people/search?name=Jane%20Doe")).json()
    assert body["results"][0]["identity_confidence"] == 40.5
    assert body["results"][0]["is_suspicious"] is False


async def test_a_suspicious_entity_is_returned_flagged(client):
    body = (await client.get("/v1/people/search?name=John%20Smith")).json()
    assert body["results"][0]["is_suspicious"] is True
    assert body["results"][0]["identity_confidence"] == 88.0


async def test_a_surname_only_query_scores_lower(client):
    body = (await client.get("/v1/people/search?name=Doe")).json()
    # Two non-merged DOEs now match. ORDER BY record_count DESC NULLS LAST,
    # hal_id puts JANE (record_count 3) ahead of RICHARD (2), so results[0] is
    # still HAL0001 -- pinned here so this test cannot start silently reading a
    # different row if the ordering ever changes.
    assert body["results"][0]["id"] == "hal:HAL0001"
    assert body["results"][0]["match_score"] == 0.6
    assert body["results"][1]["id"] == "hal:HAL0003"
    assert body["results"][1]["match_score"] == 0.6


async def test_an_unknown_name_returns_an_empty_result_not_an_error(client):
    body = (await client.get("/v1/people/search?name=Nobody%20Here")).json()
    assert body == {"total_count": 0, "has_more": False, "results": []}


async def test_a_missing_name_is_a_400(client):
    response = await client.get("/v1/people/search")
    assert response.status_code == 400
    assert "name" in response.json()["error"]


async def test_has_more_reflects_the_true_total_not_the_page(client):
    """total_count is `count(*) OVER ()` -- the true match count -- not len(rows).

    The seed holds THREE DOEs: HAL0001 (JANE), HAL0003 (RICHARD) and HAL0004
    (MARY, is_merged). The merged one is filtered out, so a `name=Doe&limit=1`
    query matches 2 and returns 1. That gap is the entire point of the test: a
    handler that reported len(results) would say total_count 1 / has_more false
    and pass every other assertion in this file.
    """
    body = (await client.get("/v1/people/search?name=Doe&limit=1")).json()
    assert len(body["results"]) == 1
    assert body["total_count"] == 2
    assert body["has_more"] is True


async def test_a_merged_duplicate_is_never_offered_as_a_person(client):
    """HAL0004 MARY DOE is `is_merged = true`, superseded by HAL0001. The ER
    graph records its merges but never applies them, so both sides stay in
    entity_master; only the filter keeps the dead one out of the results."""
    body = (await client.get("/v1/people/search?name=Doe&limit=50")).json()
    assert [result["id"] for result in body["results"]] == ["hal:HAL0001", "hal:HAL0003"]
    assert body["total_count"] == 2
    assert body["has_more"] is False


# --- /v1/people/... vs /v1/person/... -----------------------------------------


async def test_the_search_route_and_the_person_route_do_not_shadow_each_other(client):
    """`people` and `person` are distinct literals, so neither path can fall
    into the other's handler. Pinned by the response SHAPE, which is unique per
    handler: only the search returns `results`, only person records returns
    `records_by_source`."""
    search = (await client.get("/v1/people/search?name=Doe")).json()
    assert "results" in search and "records_by_source" not in search

    person = (await client.get("/v1/person/hal:HAL0001/records")).json()
    assert "records_by_source" in person and "results" not in person

    # The person route is a wildcard, so "search" is a legal (if unknown)
    # person id there. It must be rejected as a person id, NOT quietly served
    # by people_search.
    stray = await client.get("/v1/person/search/records")
    assert stray.status_code == 400
    assert "malformed person id" in stray.json()["error"]


async def test_route_order_is_documentation_not_load_bearing(service_pool, service_cache):
    """Registration order is chosen for readability; correctness must not depend
    on it. Swapping the two routes in a real app must change nothing -- if a
    future path ever DOES need the ordering, this fails and says so."""
    from occupancy_graph.service.app import create_app

    app = create_app(pool=service_pool, cache=service_cache)
    paths = [route.path for route in app.routes]
    left, right = paths.index("/v1/people/search"), paths.index("/v1/person/{person_id}/records")
    assert left < right, "the literal route is registered first, by intent"
    app.routes[left], app.routes[right] = app.routes[right], app.routes[left]

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://graph.test") as swapped:
        search = await swapped.get("/v1/people/search?name=Doe")
        person = await swapped.get("/v1/person/hal:HAL0001/records")

    assert search.status_code == 200
    assert [result["id"] for result in search.json()["results"]] == ["hal:HAL0001", "hal:HAL0003"]
    assert person.status_code == 200
    assert person.json()["person"]["id"] == "hal:HAL0001"
