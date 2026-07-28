"""asyncpg pool for the partner corpus.

Every connection is pinned read-only with a statement timeout at setup, so the
guarantee holds for every query without each call site remembering to ask.
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from dataclasses import dataclass

import asyncpg

DEFAULT_STATEMENT_TIMEOUT_MS = 20_000


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
            statement_timeout_ms=int(
                os.environ.get("PARTNER_STATEMENT_TIMEOUT_MS", DEFAULT_STATEMENT_TIMEOUT_MS)
            ),
            min_size=int(os.environ.get("PARTNER_POOL_MIN", 1)),
            max_size=int(os.environ.get("PARTNER_POOL_MAX", 8)),
        )

    @asynccontextmanager
    async def acquire(self):
        async with self.pool.acquire() as conn:
            yield conn

    async def close(self) -> None:
        await self.pool.close()
