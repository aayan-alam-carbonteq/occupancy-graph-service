"""Opt-in smoke against the real partner corpus.

    PARTNER_DSN=... .venv/bin/python -m pytest -m live -q

Skipped, not failed, without credentials -- the calibration and the surface are
both provable against the fixture, and this is the confirmation that the live
corpus behaves as the coverage spec measured it.

1104 Spring Run Rd / 40514 is the address that produced a genuine
absentee-owner signal end to end: the property is in Lexington KY and the
assessor row's owner mails to AURORA, IL.

WHAT THESE ARE FOR. Every assertion here is one the fixture CANNOT make. The
fixture is 20 rows with a hand-written index set that is narrower than
production (tests/fixtures/schema.sql omits ssn, ssn2, phone, mobile, email,
dob, record_id, address_id, the tsv_name GIN index and the trigram indexes), so
several documented access paths plan as seq scans locally and are not exercised
against any index at all. These tests are where those paths are actually
confirmed. They are not decoration: each one fails if a real access path is
broken, and none of them passes vacuously on an empty result.

THREE OF THEM ARE OPEN QUESTIONS, NOT REGRESSION TESTS. They pin an assumption
the shipped code rests on, and their failure is the news:

  * test_no_index_covers_record_id_on_the_records_tables -- settles a
    contradiction between two of our own documents. Read its docstring first
    when credentials arrive; it is the FIRST thing to confirm.
  * test_the_role_physically_cannot_write -- a green run means the partner
    actioned the write-revocation ask. A red run means sql_guard.py is still
    the only control standing between an LLM and a 3.7 TB production database.
  * test_the_pool_safety_settings_survive_connection_reuse -- answers whether
    server_settings still wins when PARTNER_DSN carries its own
    `?options=-c statement_timeout=...`, which nothing local can test.
"""
from __future__ import annotations

import json
import os

import httpx
import pytest
import pytest_asyncio

from occupancy_graph.service import limits
from occupancy_graph.service.app import create_app
from occupancy_graph.service.schema_doc import TABLES
from occupancy_graph.source import search
from occupancy_graph.source.bundle import BundleCache
from occupancy_graph.source.pool import PartnerPool

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not os.environ.get("PARTNER_DSN"), reason="PARTNER_DSN is not set"
    ),
]

SUBJECT_ADDRESS = "1104 Spring Run Rd"
SUBJECT_ZIP = "40514"
SUBJECT_HOUSE_NUMBER = "1104"

# The tables the typed surface reads, for the privilege probe. Names only --
# nothing here writes to any of them.
WRITE_PRIVILEGES = ("INSERT", "UPDATE", "DELETE", "TRUNCATE")
PROBED_TABLES = (
    "public.records_legacy",
    "public.records_partitioned",
    "silver.entity_links",
    "silver.entity_master",
)


@pytest_asyncio.fixture(scope="module", loop_scope="session")
async def live_pool():
    """One pool for the module. Shaped exactly like the production one --
    PartnerPool.from_env() reads the same env vars serve.py does, so a
    misconfigured PARTNER_STATEMENT_TIMEOUT_MS fails here first."""
    pool = await PartnerPool.from_env()
    try:
        yield pool
    finally:
        await pool.close()


@pytest_asyncio.fixture(scope="module", loop_scope="session")
async def live_client(live_pool):
    """The app over the live pool, with ONE shared BundleCache.

    Module-scoped on purpose: resolving the subject address costs 173 ms - 32 s
    on the phase-1 scan plus 613 ms - 53 s on the phase-2 tax scan, and every
    address-scoped test below reads the same bundle. A function-scoped cache
    would re-pay that per test and turn this file into a minutes-long run
    against someone else's production database.
    """
    app = create_app(pool=live_pool, cache=BundleCache(live_pool))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://graph.live") as http:
        yield http


@pytest_asyncio.fixture(scope="module", loop_scope="session")
async def subject_bundle(live_client) -> dict:
    """POST /v1/resolve for the subject address, resolved once."""
    response = await live_client.post(
        "/v1/resolve", json={"address": SUBJECT_ADDRESS, "zip": SUBJECT_ZIP}
    )
    assert response.status_code == 200, response.text
    return response.json()


