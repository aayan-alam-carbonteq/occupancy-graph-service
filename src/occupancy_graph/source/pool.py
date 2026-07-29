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

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import asyncpg

logger = logging.getLogger(__name__)

DEFAULT_STATEMENT_TIMEOUT_MS = 20_000

# How long close() waits for a graceful drain before falling back to
# terminate(). asyncpg.Pool.close() waits INDEFINITELY for its connections to
# release -- there is no timeout parameter -- and a connection left mid-
# cancellation never releases. Unbounded, that stalls a test session with no
# output and stalls a production container until the orchestrator SIGKILLs it.
#
# The window is bracketed:
#   LOWER  the only thing that can legitimately hold the drain is a query
#          already in flight when shutdown began, and that is hard-bounded by
#          this pool's own statement_timeout. A shorter window would terminate
#          drains that were about to succeed, so the value is DERIVED from the
#          statement timeout rather than fixed -- raising the query budget must
#          not silently make the shutdown window too tight.
#   GRACE  +5 s for the socket/protocol teardown asyncpg does after the last
#          query releases, which is not covered by statement_timeout.
#   UPPER  capped, because the derivation must not be able to push shutdown
#          past the orchestrator's patience. Kubernetes'
#          terminationGracePeriodSeconds defaults to 30 s; firing at 25 s means
#          we terminate ourselves (and log why) with room to spare instead of
#          being SIGKILLed and learning nothing.
#
# At the default 20 000 ms statement timeout this yields 25.0 s.
CLOSE_DRAIN_GRACE_SECONDS = 5.0
MAX_CLOSE_TIMEOUT_SECONDS = 25.0


def close_timeout_for(statement_timeout_ms: int) -> float:
    """The graceful-drain window implied by a pool's statement timeout."""
    return min(
        statement_timeout_ms / 1000 + CLOSE_DRAIN_GRACE_SECONDS,
        MAX_CLOSE_TIMEOUT_SECONDS,
    )


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
    # Overridable per instance so a test can prove the fallback without waiting
    # out the production window; `create()` derives it from statement_timeout.
    close_timeout_seconds: float = field(
        default_factory=lambda: close_timeout_for(DEFAULT_STATEMENT_TIMEOUT_MS)
    )

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
        return cls(
            pool=pool, close_timeout_seconds=close_timeout_for(statement_timeout_ms)
        )

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
        """Drain gracefully if we can, terminate if we must — but always RETURN.

        `asyncpg.Pool.close()` has no timeout and waits forever for every
        connection to be released; `terminate()` is the only escape hatch. A
        connection that was mid-cancellation when shutdown began can never
        release, so the bare await is an unbounded stall: a silent, output-less
        test hang, and in production a container that has to be SIGKILLed.

        The fallback is logged at WARNING and never swallowed silently. If this
        fires in production it is the only signal that a connection wedged, and
        a quiet terminate() would teach us nothing.
        """
        try:
            await asyncio.wait_for(self.pool.close(), timeout=self.close_timeout_seconds)
        except TimeoutError:
            logger.warning(
                "partner pool did not drain within %.1fs; terminating its "
                "connections. A connection was still held after the graceful "
                "window (most likely one left mid-cancellation by a statement "
                "timeout). Shutdown continues, but this is worth investigating.",
                self.close_timeout_seconds,
            )
            # asyncpg cancels the in-flight close() above and calls terminate()
            # itself on the way out, which makes this call a no-op. It is still
            # made unconditionally: the fallback must not depend on an
            # implementation detail of asyncpg's own cancellation handling, and
            # terminate() is idempotent.
            self.pool.terminate()
