from __future__ import annotations

import pytest

from occupancy_graph.source.pool import PartnerPool
from occupancy_graph.source.resolve import AddressQuery, scan_zip_sources


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
