"""Request handlers for the typed surface and the SQL hatch.

Every handler reads its dependencies off request.app.state (`pool`, `cache`),
so the app is constructible with injected fakes and no environment.
"""
from __future__ import annotations

from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse

from occupancy_graph.service.jsonio import jsonable


def ok(payload: dict[str, Any]) -> JSONResponse:
    return JSONResponse(jsonable(payload))


def error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


async def healthz(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})
