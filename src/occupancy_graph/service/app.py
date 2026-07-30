"""The ASGI application.

Single process, single event loop, ONE shared pool and ONE shared BundleCache.
The deleted GraphQL server forked uvicorn workers because its SQLite resolvers
were synchronous and blocked the loop; every path here is async I/O against
asyncpg. Multiple workers would each hold their own bundle cache and re-run the
173 ms - 32 s address scan per worker, which is precisely the cost the cache
exists to remove.
"""
from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator

from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from occupancy_graph.service import handlers
from occupancy_graph.source.bundle import BundleCache
from occupancy_graph.source.pool import PartnerPool


async def _http_exception(request: Request, exc: HTTPException) -> JSONResponse:
    """Starlette's default 404/405 body is plain text. The engine parses JSON
    unconditionally, so every error on this service is a JSON object.

    `exc.headers` must be forwarded, not dropped: the router attaches
    `Allow` to the 405 it raises, and RFC 9110 requires that header on a 405 so
    a client can discover the method it should have used.
    """
    return JSONResponse(
        {"error": exc.detail}, status_code=exc.status_code, headers=exc.headers
    )


def create_app(*, pool: PartnerPool | None = None, cache: BundleCache | None = None) -> Starlette:
    """Build the app. With `pool` supplied the lifespan is a no-op (tests);
    without it the pool is built from PARTNER_DSN on startup and closed on
    shutdown."""

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        if app.state.pool is None:
            app.state.pool = await PartnerPool.from_env()
            app.state.cache = BundleCache(app.state.pool)
            owns_pool = True
        else:
            owns_pool = False
        try:
            yield
        finally:
            if owns_pool:
                await app.state.pool.close()

    routes = [
        Route("/healthz", handlers.healthz, methods=["GET"]),
        Route("/v1/resolve", handlers.resolve_address, methods=["POST"]),
        Route("/v1/address/{address_id:int}/records", handlers.address_records, methods=["GET"]),
        Route("/v1/address/{address_id:int}/people", handlers.address_people, methods=["GET"]),
        # Before /v1/person/... . The prefixes are distinct (`people` vs
        # `person`) so neither can shadow the other, but the literal route is
        # kept ahead of the parameterised one so the read order is obvious.
        Route("/v1/people/search", handlers.people_search, methods=["GET"]),
        Route("/v1/person/{person_id}/records", handlers.person_records, methods=["GET"]),
        Route("/v1/source-record/{shape}/{rowid:int}", handlers.source_record, methods=["GET"]),
        Route("/v1/sql", handlers.run_sql, methods=["POST"]),
    ]
    app = Starlette(
        routes=routes,
        lifespan=lifespan,
        exception_handlers={HTTPException: _http_exception},
    )
    app.state.pool = pool
    app.state.cache = cache if cache is not None else (BundleCache(pool) if pool else None)
    return app


# Import target for `uvicorn occupancy_graph.service.app:app`.
app = create_app()
