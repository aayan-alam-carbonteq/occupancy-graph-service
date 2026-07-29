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
    assert query.like_prefix == "1104 Spring Run%"


def test_address_query_without_a_house_number_falls_back_to_the_whole_string():
    query = AddressQuery.build("Esther St", "02920")
    assert query.like_prefix == "Esther St%"