@pytest_asyncio.fixture(scope="module", loop_scope="session")
async def subject_links(live_pool) -> tuple[str, list[dict]]:
    """(hal_id, entity_links rows) for a real person at the subject address.

    This is the working query from the partner-DB findings spec §11 -- the
    address -> people hop, measured at ~550 ms live. The coverage spec recorded
    44 records_legacy rows at this address, 24 of them linked by `ssn` and 20 by
    `name_house_zip`, so a hal_id MUST come back; an empty result means the
    (zip, house_number) index or silver.entity_links has moved under us.
    """
    sql = """
        WITH at_addr AS (
            SELECT record_id FROM public.records_legacy
            WHERE zip = $1 AND house_number = $2
            LIMIT 10
        )
        SELECT el.hal_id
        FROM silver.entity_links el
        JOIN at_addr a ON el.record_id = a.record_id
        WHERE el.source_table = 'records_legacy'
        LIMIT 1
    """
    async with live_pool.acquire() as conn:
        hal_id = await conn.fetchval(sql, SUBJECT_ZIP, SUBJECT_HOUSE_NUMBER)
    assert hal_id, (
        f"no silver.entity_links row for any record at {SUBJECT_ZIP}/"
        f"{SUBJECT_HOUSE_NUMBER}. The coverage spec measured 44 linked rows "
        "here; either the (zip, house_number) index no longer reaches them or "
        "the partner's entity graph was rebuilt."
    )
    links = await search.records_for_hal_id(live_pool, hal_id)
    assert links, f"entity_links(hal_id) returned nothing for {hal_id}"
    return hal_id, links


# --- the typed surface, against real rows -----------------------------------


async def test_the_subject_address_resolves_with_its_assessor_row(subject_bundle):
    """Phase 1 (zip + address prefix), phase 2 ((upper(state), upper(city)) +
    prefix, the ONLY path to tax), the tax quality gate and the projection, in
    one call. The owner-mails-elsewhere fact is the whole reason this address
    was chosen: it is a real absentee-owner signal, not a synthetic one."""
    body = subject_bundle
    assert body["address_id"] is not None
    assert body["source_counts"]["trace"] > 0
    assert body["source_counts"]["utility"] > 0
    tax = body["records_by_source"]["tax"]["records"]
    assert tax, (
        f"no tax row; tax_timed_out={body['tax_timed_out']}, "
        f"dropped_counts={body['dropped_counts']}. A drop means the "
        "column-shift gate rejected the assessor row; a timeout means the "
        "phase-2 (upper(state), upper(city)) scan exceeded the statement "
        "timeout on a cold cache."
    )
    assert tax[0]["ownerstate"] == "IL"
    assert tax[0]["ownercity"] == "AURORA"
    # Contract B addendum 2: every bundle-sourced record carries the handle
    # GET /v1/source-record takes. Without it the engine cannot cite a row.
    assert tax[0]["__rowid"] == 0


