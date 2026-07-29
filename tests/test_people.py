from __future__ import annotations

import pytest

from occupancy_graph.source.bundle import materialize
from occupancy_graph.source.people import PERSON_ID_PREFIX, people_for_bundle
from occupancy_graph.source.pool import PartnerPool


@pytest.fixture
async def bundle(fixture_db):
    pool = await PartnerPool.create(fixture_db, statement_timeout_ms=10_000)
    try:
        yield await materialize(pool, "123 Main St", "40505", address_id=7)
    finally:
        await pool.close()


def test_rows_are_clustered_by_normalized_name_key(bundle):
    people = people_for_bundle(bundle)
    names = {person["norm_name_key"] for person in people}
    assert "jane|doe" in names
    assert "pat|tenant" in names


def test_one_person_per_name_not_one_per_row(bundle):
    people = people_for_bundle(bundle)
    jane = next(p for p in people if p["norm_name_key"] == "jane|doe")
    # Jane appears in trace, base, loan/drive and auto rows at this address.
    assert jane["sources"] >= {"trace", "base", "loan", "auto"}


def test_person_ids_are_address_scoped_and_prefixed(bundle):
    people = people_for_bundle(bundle)
    assert all(p["id"].startswith(f"{PERSON_ID_PREFIX}7:") for p in people)


def test_person_ids_are_stable_across_calls(bundle):
    first = {p["norm_name_key"]: p["id"] for p in people_for_bundle(bundle)}
    second = {p["norm_name_key"]: p["id"] for p in people_for_bundle(bundle)}
    assert first == second


def test_rows_without_a_name_are_skipped(bundle):
    people = people_for_bundle(bundle)
    assert all(p["norm_name_key"] != "|" for p in people)


def test_person_carries_the_bundle_address_as_primary(bundle):
    assert all(p["primary_address_id"] == 7 for p in people_for_bundle(bundle))
