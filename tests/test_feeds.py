from __future__ import annotations

from occupancy_graph.source.feeds import FEEDS, feed_clause, shapes_for_row
from occupancy_graph.source.manifest import SHAPES


def test_every_shape_has_a_feed_definition():
    assert set(FEEDS) == {"base", "tax", "utility", "trace", "auto", "loan", "drive"}


def test_utility_and_trace_live_in_the_legacy_table():
    assert FEEDS["utility"].tables == ("records_legacy",)
    assert FEEDS["trace"].tables == ("records_legacy",)


def test_tax_lives_only_in_the_partitioned_parent():
    """`records_new` IS the partitioned parent on the live corpus. The name
    `records_partitioned` belongs to its PARTITIONS and is not a relation, so
    naming it here would raise `relation does not exist` in production."""
    assert FEEDS["tax"].tables == ("records_new",)


def test_feed_clause_is_a_heap_filter_never_a_driving_predicate():
    clause, params = feed_clause("utility", start_index=1)
    assert clause == "(source_file LIKE $1)"
    assert params == ["Export Utility Stripped Down/%"]


def test_multi_pattern_feeds_are_ored():
    clause, params = feed_clause("auto", start_index=3)
    assert clause == "(source_file LIKE $3 OR source_file LIKE $4 OR source_file LIKE $5)"
    assert len(params) == 3


def test_drive_adds_a_non_null_licence_requirement():
    clause, _ = feed_clause("drive", start_index=1)
    assert "dl_number IS NOT NULL" in clause


# --- Reverse mapping: a fetched row carries source_file, not a shape. The
# --- hal: traversal fetches by record_id, so the predicates must run backwards.


def test_a_utility_row_maps_back_to_the_utility_shape():
    row = {"source_file": "Export Utility Stripped Down/Utility_ky/Utility_ky.csv"}
    assert shapes_for_row(row) == ("utility",)


def test_a_property_owner_row_maps_back_to_tax_only():
    assert shapes_for_row({"source_file": "property_owner_49/property_owner_49.csv"}) == ("tax",)


def test_a_payday_row_with_a_licence_is_both_loan_and_drive():
    row = {"source_file": "Payday_Big_1/Payday_Big_1.csv", "dl_number": "A12345678"}
    assert shapes_for_row(row) == ("drive", "loan")


def test_a_payday_row_without_a_licence_is_loan_only():
    row = {"source_file": "Payday_Big_1/Payday_Big_1.csv", "dl_number": None}
    assert shapes_for_row(row) == ("loan",)


def test_like_underscore_is_a_single_char_wildcard_not_a_literal():
    # "Payday_Big_%" must match "PaydayXBigY..." as SQL LIKE does.
    assert shapes_for_row({"source_file": "PaydayXBigY/x.csv", "dl_number": None}) == ("loan",)


def test_an_unknown_source_file_maps_to_no_shape():
    assert shapes_for_row({"source_file": "Some Other Feed/x.csv"}) == ()
    assert shapes_for_row({"source_file": None}) == ()


def test_manifest_and_feeds_cover_exactly_the_same_shapes():
    """shapes_for_row walks SHAPES and indexes FEEDS, so a shape added to one
    table and not the other fails asymmetrically and invisibly: missing from
    FEEDS it raises KeyError at import time, missing from SHAPES it is silently
    dropped from every reverse lookup. Neither is discoverable without this."""
    assert set(SHAPES) == set(FEEDS)