async def test_the_assessor_row_is_citable_back_through_provenance(
    live_client, subject_bundle
):
    """Contract B addenda 1 and 2 together: the `__rowid` the record carried is
    resolvable to the same row via GET /v1/source-record, and the address_id
    query param that scopes it is honoured."""
    address_id = subject_bundle["address_id"]
    row = subject_bundle["records_by_source"]["tax"]["records"][0]
    response = await live_client.get(
        f"/v1/source-record/tax/{row['__rowid']}?address_id={address_id}"
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["source"] == "tax"
    assert body["rowid"] == row["__rowid"]
    assert body["data"]["ownercity"] == "AURORA"
    assert "ownername=" in body["summary"]

    # Without the address the rowid is meaningless, and the service says so
    # rather than guessing a bundle.
    naked = await live_client.get(f"/v1/source-record/tax/{row['__rowid']}")
    assert naked.status_code == 400, naked.text


async def test_the_people_at_the_address_cluster_from_real_rows(
    live_client, subject_bundle
):
    """Name-key clustering over the live bundle. The coverage spec counted 12
    distinct people on the trace feed and 9 on utility at this address, so a
    single-person result would mean the clustering collapsed."""
    address_id = subject_bundle["address_id"]
    response = await live_client.get(f"/v1/address/{address_id}/people?limit=50")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total_count"] >= 2, body
    for person in body["people"]:
        assert person["id"].startswith("addr:")
        assert person["lastname"], person
        assert person["sources"], person
        assert person["primary_address_id"] == address_id

    # And the person view of those same rows resolves. `addr:` ids never touch
    # entity_links, so this can never time out.
    first = body["people"][0]["id"]
    records = await live_client.get(f"/v1/person/{first}/records?limit=5")
    assert records.status_code == 200, records.text
    assert records.json()["records_timed_out"] is False


async def test_the_record_id_hop_returns_the_rows_entity_links_named(
    live_pool, subject_links
):
    """THE UNINDEXED HOP, measured for real.

    entity_links names N (source_table, record_id) pairs for this person. The
    rows behind them are fetched by `record_id` on records_legacy /
    records_partitioned -- the one hop in the typed surface that no measurement
    in any spec covers (every chain the specs timed stops at entity_links; see
    the findings spec §7 and §11, whose address -> people query ends in a
    GROUP BY over entity_links and never joins back to public.records_*).

    The assertion is exact and cannot pass vacuously: if the fetch did NOT time
    out, then the rows the links point at must be there. Empty-and-not-timed-out
    is a silent data loss, which is precisely the failure `records_timed_out`
    exists to make impossible to mistake for "this person has no records".
    """
    _, links = subject_links
    rows, timed_out = await search.rows_for_links(live_pool, links)
    if timed_out:
        pytest.fail(
            f"the record_id hop timed out on {len(links)} links, at the pool's "
            "statement timeout. The service degrades correctly "
            "(records_timed_out=true), so this is not a crash -- it is the "
            "partner ask for an index on records_*(record_id) coming due. See "
            "test_no_index_covers_record_id_on_the_records_tables."
        )
    assert rows, (
        f"entity_links named {len(links)} rows for this person and the fetch "
        "did not time out, yet not one row came back. That is silent data "
        "loss in source/search.py::rows_for_links, not a partner-data fact."
    )


async def test_the_hal_traversal_surfaces_the_confidence_fields(
    live_client, subject_links
):
    """Contract B pins identity_confidence and is_suspicious on every
    `hal:`-sourced person: the partner's ER graph is 17.9% suspicious and never
    applies its own computed merges, so the model must be able to discount it.
    Omitting them would let the engine treat a partner guess as a fact."""
    hal_id, _ = subject_links
    response = await live_client.get(f"/v1/person/hal:{hal_id}/records?limit=10")
    assert response.status_code == 200, response.text
    body = response.json()
    person = body["person"]
    assert person["id"] == f"hal:{hal_id}"
    assert person["identity_confidence"] is not None
    assert isinstance(person["is_suspicious"], bool)
    assert isinstance(body["records_timed_out"], bool)


async def test_a_name_search_finds_the_person_the_traversal_found(
    live_pool, live_client, subject_links
):
    """The entity_master name path, driven with a surname we KNOW is in the
    graph -- taken from the hal_id the address traversal produced -- so it can
    never pass on an empty result the way an arbitrary name could."""
    hal_id, _ = subject_links
    person = await search.person_for_hal_id(live_pool, hal_id)
    assert person is not None
    surname = person["canonical_last_name"]
    assert surname, f"{hal_id} has no canonical_last_name to search by"

    response = await live_client.get(f"/v1/people/search?name={surname}&limit=25")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total_count"] > 0, (
        f"entity_master has {hal_id} with canonical_last_name={surname!r}, but "
        "searching that surname matched nothing. The search predicate is "
        "upper(canonical_last_name) = $1 -- check that index still exists."
    )
    assert body["results"]
    assert body["has_more"] == (len(body["results"]) < body["total_count"])
    for result in body["results"]:
        assert result["id"].startswith("hal:")
        assert "identity_confidence" in result
        assert "is_suspicious" in result


# --- the SQL hatch, against real plans ---------------------------------------


async def test_the_measured_access_path_costs_still_hold(live_client):
    """If the live plan costs have drifted past the ceiling, the hatch would
    start refusing servable queries -- fail here rather than in production.

    The ceiling is read from limits rather than hardcoded so a re-tuned
    SQL_HATCH_MAX_PLAN_COST (docs/explain-cost-calibration.md) is honoured
    instead of silently contradicted.
    """
    response = await live_client.post(
        "/v1/sql",
        json={
            "query": "SELECT record_id, address FROM public.records_partitioned "
                     "WHERE zip = '40514' AND address ILIKE '1104 Spring%'",
            "max_rows": 25,
        },
    )
    assert response.status_code == 200, response.json()
    body = response.json()
    ceiling = limits.max_plan_cost()
    assert body["plan_cost"] < ceiling, (
        f"the documented zip + address-prefix path now plans at "
        f"{body['plan_cost']}, above the {ceiling} ceiling. Re-run "
        "scripts/explain_cost_probe.py and re-derive the ceiling before the "
        "hatch starts refusing queries it is supposed to serve."
    )


