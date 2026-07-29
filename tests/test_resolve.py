from __future__ import annotations

import pytest

from occupancy_graph.source import resolve as resolve_module
from occupancy_graph.source.pool import PartnerPool
from occupancy_graph.source.resolve import AddressQuery, scan_tax_source, scan_zip_sources


@pytest.fixture
async def pool(fixture_db):
    pool = await PartnerPool.create(fixture_db, statement_timeout_ms=10_000)
    yield pool
    await pool.close()


async def test_scan_finds_every_zip_indexed_shape_at_the_subject_address(pool):
    result = await scan_zip_sources(pool, AddressQuery.build("123 Main St", "40505"))
    assert set(result.rows_by_shape) == {"utility", "trace", "base", "loan", "drive", "auto"}
    assert len(result.rows_by_shape["utility"]) == 1
    assert len(result.rows_by_shape["trace"]) == 2
    assert len(result.rows_by_shape["auto"]) == 2
    assert len(result.rows_by_shape["loan"]) == 1
    assert len(result.rows_by_shape["drive"]) == 1   # only 2001 has a dl_number


async def test_scan_learns_city_and_state_for_the_phase_two_lookup(pool):
    result = await scan_zip_sources(pool, AddressQuery.build("123 Main St", "40505"))
    assert result.city == "LEXINGTON"
    assert result.state == "KY"


async def test_scan_of_an_unknown_address_returns_empty_without_city_or_state(pool):
    result = await scan_zip_sources(pool, AddressQuery.build("999 Nowhere Rd", "40505"))
    assert all(rows == [] for rows in result.rows_by_shape.values())
    assert result.city is None


def test_address_query_normalizes_and_extracts_the_prefix():
    query = AddressQuery.build("1104 Spring Run Road", "40514-1046")
    assert query.norm_address == "1104 SPRING RUN RD"
    assert query.zip5 == "40514"
    assert query.like_prefix == "1104 Spring%"


def test_address_query_without_a_house_number_falls_back_to_the_whole_string():
    query = AddressQuery.build("Esther St", "02920")
    assert query.like_prefix == "Esther St%"


def test_address_query_prefix_excludes_the_unit_designator():
    # A unit designator ("Apt 4") must never enter the prefix: a stored row
    # may omit it, abbreviate it differently, or place it elsewhere, so
    # including it risks silently losing rows rather than merely being less
    # selective.
    query = AddressQuery.build("123 Main St Apt 4", "40505")
    assert query.like_prefix == "123 Main%"


def test_address_query_recognizes_an_alphanumeric_house_number():
    query = AddressQuery.build("12A Oak Ct", "40505")
    assert query.like_prefix == "12A Oak%"


async def test_scan_of_an_empty_address_returns_empty_without_querying(pool):
    result = await scan_zip_sources(pool, AddressQuery.build("", "40505"))
    assert all(rows == [] for rows in result.rows_by_shape.values())
    assert result.city is None


# --- Review fix 1: MAX_ROWS_PER_SHAPE is a per-SHAPE budget, not per-table ---


async def test_scan_truncates_a_multi_table_shape_to_the_shape_budget(monkeypatch):
    """`base` is the only shape reading two tables (records_legacy,
    records_partitioned). _scan_one applies MAX_ROWS_PER_SHAPE per table, so
    without a post-loop truncation `base` would return double everyone else's
    budget. _scan_one is stubbed so this exercises the two-table combination
    deterministically, without inserting hundreds of rows into the shared
    fixture or depending on real network/DB state."""
    monkeypatch.setattr(resolve_module, "MAX_ROWS_PER_SHAPE", 1)

    async def _fake_scan_one(pool, table, shape, query):
        # Simulate every table hitting its own (patched) per-table LIMIT.
        return [{"table": table}]

    monkeypatch.setattr(resolve_module, "_scan_one", _fake_scan_one)

    result = await scan_zip_sources(None, AddressQuery.build("123 Main St", "40505"))
    assert len(result.rows_by_shape["base"]) == 1


# --- Review fix 2: a _decode regression must be caught, not silently pass ---


async def test_scan_decodes_raw_data_from_a_json_string_into_a_dict(pool):
    result = await scan_zip_sources(pool, AddressQuery.build("123 Main St", "40505"))
    trace_row = next(row for row in result.rows_by_shape["trace"] if row["record_id"] == 1002)
    assert isinstance(trace_row["raw_data"], dict)
    assert trace_row["raw_data"]["Record_Date"] == "20240115"


# --- Review fix 3: city/state are a majority vote, not first-non-null ---


async def test_scan_majority_vote_prefers_the_common_city_over_a_stray_row(monkeypatch):
    """The address prefix is deliberately loose ("123 Main%" also matches
    "123 MAINLINE RD"), so a different street in the same ZIP can enter the
    candidate set, and conn.fetch() has no ORDER BY. First-non-null would pick
    whichever row happened to come back first; majority vote must not.
    _scan_one is stubbed (rather than seeding the interfering row into the
    shared, session-scoped fixture) so this can't perturb the exact
    per-shape row counts other tests assert against that same corpus."""

    async def _fake_scan_one(pool, table, shape, query):
        if shape == "utility":
            return [{"city": "LEXINGTON", "state": "KY"}, {"city": "LEXINGTON", "state": "KY"}]
        if shape == "trace":
            return [{"city": "OTHERCITY", "state": "KY"}]
        return []

    monkeypatch.setattr(resolve_module, "_scan_one", _fake_scan_one)

    result = await scan_zip_sources(None, AddressQuery.build("123 Main St", "40505"))
    assert result.city == "LEXINGTON"


async def test_scan_city_vote_tie_break_is_alphabetical_and_deterministic(monkeypatch):
    async def _fake_scan_one(pool, table, shape, query):
        if shape == "utility":
            return [{"city": "ZZZTOWN", "state": "KY"}]
        if shape == "trace":
            return [{"city": "AAATOWN", "state": "KY"}]
        return []

    monkeypatch.setattr(resolve_module, "_scan_one", _fake_scan_one)

    query = AddressQuery.build("123 Main St", "40505")
    results = [(await scan_zip_sources(None, query)).city for _ in range(5)]
    assert results == ["AAATOWN"] * 5


# --- Task 13: phase-2 property_owner scan ---


async def test_tax_scan_finds_the_property_owner_row(pool):
    query = AddressQuery.build("123 Main St", "40505")
    result = await scan_tax_source(pool, query, city="LEXINGTON", state="KY")
    assert result.timed_out is False
    ids = {row["record_id"] for row in result.rows}
    assert 4001 in ids


async def test_tax_scan_drops_column_shifted_rows_and_counts_them(pool):
    query = AddressQuery.build("123 Main St", "40505")
    result = await scan_tax_source(pool, query, city="LEXINGTON", state="KY")
    ids = {row["record_id"] for row in result.rows}
    assert 4003 not in ids, "column-shifted row must be dropped"
    assert result.dropped == 1


async def test_tax_scan_without_city_or_state_returns_empty_not_an_error(pool):
    query = AddressQuery.build("123 Main St", "40505")
    result = await scan_tax_source(pool, query, city=None, state=None)
    assert result.rows == []
    assert result.timed_out is False


async def test_tax_scan_reports_a_timeout_instead_of_raising(fixture_db):
    pool = await PartnerPool.create(fixture_db, statement_timeout_ms=1)
    try:
        query = AddressQuery.build("123 Main St", "40505")
        result = await scan_tax_source(pool, query, city="LEXINGTON", state="KY")
    finally:
        await pool.close()
    assert result.timed_out is True
    assert result.rows == []
