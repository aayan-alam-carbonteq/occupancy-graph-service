"""Startup assertion that the address indexes this service depends on exist.

WHY THIS EXISTS. Every address predicate in resolve.py is written to match an
EXPRESSION index the PARTNER owns, on a database we hold a read-only guest role
on. We cannot create these indexes, we are not told when they change, and the
partner has re-architected large parts of this schema inside ten days before
(the entire `silver` layer was rebuilt between 2026-07-27 and 2026-08-07).

The failure mode without this check is silent and expensive rather than loud.
If `idx_records_legacy_zip_normaddr` disappears, nothing errors: the predicate
is still VALID SQL, so Postgres simply falls back to the `zip` btree and heap-
filters the address over every row in the ZIP -- ~273k scattered reads at ~195 ms
a cold page on a 3.7 TB heap. Measured, that query does not finish: observed
server-side ACTIVE at 14+ minutes, and cancelled at our own 120 s ceiling. In
production it presents as investigations that hang and time out one by one,
with the cause three layers down in a query plan nobody is looking at.

There is no fallback path to degrade onto -- the resident hop that used to
serve records_legacy was deleted when these indexes landed, because it cost
~0.35 measured accuracy. So the honest response to a missing index is to refuse
to start: a container that fails its healthcheck with the index name in the log
is diagnosable in seconds, and the orchestrator will not route traffic to it.

CHECKED AT STARTUP, NOT PER QUERY. `pg_index` is catalog state that changes on
a DDL timescale, not a request timescale, and this runs inside the lifespan
before the first request is served. A per-query check would add a catalog round
trip to every scan to defend against something that cannot change mid-request.
"""
from __future__ import annotations

import logging

from occupancy_graph.source.pool import PartnerPool

logger = logging.getLogger(__name__)

# (index name, relation, what breaks without it). Relation is carried so the
# error names it -- "idx_records_legacy_zip_normaddr" alone does not tell an
# on-call engineer which access path just died.
REQUIRED_INDEXES: tuple[tuple[str, str, str], ...] = (
    (
        "idx_records_legacy_zip_normaddr",
        "public.records_legacy",
        "phase-1 address scan on the 6.24 B-row legacy corpus",
    ),
    (
        "idx_records_new_zip_normaddr",
        "public.records_new",
        "phase-1 address scan on the partitioned corpus",
    ),
    (
        "idx_p20260301_property_owner_addr",
        "public.records_partitioned_p20260301",
        "phase-2 assessor/tax lookup, the only path to property_owner rows",
    ),
)

# The function the indexes are built on. Checked separately and FIRST: if it is
# gone the indexes cannot exist either, and "s5_street_norm is missing" is a far
# more useful message than three index errors that share one cause.
REQUIRED_FUNCTION = "silver.s5_street_norm"


class MissingIndexError(RuntimeError):
    """Raised in the lifespan, so the container never passes its healthcheck."""


# The collation the name-search range scan depends on.
#
# search.py resolves a name to hal_ids with `key_value >= 'LAST|FIRST|' AND
# key_value < 'LAST|FIRST}'`, which is only equivalent to a prefix match when
# comparison is BYTEWISE. Under a linguistic collation (en_US.utf8 and friends)
# punctuation is reordered and the upper bound stops bounding what we think:
# `'DOE|JANE|1980-04-01' < 'DOE|JANE}'` is TRUE under C.UTF-8 and FALSE under
# en_US.utf8. The failure is silent -- no error, just an empty result for a name
# that exists -- which is why it is checked here rather than left to surface as
# "search found nobody". Caught exactly this way when the test fixture, which
# defaulted to en_US.utf8, disagreed with production.
EXPECTED_COLLATION = "C.UTF-8"


