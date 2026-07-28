from __future__ import annotations

import pytest

from occupancy_graph.source import derive


def test_tax_zip5_reads_zip_code_plus_four_because_the_column_is_empty():
    row = {"zip": None, "raw_data": {"zipCodePlusFour": "40505-1046"}}
    assert derive.tax_zip5(row) == "40505"


def test_tax_zip5_returns_none_when_shifted_to_a_boolean():
    row = {"zip": None, "raw_data": {"zipCodePlusFour": "True"}}
    assert derive.tax_zip5(row) is None


def test_fips_county_maps_state_and_county_codes_to_a_name():
    row = {"raw_data": {"fipsState": "21", "fipsCounty": "067"}}
    assert derive.fips_county(row) == "FAYETTE"


def test_fips_county_is_none_for_an_unknown_code():
    assert derive.fips_county({"raw_data": {"fipsState": "99", "fipsCounty": "999"}}) is None


@pytest.mark.parametrize(
    "owner_name",
    ["ACME HOLDINGS LLC", "Smith Family Trust", "BETA PROPERTIES", "GAMMA CORP", "ESTATE OF J DOE"],
)
def test_company_from_owner_name_detects_entity_owners(owner_name):
    assert derive.company_from_owner_name({"raw_data": {"ownerName": owner_name}}) == owner_name


@pytest.mark.parametrize("owner_name", ["DOE, JANE ANN", "DOE, JANE; DOE, JOHN", "SMITH, ROBERT"])
def test_company_from_owner_name_is_none_for_natural_persons(owner_name):
    assert derive.company_from_owner_name({"raw_data": {"ownerName": owner_name}}) is None


def test_company_from_owner_name_detects_tr_abbreviation_for_trust():
    """TR is the standard county-assessor abbreviation for TRUST and appears
    throughout real property records — a genuine coverage gap fixed after an
    earlier version's token set had a dead "L L C" entry (unreachable because
    the tokenizer splits on non-alpha, so a multi-word entry can never match)
    and no "TR" entry at all."""
    owner_name = "JONES REVOCABLE TR"
    assert derive.company_from_owner_name({"raw_data": {"ownerName": owner_name}}) == owner_name


def test_known_false_positive_person_surnamed_church_is_flagged():
    """Pinned deliberately: CHURCH is a company token, so a person surnamed
    Church is misdetected as an entity owner. This is a known, accepted limit
    of the heuristic (see company_from_owner_name's docstring) — the consuming
    engine still sees the raw ownerName. If this ever starts returning None,
    that must be a deliberate decision, not an accident."""
    owner_name = "CHURCH, MARY"
    assert derive.company_from_owner_name({"raw_data": {"ownerName": owner_name}}) == owner_name


def test_company_tokens_are_all_single_alphabetic_words():
    """The tokenizer splits ownerName on any non-alpha character, so a
    multi-word or punctuated entry in _COMPANY_TOKENS would be unreachable
    (this is exactly how the old "L L C" entry went dead)."""
    assert all(t.isalpha() for t in derive._COMPANY_TOKENS)


def test_house_number_parses_the_leading_number_from_free_text():
    assert derive.house_number({"address": "1104 Spring Run Rd"}) == "1104"
    assert derive.house_number({"address": "ESTHER ST"}) is None


def test_dob_parts_split_a_date():
    row = {"dob": __import__("datetime").date(1980, 1, 2)}
    assert derive.dob_year(row) == "1980"
    assert derive.dob_month(row) == "1"
    assert derive.dob_day(row) == "2"


def test_dob_parts_are_none_when_dob_is_missing():
    assert derive.dob_year({"dob": None}) is None


def test_year_of_returns_the_year_component():
    d = __import__("datetime").date(2018, 6, 27)
    assert derive.year_of("home_purchase_date")({"home_purchase_date": d}) == "2018"


def test_synthetic_id_is_the_record_id_as_a_string():
    assert derive.synthetic_id({"record_id": 4001}) == "4001"
