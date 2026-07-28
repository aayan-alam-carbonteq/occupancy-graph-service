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


def test_every_surviving_shape_is_present_and_the_dead_ones_are_not():
    assert set(SHAPES) == {"base", "tax", "utility", "trace", "auto", "loan", "drive"}


def test_shape_column_lists_match_the_committed_contract_exactly():
    """These are the upstream vendor's CSV headers. They are inconsistent between
    shapes on purpose (utility uses first_name, trace uses firstname). The engine
    reads SourceRecord.data by these exact keys."""
    assert SHAPES["utility"].columns == (
        "first_name", "last_name", "middle_name", "dob", "dod",
        "address", "city", "county", "state", "zip", "phone",
    )
    assert SHAPES["loan"].columns == (
        "id", "loan_id", "loan_amount", "monthly_income", "month_pay", "own_rent",
        "employer", "occupation", "address", "zip", "firstname", "lastname",
    )
    assert SHAPES["drive"].columns == (
        "id", "drive_id", "dl_num", "dl_state", "zip", "address", "firstname", "lastname",
    )
    assert SHAPES["auto"].columns == (
        "id", "auto_id", "vin", "zip", "city", "make", "model", "year",
        "phone", "address", "housenumber", "firstname", "lastname",
    )
    assert len(SHAPES["trace"].columns) == 23
    assert len(SHAPES["base"].columns) == 33


def test_norm_helpers_match_the_committed_contract_for_every_shape():
    assert SHAPES["utility"].norm_fields == (
        "firstname", "lastname", "name_key", "address", "address_zip_key", "phone",
    )
    assert SHAPES["drive"].norm_fields == (
        "firstname", "lastname", "name_key", "address", "address_zip_key",
    )
    assert SHAPES["trace"].norm_fields == (
        "firstname", "lastname", "name_key", "address", "address_zip_key",
        "phone", "cellphone", "email", "email_02", "email_03",
    )
    assert SHAPES["base"].norm_fields == (
        "firstname", "lastname", "name_key", "address", "address_zip_key",
        "primaryaddress", "phone",
    )


def test_mortgage_amount_is_a_direct_mapping_because_it_is_already_in_thousands():
    """Observed range on real data is 3-598 (avg 171). A /1000 would render a
    $171k mortgage as 0.17."""
    assert SHAPES["base"].fields["mortgageamountinthousands"].kind == "col"
    assert SHAPES["base"].fields["mortgageamountinthousands"].key == "mortgage_amount"


def test_drive_and_loan_read_the_same_physical_row():
    """There is no DMV feed. dl_number is a column on payday-loan rows."""
    assert SHAPES["drive"].fields["dl_num"].key == "dl_number"
    assert SHAPES["loan"].fields["employer"].key == "employer"


def test_no_two_distinct_derived_fields_in_a_shape_collapse_to_one_origin():
    """derived() takes its comparable `key` from fn.__name__ because FieldOrigin.fn
    is compare=False. A factory that forgets to set __name__ makes every closure
    `_fn`, so distinct fields silently compare equal and a mis-wiring becomes
    invisible. Deleting the __name__ line in derive.first_raw previously failed
    no test at all.
    """
    for shape in SHAPES.values():
        derived_fields = [(n, o) for n, o in shape.fields.items() if o.kind == "derived"]
        for i, (name_a, origin_a) in enumerate(derived_fields):
            for name_b, origin_b in derived_fields[i + 1:]:
                if origin_a.fn is origin_b.fn:
                    continue  # genuinely the same function, e.g. id and tax_id
                assert origin_a != origin_b, (
                    f"{shape.name}.{name_a} and {shape.name}.{name_b} are backed by "
                    f"different functions but compare equal — a factory in derive.py "
                    f"is not setting _fn.__name__"
                )
