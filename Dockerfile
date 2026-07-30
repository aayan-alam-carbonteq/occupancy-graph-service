FROM python:3.14-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
# No build toolchain on purpose. The only dependency that could need one is
# pglast (it wraps libpg_query, which is C), and it publishes prebuilt cp314
# wheels for manylinux_2_17/2_28 and musllinux_1_2 on x86_64 and aarch64 -- this
# base resolves the manylinux one. Verified by building this image rather than
# assumed: adding gcc "just in case" would put a compiler in a production image
# for nothing.
RUN pip install --no-cache-dir .
EXPOSE 8000
# PARTNER_DSN (and optionally PARTNER_STATEMENT_TIMEOUT_MS, PARTNER_POOL_MIN/MAX,
# SQL_HATCH_*) come from the environment. There is no mounted database any more.
#
# ONE uvicorn process per container, and no --workers flag to change that. The
# AddressBundle cache is per-process, so a second worker would not share it --
# it would re-pay the 173 ms - 32 s address scan into a cache nothing else can
# read, which is the exact cost the cache exists to remove. Every path in this
# service is async I/O against asyncpg, so one event loop keeps the pool busy.
# Scale with more containers against the same Postgres, not more workers.
CMD ["occupancy-graph-serve", "--host", "0.0.0.0", "--port", "8000"]
