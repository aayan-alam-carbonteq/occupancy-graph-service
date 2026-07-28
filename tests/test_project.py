from __future__ import annotations

from datetime import date

from occupancy_graph.source.manifest import SHAPES
from occupancy_graph.source.project import project_row

TAX_ROW = {
    "record_id": 4001, "address": "123 MAIN ST", "city": "LEXINGTON", "state": "KY",
    "zip": None, "house_number": None, "first_name": "JANE", "last_name": "DOE",
    "raw_data": {
        "ownerName": "DOE, JANE ANN", "ownerAddressLine1": "777 FAR AWAY DR",
        "ownerCity": "AURORA", "ownerState": "IL", "ownerZipCode": "60504",
        "residential": "True", "condo": "False", "streetNumber": "123",
        "zipCodePlusFour": "40505-1046", "fipsState": "21", "fipsCounty": "067",
        "equityCurrentEstBal": "118158.0", "LTVCurrentEstCombined": "61.0564",
    },
}


def test_raw_keys_are_renamed_to_contract_keys():
    out = project_row(SHAPES["tax"], TAX_ROW)
    assert out["ownername"] == "DOE, JANE ANN"
    assert out["equitycurrentestbal"] == "118158.0"
    assert out["ltvcurrentestcombined"] == "61.0564"


def test_derived_fields_fill_the_gaps_the_columns_leave():
    out = project_row(SHAPES["tax"], TAX_ROW)
    assert out["zip"] == "40505"          # column is NULL; from zipCodePlusFour
    assert out["housenumber"] == "123"    # column is NULL; from streetNumber
    assert out["county"] == "FAYETTE"     # from fipsState + fipsCounty


def test_absent_fields_are_present_as_none_not_missing():
    out = project_row(SHAPES["tax"], TAX_ROW)
    assert "yearbuilt" in out
    assert out["yearbuilt"] is None


def test_every_contract_field_is_present_even_when_empty():
    out = project_row(SHAPES["tax"], TAX_ROW)
    assert set(out) >= set(SHAPES["tax"].columns)


def test_norm_helpers_are_computed():
    out = project_row(SHAPES["tax"], TAX_ROW)
    assert out["__norm_address"] == "123 MAIN ST"
    assert out["__norm_name_key"] == "jane|doe"
    assert out["__norm_address_zip_key"] == "123 MAIN ST|40505"
    assert out["__norm_owneraddressline1"] == "777 FAR AWAY DR"


def test_own_rent_is_normalized_during_projection():
    row = {"record_id": 2002, "own_rent": "own", "address": "456 PINE ST",
           "zip": "40505", "first_name": "John", "last_name": "Smith", "raw_data": {}}
    assert project_row(SHAPES["loan"], row)["own_rent"] == "OWN"


def test_trace_garbage_is_coerced_to_none_but_valid_values_survive():
    row = {"record_id": 1003, "address": "123 MAIN ST", "zip": "40505",
           "first_name": "John", "last_name": "Smith",
           "raw_data": {"Date_Of_Birth_Year": "NOTAYEAR", "Home_Built_Year": "1990"}}
    out = project_row(SHAPES["trace"], row)
    assert out["dob_year"] is None
    assert out["home_built_year"] == "1990"


def test_values_are_stringified_because_the_contract_types_are_all_string():
    row = {"record_id": 1004, "address": "123 MAIN ST", "zip": "40505",
           "first_name": "Jane", "last_name": "Doe", "mortgage_amount": 171,
           "dob": date(1980, 1, 1), "raw_data": {}}
    out = project_row(SHAPES["base"], row)
    assert out["mortgageamountinthousands"] == "171"
    assert out["persondateofbirthyear"] == "1980"
