"""asyncpg pool for the partner corpus.

Both safety settings — `statement_timeout` and `default_transaction_read_only`
— are passed as `server_settings`, so they travel in the connection's STARTUP
PACKET and hold for the whole life of that connection, every acquire.

WHY `server_settings` AND NOT `SET` IN AN init=/setup= CALLBACK. This is
load-bearing; do not "simplify" it back. asyncpg runs a reset script that
includes `RESET ALL` on every RELEASE (see `Connection.get_reset_query`), so a
session-level `SET` issued from `init=` survives exactly ONE acquire. Measured
against the fixture with the previous `init=` version, statement_timeout_ms
20 000, max_size 1:

    acquire #1: default_transaction_read_only=on   statement_timeout=20s
    acquire #2: default_transaction_read_only=off  statement_timeout=0
    acquire #3: default_transaction_read_only=off  statement_timeout=0
    CREATE TEMP TABLE on the next acquire: SUCCEEDED

`RESET ALL` restores each GUC to its SESSION-START value, and startup-packet
options ARE that value — which is precisely why `server_settings` survives the
reset and `SET` does not. In a long-lived service the first request was the
only protected one.

The timeout half is the quieter of the two failures. It reset to `0`, which
Postgres reads as UNLIMITED — the exact state `create()` below raises
`ValueError` to prevent, arrived at silently one release later. From that point
every typed operation and the SQL hatch ran against a 7.6 B-row partner corpus
with no cost bound at all.

SCOPE OF THE READ-ONLY GUARANTEE. `default_transaction_read_only` is a session
DEFAULT, not a security boundary. Idiomatic asyncpg usage cannot escape it —
including `conn.transaction(readonly=False)`, which omits the qualifier rather
than forcing READ WRITE — but raw `BEGIN READ WRITE` or `SET TRANSACTION READ
WRITE` will; that is pinned in
tests/test_pool.py::test_read_only_is_a_session_default_not_a_boundary.

We no longer control every call site. The SQL hatch executes agent-authored
SQL, so `service/sql_guard.py` — not this default — is the PRIMARY write
control; this pool is a second layer behind it. The durable protection remains
the partner granting a role with no write privileges at all, which is the only
one of the three that an unforeseen statement shape cannot walk through.
"""
from __future__ import annotations

import asyncio
import logging
import os
import socket
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


# Client-side ceiling on how long we WAIT for one command.
#
# `statement_timeout` is a SERVER setting: it bounds how long the server will
# WORK, not how long we will wait for the answer. Those two diverge the instant
# the connection dies mid-query -- the server's statement dies with the socket,
# and asyncpg goes on waiting for a reply that will never arrive. Nothing in the
# pool can free it, because a connection stuck mid-command never releases.
#
# Observed 2026-08-03 against the live partner corpus. A bundle resolve issues a
# tax scan that takes 241 s server-side and sends NOTHING on the socket while it
# runs -- precisely what an idle NAT or firewall reaps. The client sat in
# epoll_wait for 43 MINUTES at 0% CPU, with pg_stat_activity showing no active
# statement for our user, and would have sat there indefinitely. In production
# that is a request that never returns and a pool slot that never comes back.
#
# Deliberately set ABOVE statement_timeout so the server's own bound still wins
# in the normal case and this fires only when the REPLY is lost rather than when
# the query is merely slow. asyncpg raises TimeoutError, which the callers that
# already handle QueryCanceledError degrade on identically.
COMMAND_TIMEOUT_GRACE_SECONDS = 30.0


def command_timeout_for(statement_timeout_ms: int) -> float:
    """The client-side wait ceiling implied by a pool's statement timeout."""
    return statement_timeout_ms / 1000 + COMMAND_TIMEOUT_GRACE_SECONDS


# TCP keepalive cadence. Why these exist -- and why command_timeout alone was
# NOT enough, observed twice on 2026-08-03 against the live corpus:
#
# A multi-minute analytical query sends NOTHING on the socket while the server
# works. A NAT/firewall between us and the partner reaps the mapping as idle,
# so the server's eventual reply is lost. command_timeout does fire then, but
# asyncpg's cancellation path waits for the server to confirm on the SAME dead
# socket -- so the connection hangs in cancellation, its pool slot leaks, and
# after max_size losses acquire() blocks forever. Reproduced WITH
# command_timeout set: client in epoll_wait, zero CPU, pg_stat_activity empty.
#
# Keepalives fix the CAUSE rather than the symptom: probes every KEEPALIVE_IDLE
# seconds keep the NAT mapping alive during long server-side work, and if the
# path genuinely dies, KEEPALIVE_COUNT failed probes surface as a socket error
# that unblocks asyncpg instead of an eternal wait. These are SOCKET options,
# not server GUCs -- the RESET ALL hazard that rules out `init=` for session
# settings (see module docstring) does not touch them, so a setup callback is
# the right vehicle.
KEEPALIVE_IDLE_SECONDS = 30
KEEPALIVE_INTERVAL_SECONDS = 10
KEEPALIVE_COUNT = 6


async def _enable_tcp_keepalives(conn: asyncpg.Connection) -> None:
    """Turn on TCP keepalives for one pooled connection's socket."""
    transport = getattr(conn, "_transport", None)
    sock = transport.get_extra_info("socket") if transport is not None else None
    if sock is None:  # e.g. a unix-socket DSN in tests; nothing to keep alive
        return
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, KEEPALIVE_IDLE_SECONDS)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, KEEPALIVE_INTERVAL_SECONDS)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, KEEPALIVE_COUNT)
    except OSError as exc:
        # A socket that refuses the options still works as a connection; log
        # rather than fail the acquire, but loudly -- this is the hang guard.
        logger.warning("could not enable TCP keepalives: %s", exc)


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

        # Startup-packet settings, NOT an init=/setup= callback running `SET`.
        # asyncpg's per-release `RESET ALL` wipes session `SET`s but restores
        # startup-packet values, so only this form survives a release. The
        # module docstring has the measurements. Values must be strings.
        #
        # No belt-and-braces `setup=` re-apply alongside this: it would cost a
        # round trip on every acquire and create a second place the truth can
        # live, and there is no state it would catch that this does not — a
        # `SET` issued *within* an acquire is the SQL hatch's problem
        # (sql_guard refuses VariableSetStmt), and `setup=` runs before the
        # query, so it could not catch that either.
        pool = await asyncpg.create_pool(
            dsn,
            min_size=min_size,
            max_size=max_size,
            # The client-side backstop. Without it a lost reply blocks forever --
            # see command_timeout_for.
            command_timeout=command_timeout_for(statement_timeout_ms),
            # The hang-class guard: keepalives stop the NAT from reaping the
            # socket under a long silent query. See _enable_tcp_keepalives.
            setup=_enable_tcp_keepalives,
            server_settings={
                "statement_timeout": str(int(statement_timeout_ms)),
                "default_transaction_read_only": "on",
            },
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
