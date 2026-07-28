from __future__ import annotations

from occupancy_graph.source.manifest import SHAPES, FieldOrigin


def test_tax_shape_metadata():
    tax = SHAPES["tax"]
    assert tax.graphql_type == "TaxRecord"
    assert tax.collection_field == "taxes"
    assert tax.singular_field == "tax"
    assert tax.id_linked is True


def test_tax_field_order_matches_the_committed_contract():
    """Field ORDER is part of the SDL, so the manifest must preserve it."""
    assert list(SHAPES["tax"].fields)[:6] == [
        "id", "tax_id", "address", "addressformal", "housenumber", "city",
    ]


def test_tax_origins_are_exact_lowercase_renames_of_vendor_keys():
    fields = SHAPES["tax"].fields
    assert fields["ownername"] == FieldOrigin(kind="raw", key="ownerName")
    assert fields["equitycurrentestbal"] == FieldOrigin(kind="raw", key="equityCurrentEstBal")
    assert fields["ltvcurrentestcombined"] == FieldOrigin(kind="raw", key="LTVCurrentEstCombined")
    assert fields["totalfinancinghistcount"] == FieldOrigin(kind="raw", key="totalFinancingHistCount")


def test_yearbuilt_is_declared_absent_not_silently_missing():
    assert SHAPES["tax"].fields["yearbuilt"].kind == "absent"


def test_tax_norm_helpers_match_the_committed_contract():
    assert SHAPES["tax"].norm_fields == (
        "firstname", "lastname", "name_key", "address", "address_zip_key",
        "owneraddressline1",
    )


def test_derived_origins_backed_by_different_functions_are_not_equal():
    """fn is compare=False, so without a discriminator every derived() origin
    would compare equal and a mis-wiring would be invisible."""
    fields = SHAPES["tax"].fields
    assert fields["zip"] != fields["county"]
    assert fields["zip"].key == "tax_zip5"
    assert fields["county"].key == "fips_county"


def test_fields_sharing_one_derive_function_still_compare_equal():
    """id and tax_id genuinely have the same origin."""
    fields = SHAPES["tax"].fields
    assert fields["id"] == fields["tax_id"]
