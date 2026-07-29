"""silver.entity_master / entity_links access.

people.py deliberately does NOT use this graph for the address view -- it is
17.9% is_suspicious, 45% singletons, and never applies its computed merges.
It is used here because for search and for owner-elsewhere traversal the
alternative is nothing at all, and every result carries identity_confidence
and is_suspicious so the model can discount it.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import asyncpg
import pytest

from occupancy_graph.source import search
from occupancy_graph.source.pool import PartnerPool


@pytest.fixture
async def pool(fixture_db):
    pool = await PartnerPool.create(fixture_db, statement_timeout_ms=10_000)
    yield pool
    await pool.close()


async def test_search_by_full_name_returns_the_entity_with_its_er_metadata(pool):
    total, results = await search.search_people(pool, "Jane Doe", limit=10)
    assert total == 1
    person = results[0]
    assert person["hal_id"] == "HAL0001"
    assert person["canonical_first_name"] == "JANE"
    assert person["identity_confidence"] == 40.5
    assert person["is_suspicious"] is False
    assert person["record_count"] == 3
    assert person["match_score"] == 1.0


async def test_search_by_last_name_alone_scores_lower(pool):
    total, results = await search.search_people(pool, "Smith", limit=10)
    assert total == 1
    assert results[0]["hal_id"] == "HAL0002"
    assert results[0]["match_score"] == 0.6


async def test_a_suspicious_entity_is_flagged_not_hidden(pool):
    _, results = await search.search_people(pool, "John Smith", limit=10)
    assert results[0]["is_suspicious"] is True
    assert results[0]["identity_confidence"] == 88.0


async def test_search_reports_the_true_total_even_when_the_page_is_capped(pool):
    """The seed carries two non-merged DOE entities precisely so `total` (2) and
    `len(results)` (1) differ. With a single DOE this assertion could not tell a
    real count(*) OVER () from `len(rows)`, and the paging contract -- the
    consumer needs to know a page is partial -- would be untested."""
    total, results = await search.search_people(pool, "Doe", limit=1)
    assert total == 2
    assert len(results) == 1
    # ORDER BY record_count DESC pins which one the single-row page holds.
    assert results[0]["hal_id"] == "HAL0001"


async def test_a_merged_entity_is_never_returned_by_search(pool):
    """The graph never APPLIES its computed merges -- both sides of a merge stay
    in entity_master -- so `is_merged` is the only thing keeping a superseded
    duplicate out of the results. HAL0004 is a merged DOE: it must be invisible
    to a surname search and to a search for its own full name."""
    total, results = await search.search_people(pool, "Doe", limit=10)
    assert total == 2
    assert [person["hal_id"] for person in results] == ["HAL0001", "HAL0003"]
    assert await search.search_people(pool, "Mary Doe", limit=10) == (0, [])


async def test_an_unknown_name_returns_nothing_rather_than_raising(pool):
    assert await search.search_people(pool, "Nobody Here", limit=10) == (0, [])
    assert await search.search_people(pool, "", limit=10) == (0, [])


async def test_person_for_hal_id_returns_the_entity(pool):
    person = await search.person_for_hal_id(pool, "HAL0001")
    assert person["canonical_last_name"] == "DOE"
    assert person["identity_confidence"] == 40.5
    assert await search.person_for_hal_id(pool, "HAL9999") is None


async def test_records_for_hal_id_returns_every_link_highest_confidence_first(pool):
    links = await search.records_for_hal_id(pool, "HAL0001", limit=50)
    assert [(link["source_table"], link["record_id"]) for link in links] == [
        # 2010 (the owner-elsewhere payday row) is seeded at confidence 0.95, above
        # every other HAL0001 link, so it leads. 1002 and 2001 tie at 0.90 and are
        # broken by source_table ASC; 1004 trails at 0.85.
        ("records_partitioned", 2010),
        ("records_legacy", 1002), ("records_new", 2001), ("records_legacy", 1004),
    ]


async def test_rows_for_links_fetches_across_both_physical_tables(pool):
    links = await search.records_for_hal_id(pool, "HAL0001", limit=50)
    rows, timed_out = await search.rows_for_links(pool, links)
    assert timed_out is False
    assert {row["record_id"] for row in rows} == {1002, 1004, 2001, 2010}
    # raw_data must arrive decoded, exactly as the address scan delivers it.
    payday = next(row for row in rows if row["record_id"] == 2001)
    assert payday["raw_data"]["loan_amount"] == "500"


async def test_an_unknown_source_table_is_refused_not_interpolated(pool):
    with pytest.raises(ValueError, match="source_table"):
        await search.rows_for_links(pool, [{"source_table": "pg_class; DROP", "record_id": 1}])


async def test_rows_for_links_reports_a_timeout_instead_of_raising(fixture_db):
    """SMOKE TEST -- NOT the timeout guarantee. Read this before trusting it.

    Its value is that it is the only test driving a genuine Postgres
    QueryCanceledError through asyncpg rather than a hand-rolled double, which
    has caught real bugs. But the `if timed_out:` guard means that on a fast
    machine, or once records_*(record_id) is indexed, it may assert NOTHING: the
    fixture tables hold ~20 rows and a 1 ms statement_timeout is a race, not a
    guarantee. (Measured 20/20 cancellations on the dev box -- still a race.)
    It also cannot catch a partial-leak regression, since a single table has no
    earlier rows to leak.

    The deterministic coverage of the ([], True) contract lives in
    test_a_cancelled_row_fetch_degrades_to_empty_rather_than_raising and
    test_a_timeout_on_the_second_table_does_not_leak_the_first_tables_rows.
    """
    tiny = await PartnerPool.create(fixture_db, statement_timeout_ms=1)
    try:
        links = [{"source_table": "records_legacy", "record_id": n} for n in range(1000, 1200)]
        rows, timed_out = await search.rows_for_links(tiny, links)
    finally:
        await tiny.close()
    assert isinstance(timed_out, bool)
    if timed_out:
        assert rows == []


# --- The degradation path, made deterministic -------------------------------
#
# The test above cannot be trusted to exercise anything: the fixture tables hold
# ~20 rows, so a 1 ms statement_timeout usually finishes in time and `if
# timed_out:` is skipped. The two tests below pin the ([], True) contract by
# injecting the cancellation instead of racing for it. An empty result mistaken
# for "this person has no records" is a failure mode this repo has already been
# bitten by twice (TaxScanResult.queried_city, tax_timed_out).


class _ScriptedPool:
    """Stands in for PartnerPool. `rows_for_links` touches exactly two things --
    `pool.acquire()` as an async context manager and `conn.fetch(sql, ids)` --
    so this is the whole surface, not a re-implementation of it."""

    def __init__(self, script):
        self._script = list(script)
        self.calls: list[tuple[str, tuple]] = []

    @asynccontextmanager
    async def acquire(self):
        yield self

    async def fetch(self, sql, *args):
        self.calls.append((sql, args))
        step = self._script.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step


def _cancelled() -> asyncpg.QueryCanceledError:
    return asyncpg.QueryCanceledError("canceling statement due to statement timeout")


async def test_a_cancelled_row_fetch_degrades_to_empty_rather_than_raising():
    pool = _ScriptedPool([_cancelled()])
    result = await search.rows_for_links(
        pool, [{"source_table": "records_legacy", "record_id": 1002}]
    )
    assert result == ([], True)
    assert len(pool.calls) == 1


async def test_a_timeout_on_the_second_table_does_not_leak_the_first_tables_rows():
    """rows_for_links fetches one physical table at a time. A partial set
    returned with timed_out=True would read downstream as a complete answer for
    the tables that did respond; the contract is all-or-nothing."""
    pool = _ScriptedPool([
        [{"record_id": 1002, "raw_data": '{"Record_Date": "20240115"}'}],
        _cancelled(),
    ])
    rows, timed_out = await search.rows_for_links(pool, [
        {"source_table": "records_legacy", "record_id": 1002},
        {"source_table": "records_new", "record_id": 2001},
    ])
    assert timed_out is True
    assert rows == []
    # Both tables really were attempted -- the first one succeeded and its row
    # was still discarded, which is the whole point of this assertion.
    assert len(pool.calls) == 2
    assert "records_legacy" in pool.calls[0][0]
    assert "records_new" in pool.calls[1][0]


# --- One physical row reached two ways --------------------------------------
#
# schema.sql:66 makes records_new an unfiltered view over records_partitioned,
# so entity_links can name a single physical row under two source_table values.
# The fixture's own links only use records_legacy and records_new, so the
# collision has to be injected.


async def test_the_same_physical_row_reached_through_the_view_and_its_base_table_is_returned_once(
    caplog,
):
    """A doubled row does not raise -- it inflates a per-person record count
    that feeds a downstream score, which is the same class of silent-wrong as
    an empty result read as "no records"."""
    row = {"record_id": 2001, "raw_data": '{"loan_amount": "500"}'}
    pool = _ScriptedPool([[dict(row)], [dict(row)]])
    with caplog.at_level(logging.WARNING, logger="occupancy_graph.source.search"):
        rows, timed_out = await search.rows_for_links(pool, [
            {"source_table": "records_new", "record_id": 2001},
            {"source_table": "records_partitioned", "record_id": 2001},
        ])
    assert timed_out is False
    assert [r["record_id"] for r in rows] == [2001]
    # First occurrence wins, and raw_data is still decoded on the kept copy.
    assert rows[0]["raw_data"]["loan_amount"] == "500"
    # Each relation was queried separately: the dedup is over the RESULTS, not a
    # merged query bucket that would assume the view is an unfiltered passthrough.
    assert len(pool.calls) == 2
    assert "public.records_new" in pool.calls[0][0]
    assert "public.records_partitioned" in pool.calls[1][0]
    # The drop must be observable -- an inert guard teaches us nothing about
    # whether the partner really emits both link kinds for one row.
    assert "records_new" in caplog.text and "records_partitioned" in caplog.text
    assert "2001" in caplog.text


async def test_the_same_record_id_in_a_different_storage_family_is_not_deduped():
    """record_id is unique only WITHIN a corpus. records_legacy is a separate
    corpus from the partitioned tables and can hold an unrelated row with the
    same record_id, so deduping on record_id alone would silently drop a real
    record -- the same failure it is meant to prevent, pointed the other way."""
    pool = _ScriptedPool([
        [{"record_id": 1002, "raw_data": "{}"}],
        [{"record_id": 1002, "raw_data": "{}"}],
    ])
    rows, timed_out = await search.rows_for_links(pool, [
        {"source_table": "records_legacy", "record_id": 1002},
        {"source_table": "records_new", "record_id": 1002},
    ])
    assert timed_out is False
    assert [r["record_id"] for r in rows] == [1002, 1002]