async def test_the_explain_gate_refuses_a_real_unindexed_predicate(live_client):
    """The other bound: a predicate on a real but unindexed column must be
    refused with the planner's own reason, not run. `employer` is a documented
    key column on records_legacy with no index on it."""
    response = await live_client.post(
        "/v1/sql",
        json={"query": "SELECT record_id FROM public.records_legacy WHERE employer = 'ACME'"},
    )
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["refused"] is True
    assert body["stage"] == "explain"
    assert "Indexed paths" in body["hint"]


async def test_a_sequence_write_disguised_as_a_select_is_refused(live_client):
    """`SELECT nextval('...')` is ONE SelectStmt with no DML node anywhere in
    its tree, and it commits a write. It is the reason the function denylist in
    sql_guard.py is load-bearing rather than defence in depth, and the reason no
    denylist can ever be proven complete. Refused at parse, before EXPLAIN."""
    response = await live_client.post(
        "/v1/sql", json={"query": "SELECT nextval('public.any_sequence')"}
    )
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["stage"] == "parse"
    assert body["reason"] == "function nextval is not permitted"


# --- the three open questions ------------------------------------------------


async def test_no_index_covers_record_id_on_the_records_tables(live_pool, subject_links):
    """OPEN QUESTION -- CONFIRM THIS FIRST. Two of our own documents disagree.

      * docs/superpowers/specs/2026-07-27-partner-records-db-findings.md §6
        ("Indexes present") lists `record_id` among the indexes on
        `records_legacy` (14 idx) AND on every `records_new` partition (18-19
        idx). That table reads like real introspection: it carries index counts
        and per-table detail (`address_id` and `tsv_name` on the partitions
        only, `(zip, house_number)` on legacy only).
      * The pinned Contract B addendum 3 in the umbrella plan
        (docs/superpowers/plans/2026-07-29-typed-data-service.md, "Contract B
        addenda") states that **no index covers `record_id`**, and raises the
        partner ask to add one.

    The SHIPPED CODE believes the second: source/search.py::rows_for_links
    degrades to `records_timed_out` on this hop, and service/schema_doc.py tells
    the agent "record_id is not indexed on the records tables". This test pins
    that belief, so a FAILURE here is the good news, not a regression:

      -> If it fails, an index DOES exist. Update Contract B addendum 3, the
         rows_for_links docstring, and the schema_doc caveat; drop the partner
         ask for the index; and consider whether `records_timed_out` can stop
         being a degradation path on records_legacy (it still cannot on
         records_partitioned, which is partitioned by imported_at and therefore
         cannot prune on record_id -- an ANY() probe must touch every
         partition's index regardless).

    The catalog answer and the planner's answer are BOTH read, and asserted to
    agree, so neither can be dismissed as a stale view.
    """
    index_sql = """
        SELECT c.relname AS table_name,
               i.relname AS index_name,
               pg_get_indexdef(x.indexrelid) AS indexdef
        FROM pg_index x
        JOIN pg_class c ON c.oid = x.indrelid
        JOIN pg_class i ON i.oid = x.indexrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public'
          AND (c.relname = 'records_legacy' OR c.relname LIKE 'records_partitioned%')
          AND pg_get_indexdef(x.indexrelid) ~ 'USING [a-z]+ \\(record_id'
        ORDER BY 1, 2
    """
    _, links = subject_links
    record_ids = [int(link["record_id"]) for link in links[:10]]
    literal = ", ".join(str(value) for value in record_ids)
    explain_sql = (
        "EXPLAIN (FORMAT JSON) SELECT record_id FROM public.records_legacy "
        f"WHERE record_id = ANY(ARRAY[{literal}]::bigint[])"
    )

    async with live_pool.acquire() as conn:
        indexes = [dict(row) for row in await conn.fetch(index_sql)]
        plan = json.loads(await conn.fetchval(explain_sql))

    def node_types(node):
        yield node.get("Node Type", "")
        for child in node.get("Plans", ()):
            yield from node_types(child)

    types = list(node_types(plan[0]["Plan"]))
    scans = [name for name in types if "Seq Scan" in name]

    assert bool(indexes) is not bool(scans), (
        "the catalog and the planner disagree about record_id on "
        f"records_legacy. Indexes found: {indexes}. Plan node types: {types}. "
        "Resolve that before trusting either half of this test."
    )
    assert not indexes, (
        "GOOD NEWS, AND AN ACTION: an index DOES cover record_id -- "
        f"{indexes}. Contract B addendum 3 and service/schema_doc.py both say "
        "none does, and the partner-DB findings spec §6 said one does all "
        "along. The findings spec was right. Update both, drop the index ask, "
        f"and re-read rows_for_links. Plan node types were {types}."
    )


