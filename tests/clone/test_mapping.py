"""The ETL must READ manifest.py, never restate it.

manifest.py is the single source of truth for shape<->column mapping. A loader
carrying its own copy would let the clone silently desync from the contract the
service reads -- the same class of drift that let a backwards fixture topology
survive 548 passing tests.
"""
from clone.loader.mapping import partner_row_for


def test_col_origins_become_partner_columns():
    cols, raw = partner_row_for("utility", {
        "first_name": "PAT", "last_name": "TENANT", "zip": "40505"})
    assert cols["first_name"] == "PAT"
    assert cols["last_name"] == "TENANT"
    assert cols["zip"] == "40505"
    assert raw == {}


def test_raw_origins_go_into_raw_data_not_a_column():
    """trace.email_02 is raw("Email_02") -- it lives in the jsonb, and there is
    no email_02 column on the partner table to receive it."""
    cols, raw = partner_row_for("trace", {"email_02": "x@y.com"})
    assert raw == {"Email_02": "x@y.com"}
    assert "email_02" not in cols


def test_aliased_columns_use_the_partner_name():
    """trace.cellphone is col("mobile")."""
    cols, _ = partner_row_for("trace", {"cellphone": "5551112222"})
    assert cols == {"mobile": "5551112222"}


def test_derived_and_absent_origins_write_nothing():
    """trace.housenumber is derived() -- computed at read time from address text.
    Writing it would populate house_number on a feed where production has it
    NULL, handing the resident-hop anchor rows production does not have and
    inflating local hop coverage."""
    cols, raw = partner_row_for("trace", {"housenumber": "1104",
                                          "address": "1104 SPRING RUN RD"})
    assert "house_number" not in cols
    assert cols == {"address": "1104 SPRING RUN RD"}
    assert raw == {}


def test_empty_values_are_omitted_entirely():
    cols, raw = partner_row_for("utility", {"first_name": "", "last_name": "DOE"})
    assert cols == {"last_name": "DOE"}


def test_every_shape_in_the_manifest_can_be_mapped():
    """A shape added to manifest.py but unmappable here would silently load as
    empty rows."""
    from occupancy_graph.source.manifest import SHAPES
    for shape in SHAPES:
        cols, raw = partner_row_for(shape, {})
        assert cols == {} and raw == {}
