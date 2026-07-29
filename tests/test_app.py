"""App wiring: health, 404 shape, error shape, and injected dependencies."""
from __future__ import annotations

from occupancy_graph.service.app import create_app


async def test_healthz_reports_ok(client):
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_an_unknown_path_returns_a_json_error_not_html(client):
    response = await client.get("/v1/nope")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
    assert "error" in response.json()


async def test_a_wrong_method_returns_json(client):
    # The plan asserted this against GET /v1/resolve, which is not routed until
    # the next task -- Starlette would answer 404, not 405. /healthz is routed
    # GET-only here and now, so POSTing it exercises the same thing the test is
    # for: Starlette's default 405 body is plain text, and every error on this
    # service must be a JSON object.
    response = await client.post("/healthz")
    assert response.status_code == 405
    assert response.headers["content-type"].startswith("application/json")
    assert "error" in response.json()


async def test_the_app_exposes_the_injected_pool_and_cache(service_pool):
    from occupancy_graph.source.bundle import BundleCache

    cache = BundleCache(service_pool)
    app = create_app(pool=service_pool, cache=cache)
    assert app.state.pool is service_pool
    assert app.state.cache is cache


async def test_no_graphql_route_survives(client):
    for path in ("/graphql", "/v1/graphql"):
        assert (await client.post(path, json={"query": "{ __typename }"})).status_code in (404, 405)
