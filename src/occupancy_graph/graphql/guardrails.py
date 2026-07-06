from __future__ import annotations

DEFAULT_LIMIT = 50
MAX_LIMIT = 500
DEFAULT_OFFSET = 0
MAX_QUERY_DEPTH = 12
MAX_ALIASES = 50


def clamp_limit(limit: int | None) -> int:
    if limit is None:
        return DEFAULT_LIMIT
    return min(max(1, limit), MAX_LIMIT)


def clamp_offset(offset: int | None) -> int:
    if offset is None:
        return DEFAULT_OFFSET
    return max(0, offset)
