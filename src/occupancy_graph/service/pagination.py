"""limit/offset parsing and the paged-block shape used across the typed surface.

Every collection in Contract B is a {total_count, has_more, <key>} block, where
<key> is "records", "people" or "results". total_count is the size of the FULL
result, not the window, so the engine can tell "nothing there" from "more to
fetch".
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from occupancy_graph.service.limits import DEFAULT_PAGE_LIMIT, MAX_PAGE_LIMIT


@dataclass(frozen=True)
class Page:
    limit: int
    offset: int


def _int_param(params: Mapping[str, str], name: str, default: int) -> int:
    """Parse one query parameter. A malformed value RAISES rather than falling
    back to `default` -- a silently different page size is worse than a 400."""
    raw = params.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc


def page_params(
    params: Mapping[str, str],
    *,
    default_limit: int = DEFAULT_PAGE_LIMIT,
    max_limit: int = MAX_PAGE_LIMIT,
) -> Page:
    limit = _int_param(params, "limit", default_limit)
    offset = _int_param(params, "offset", 0)
    return Page(limit=min(max(1, limit), max_limit), offset=max(0, offset))


def paginate(items: Sequence[Any], page: Page, *, key: str = "records") -> dict[str, Any]:
    total = len(items)
    window = list(items[page.offset : page.offset + page.limit])
    return {
        "total_count": total,
        "has_more": page.offset + len(window) < total,
        key: window,
    }
