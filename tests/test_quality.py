from __future__ import annotations

import pytest

from occupancy_graph.source import quality

CLEAN_TAX_RAW = {
    "streetNumber": "123", "zipCodePlusFour": "40505-1046", "residential": "True",
    "ownerName": "DOE, JANE ANN",
}
SHIFTED_TAX_RAW = {
    "streetNumber": "False", "zipCodePlusFour": "True",
    "residential": "'coordinates': [-71.45", "ownerName": "PENGROVE ST",
}


def test_clean_property_owner_row_is_accepted():
    assert quality.tax_row_is_usable({"raw_data": CLEAN_TAX_RAW}) is True


def test_column_shifted_property_owner_row_is_rejected():
    assert quality.tax_row_is_usable({"raw_data": SHIFTED_TAX_RAW}) is False


@pytest.mark.parametrize(
    "bad",
    [
        {**CLEAN_TAX_RAW, "streetNumber": "True"},
        {**CLEAN_TAX_RAW, "zipCodePlusFour": "True"},
        {**CLEAN_TAX_RAW, "residential": "'coordinates': [1"},
    ],
)
def test_any_single_shift_indicator_rejects_the_row(bad):
    assert quality.tax_row_is_usable({"raw_data": bad}) is False


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("RENT", "RENT"), ("rent", "RENT"), ("Rent", "RENT"), ("r", "RENT"),
        ("OWN", "OWN"), ("own", "OWN"), ("Own", "OWN"), ("o", "OWN"),
        ("1", None), ("", None), (None, None), ("OWEN", None),
    ],
)
def test_own_rent_normalization(value, expected):
    assert quality.normalize_own_rent(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [("20240115", "20240115"), ("202401", "202401"), ("NOTADATE", None), ("", None)],
)
def test_record_date_is_coerced(value, expected):
    assert quality.coerce_trace_field("Record_Date", value) == expected


def test_numeric_trace_fields_reject_garbage():
    assert quality.coerce_trace_field("Number_of_Bedrooms", "GARBAGE") is None
    assert quality.coerce_trace_field("Number_of_Bedrooms", "3") == "3"


def test_unknown_trace_fields_pass_through_untouched():
    assert quality.coerce_trace_field("Income_Description", "anything") == "anything"


def test_sparse_row_with_no_owner_name_is_accepted():
    # Nothing to mislabel when there is no ownerName at all -- pinned so the
    # narrowness of the owner-corroboration rule stays deliberate.
    assert quality.tax_row_is_usable({"raw_data": {}}) is True


def test_owner_name_without_residential_corroboration_is_rejected():
    assert quality.tax_row_is_usable(
        {"raw_data": {"ownerName": "DOE, JANE"}}
    ) is False


def test_owner_name_with_residential_corroboration_is_accepted():
    assert quality.tax_row_is_usable(
        {"raw_data": {"ownerName": "DOE, JANE", "residential": "True"}}
    ) is True


@pytest.mark.parametrize("bad_row", [None, "nope", []])
def test_non_mapping_row_is_rejected_not_raised(bad_row):
    assert quality.tax_row_is_usable(bad_row) is False