async def test_the_role_physically_cannot_write(live_pool):
    """OPEN QUESTION. The escalated partner ask, made machine-checkable.

    `SELECT nextval('s')` commits a sequence write from a statement that is
    structurally a plain SELECT: one statement, SelectStmt head, no DML node
    anywhere in its tree. The parser fixed query SHAPE; it cannot fix EFFECTS.
    Therefore no function denylist can ever be proven complete, and a role that
    physically cannot write is the only durable control.

    A GREEN run means the partner actioned that ask. A RED run means
    service/sql_guard.py is, today, the only thing standing between an
    LLM-authored statement and a 3.7 TB third-party production database.

    Reads catalogue functions only -- nothing here attempts a write.
    """
    checks = []
    async with live_pool.acquire() as conn:
        for table in PROBED_TABLES:
            for privilege in WRITE_PRIVILEGES:
                granted = await conn.fetchval(
                    "SELECT has_table_privilege(current_user, $1, $2)", table, privilege
                )
                if granted:
                    checks.append(f"{privilege} on {table}")
        for schema in ("public", "silver"):
            granted = await conn.fetchval(
                "SELECT has_schema_privilege(current_user, $1, 'CREATE')", schema
            )
            if granted:
                checks.append(f"CREATE on schema {schema}")
        granted = await conn.fetchval(
            "SELECT has_database_privilege(current_user, current_database(), 'CREATE')"
        )
        if granted:
            checks.append("CREATE on the database")

    assert not checks, (
        "the partner role still holds write privileges: "
        + ", ".join(checks)
        + ". The write-revocation ask is REQUIRED BEFORE THE HATCH SHIPS, not "
        "tidy: sql_guard.py bounds statement shape, and nextval/setval prove "
        "shape does not bound effects."
    )


async def test_the_pool_safety_settings_survive_connection_reuse():
    """OPEN QUESTION. The pool Critical, re-checked against the real server.

    Both safety settings travel in the startup packet (server_settings) rather
    than a `SET` from an `init=` callback, because asyncpg runs `RESET ALL` on
    every RELEASE and a session `SET` therefore survives exactly one acquire.
    Measured against the fixture with the old form: acquire #1 read-only `on` /
    timeout 20s, acquires #2 and #3 read-only `off` / timeout `0` -- which
    Postgres reads as UNLIMITED -- and a write succeeded.

    THE DISTINGUISHING CONDITION IS REUSE, NOT MULTIPLICITY. The test that
    missed this used four CONCURRENT acquires, which force four DISTINCT
    connections, every one on its first acquire. max_size=1 here forces the
    same physical connection to be reused, which is the only shape that sees it.

    Live, this also answers something no local test can: whether server_settings
    still wins when PARTNER_DSN carries its own `?options=-c statement_timeout=`.
    If it does not, this fails on acquire #1.
    """
    timeout_ms = 20_000
    pool = await PartnerPool.create(
        os.environ["PARTNER_DSN"], statement_timeout_ms=timeout_ms, min_size=1, max_size=1
    )
    try:
        for attempt in range(1, 4):
            async with pool.acquire() as conn:
                read_only = await conn.fetchval("SHOW default_transaction_read_only")
                timeout = await conn.fetchval("SHOW statement_timeout")
            assert read_only == "on", (
                f"acquire #{attempt}: default_transaction_read_only is "
                f"{read_only!r}. Either RESET ALL is clearing it again or the "
                "DSN overrides it."
            )
            assert timeout != "0", (
                f"acquire #{attempt}: statement_timeout is 0, which Postgres "
                "reads as UNLIMITED against a 7.6 B-row corpus."
            )
            # Postgres renders 20000 ms as '20s'; accept either spelling.
            assert timeout in {"20s", f"{timeout_ms}ms"}, (
                f"acquire #{attempt}: statement_timeout is {timeout!r}, not the "
                f"{timeout_ms} ms this pool asked for. Check whether "
                "PARTNER_DSN carries its own `options=-c statement_timeout=`."
            )
    finally:
        await pool.close()


async def test_every_table_the_schema_document_advertises_exists(live_pool):
    """GET /v1/schema is curated, not introspected, so nothing else notices when
    the partner renames or moves a relation. The agent would find out by having
    its query refused with a message that names a table that is not there."""
    async with live_pool.acquire() as conn:
        missing = [
            table["name"]
            for table in TABLES
            if await conn.fetchval("SELECT to_regclass($1)", table["name"]) is None
        ]
    assert not missing, (
        f"GET /v1/schema advertises tables that do not exist: {missing}. "
        "Update service/schema_doc.py -- the agent is being told to query them."
    )
