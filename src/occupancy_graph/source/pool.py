"""asyncpg pool for the partner corpus.

Every connection is pinned read-only with a statement timeout at setup, so the
guarantee holds for every query without each call site remembering to ask.

Scope of the read-only guarantee: `default_transaction_read_only` is a session
DEFAULT, not a security boundary. Idiomatic asyncpg usage cannot escape it —
including `conn.transaction(readonly=False)`, which omits the qualifier rather
than forcing READ WRITE — but raw `BEGIN READ WRITE` or `SET TRANSACTION READ
WRITE` will. We control every call site, so this is adequate here; the durable
protection is the partner granting a role without write privileges.
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from dataclasses import dataclass

import asyncpg

DEFAULT_STATEMENT_TIMEOUT_MS = 20_000


def _int_env(name: str, default: int) -> int:
    """Read an int env var, failing closed with a message naming the culprit
    instead of a bare `int()` ValueError — never silently fall back to
    `default` on a malformed value."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc


@dataclass
class PartnerPool:
    pool: asyncpg.Pool

    @classmethod
    async def create(
        cls,
        dsn: str,
        *,
        statement_timeout_ms: int = DEFAULT_STATEMENT_TIMEOUT_MS,
        min_size: int = 1,
        max_size: int = 8,
    ) -> "PartnerPool":
        if statement_timeout_ms <= 0:
            raise ValueError(
                f"statement_timeout_ms must be positive, got {statement_timeout_ms}. "
                "Postgres treats 0 as UNLIMITED, which would let a single query run "
                "without bound against the partner corpus."
            )

        async def _setup(conn: asyncpg.Connection) -> None:
            await conn.execute(f"SET statement_timeout = {int(statement_timeout_ms)}")
            await conn.execute("SET default_transaction_read_only = on")

        pool = await asyncpg.create_pool(
            dsn, min_size=min_size, max_size=max_size, init=_setup
        )
        return cls(pool=pool)

    @classmethod
    async def from_env(cls) -> "PartnerPool":
        dsn = os.environ.get("PARTNER_DSN")
        if not dsn:
            raise RuntimeError("PARTNER_DSN is not set")
        return await cls.create(
            dsn,
            statement_timeout_ms=_int_env(
                "PARTNER_STATEMENT_TIMEOUT_MS", DEFAULT_STATEMENT_TIMEOUT_MS
            ),
            min_size=_int_env("PARTNER_POOL_MIN", 1),
            max_size=_int_env("PARTNER_POOL_MAX", 8),
        )

    @asynccontextmanager
    async def acquire(self):
        async with self.pool.acquire() as conn:
            yield conn

    async def close(self) -> None:
        await self.pool.close()