async def verify_address_indexes(pool: PartnerPool) -> None:
    """Raise MissingIndexError unless every required index exists and is valid.

    `indisvalid` is checked, not merely presence: a CREATE INDEX CONCURRENTLY
    that fails partway leaves an INVALID index in the catalog. It is visible in
    pg_indexes and completely ignored by the planner, which is precisely the
    silent-degradation case this function exists to catch.
    """
    async with pool.acquire() as conn:
        function_exists = await conn.fetchval(
            """
            SELECT EXISTS (
                SELECT 1 FROM pg_proc p
                JOIN pg_namespace n ON n.oid = p.pronamespace
                WHERE n.nspname = 'silver' AND p.proname = 's5_street_norm'
            )
            """
        )
        if not function_exists:
            raise MissingIndexError(
                f"{REQUIRED_FUNCTION}(text) does not exist on the partner "
                "database. Every address predicate this service emits is built "
                "on it, and all three address indexes are expression indexes "
                "over it, so none of them can exist either. This is a partner-"
                "side schema change: raise it with them before restarting."
            )

        # Matched on (schema, name, RELATION) and the DEFINITION is returned, not
        # just the name. A name-only check is what the clone-parity suite already
        # refuses to accept -- tests/clone/test_ddl_matches_production.py says
        # outright that "an index on the bare column would be silently unusable
        # while every name-based assertion passed" -- and the production guard
        # has no more reason to trust a name than the clone does. The partner
        # rebuilt their entire silver layer inside ten days; an index recreated
        # on `address` instead of `s5_street_norm(address)`, or in another
        # schema, keeps the name and loses every property we depend on.
        rows = await conn.fetch(
            """
            SELECT c.relname AS index_name,
                   i.indisvalid,
                   pg_get_indexdef(i.indexrelid) AS indexdef
            FROM pg_index i
            JOIN pg_class c ON c.oid = i.indexrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public'
              AND c.relname = ANY($1::text[])
              AND i.indrelid = ANY($2::regclass[])
            """,
            [name for name, _, _ in REQUIRED_INDEXES],
            [relation for _, relation, _ in REQUIRED_INDEXES],
        )

    found = {row["index_name"]: row for row in rows}
    problems: list[str] = []
    for name, relation, purpose in REQUIRED_INDEXES:
        row = found.get(name)
        if row is None:
            problems.append(f"  {name} on {relation} -- MISSING. Breaks: {purpose}.")
            continue
        if not row["indisvalid"]:
            problems.append(
                f"  {name} on {relation} -- present but INVALID (an interrupted "
                f"CREATE INDEX CONCURRENTLY leaves this state; the planner "
                f"ignores it). Breaks: {purpose}."
            )
            continue
        # The two properties the access path actually rests on. Without the
        # expression the predicate cannot use the index at all; without the
        # opclass the prefix LIKE degrades from an index condition to a filter,
        # which is the difference between 554 ms and a scan that does not finish.
        indexdef = row["indexdef"] or ""
        for required, why in (
            (REQUIRED_FUNCTION.split(".")[-1],
             "not built on s5_street_norm(address) -- our predicate cannot use it"),
            ("text_pattern_ops",
             "missing text_pattern_ops -- the prefix LIKE stops being an index condition"),
        ):
            if required not in indexdef:
                problems.append(
                    f"  {name} on {relation} -- {why}. Breaks: {purpose}.\n"
                    f"      definition: {indexdef}"
                )

    if problems:
        raise MissingIndexError(
            "The partner database is missing address indexes this service "
            "requires:\n"
            + "\n".join(problems)
            + "\n\nThese are partner-owned and we cannot create them (read-only "
            "role). Without them the address predicates remain valid SQL but "
            "degenerate into full-ZIP heap scans that do not finish (observed "
            "active at 14+ minutes), so serving would mean hanging on every "
            "investigation. Refusing to start instead. Re-run "
            "docs/superpowers/specs/2026-08-11-live-partner-db-cutover-design.md "
            "§ verification against the DSN to confirm, then raise with the "
            "partner."
        )

    logger.info(
        "address index preflight passed: %s",
        ", ".join(name for name, _, _ in REQUIRED_INDEXES),
    )
    await verify_collation(pool)


async def verify_collation(pool: PartnerPool) -> None:
    """Warn if the database collation is not the one the range scans assume.

    A WARNING, not a refusal, and the asymmetry is deliberate. A missing index
    makes queries hang, which takes the service down either way, so refusing to
    start costs nothing. A collation change makes name search return empty --
    degraded, but every other operation still works, and taking the whole
    service offline over one path would be the larger outage. The log line is
    what turns "search finds nobody" from a mystery into a one-line diagnosis.
    """
    async with pool.acquire() as conn:
        collation = await conn.fetchval(
            "SELECT datcollate FROM pg_database WHERE datname = current_database()"
        )
    if collation != EXPECTED_COLLATION:
        logger.warning(
            "database collation is %r, expected %r. Name search resolves through a "
            "byte-range over silver.unique_keys and a linguistic collation reorders "
            "the punctuation those bounds rely on, so GET /v1/people/search may "
            "return empty for names that exist. Nothing else is affected. See "
            "source/search.py::_prefix_upper_bound.",
            collation, EXPECTED_COLLATION,
        )
    else:
        logger.info("collation preflight passed: %s", collation)
