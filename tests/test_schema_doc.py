"""GET /v1/schema -- curated, NOT raw introspection.

Every caveat here is a measured defect from the coverage spec. Omitting them
would have the model reason over column-shifted owner names and read a load
date as an observation date.

The second half of this file pins the OTHER half of the agent's guidance: the
refusal hint. /v1/schema tells the agent which paths are fast; a 422 refusal
tells it the same thing at the moment it got one wrong. Those were two
hand-maintained lists that already disagreed -- the hint named ssn/phone/email,
the schema document did not -- so they are now one list with the hint generated
from it, and these tests are what keeps that true.
"""
from __future__ import annotations

from occupancy_graph.service import sql_hatch
from occupancy_graph.service.limits import is_records_relation
from occupancy_graph.service.schema_doc import ACCESS_PATHS, HINT

# What Contract C pins, character for character. Asserted against the GENERATED
# string rather than pasted into schema_doc.py as a constant, so adding an
# access path silently rewrites the hint the agent is handed and this test is
# what stops it going out unnoticed.
CONTRACT_C_HINT = (
    "No index supports this predicate. Indexed paths: zip; "
    "(last_name, zip, house_number); (upper(state), upper(city)); ssn; phone; email."
)


def hint_tokens(hint: str) -> list[str]:
    """The index names a refusal advertises, read the way the agent reads them."""
    body = hint.split("Indexed paths:", 1)[1].strip().rstrip(".")
    return [token.strip() for token in body.split(";")]


async def test_the_schema_names_the_tables_that_matter(client):
    response = await client.get("/v1/schema")
    assert response.status_code == 200
    body = response.json()
    names = {table["name"] for table in body["tables"]}
    assert names == {
        "public.records_legacy", "public.records_partitioned",
        "silver.entity_master", "silver.entity_links",
    }
    for table in body["tables"]:
        assert table["purpose"]
        assert table["key_columns"]


async def test_every_access_path_carries_its_measured_cost(client):
    body = (await client.get("/v1/schema")).json()
    predicates = [path["predicate"] for path in body["access_paths"]]
    assert any("zip" in predicate for predicate in predicates)
    assert any("upper(state)" in predicate for predicate in predicates)
    assert any("last_name" in predicate for predicate in predicates)
    assert any("hal_id" in predicate for predicate in predicates)
    for path in body["access_paths"]:
        assert path["index"]
        assert path["measured"]


async def test_the_three_pinned_caveats_are_present_verbatim(client):
    body = (await client.get("/v1/schema")).json()
    for caveat in (
        "house_number and zip are 0% populated on property_owner rows",
        "~17.5% of property_owner rows are column-shifted",
        "imported_at is a load date, not an observation date",
    ):
        assert caveat in body["caveats"]


async def test_the_absent_shapes_are_stated_so_the_model_stops_asking(client):
    body = (await client.get("/v1/schema")).json()
    joined = " ".join(body["caveats"])
    assert "voter" in joined and "criminal" in joined and "linkedin" in joined


async def test_the_hatch_limits_are_advertised(client):
    body = (await client.get("/v1/schema")).json()
    assert body["limits"]["max_rows"] == 500
    assert body["limits"]["max_plan_cost"] == 5000000.0
    assert body["limits"]["statement_timeout_ms"] == 20000


async def test_the_schema_is_not_raw_introspection(client):
    """144 columns dumped without the access paths would guarantee refusals."""
    body = (await client.get("/v1/schema")).json()
    for table in body["tables"]:
        assert len(table["key_columns"]) <= 25


def test_the_generated_hint_is_the_string_contract_c_pins():
    assert HINT == CONTRACT_C_HINT


def test_the_hatch_refuses_with_the_very_string_the_schema_document_builds():
    """Identity, not equality. A maintainer who pastes the text back into
    sql_hatch.py restores exactly the two-list arrangement this replaced, and
    equality would not notice."""
    assert sql_hatch.HINT is HINT


def test_every_records_path_the_schema_documents_is_named_in_the_hint():
    """Both directions, because either gap misleads the agent.

    A hint token with no documented path sends it at an index /v1/schema never
    described; a documented records path missing from the hint means a refusal
    fails to mention a way out that the schema says exists.
    """
    documented = [path["hint_key"] for path in ACCESS_PATHS if path["hint_key"]]
    assert list(dict.fromkeys(documented)) == hint_tokens(HINT)

    unnamed = [
        path["predicate"]
        for path in ACCESS_PATHS
        if is_records_relation(path["table"].split(".")[-1]) and not path["hint_key"]
    ]
    assert unnamed == []


async def test_a_real_refusal_and_a_real_schema_call_tell_the_agent_the_same_thing(
    client, monkeypatch
):
    """End to end over HTTP, which is the only place the agent sees either one.

    The seq-scan ceiling is dropped to 0 -- legal, and the strictest setting --
    because the fixture's 20-row tables cost ~1 to scan and would otherwise
    never be refused.
    """
    monkeypatch.setenv("SQL_HATCH_MAX_RECORDS_SEQSCAN_COST", "0")
    refusal = await client.post(
        "/v1/sql",
        json={"query": "SELECT record_id FROM public.records_legacy WHERE employer = 'ACME'"},
    )
    assert refusal.status_code == 422

    described = (await client.get("/v1/schema")).json()
    documented = [path["hint_key"] for path in described["access_paths"] if path["hint_key"]]
    assert hint_tokens(refusal.json()["hint"]) == list(dict.fromkeys(documented))
