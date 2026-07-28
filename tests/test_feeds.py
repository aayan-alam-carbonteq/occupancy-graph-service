from __future__ import annotations

from occupancy_graph.source.feeds import FEEDS, feed_clause


def test_every_shape_has_a_feed_definition():
    assert set(FEEDS) == {"base", "tax", "utility", "trace", "auto", "loan", "drive"}


def test_utility_and_trace_live_in_the_legacy_table():
    assert FEEDS["utility"].tables == ("records_legacy",)
    assert FEEDS["trace"].tables == ("records_legacy",)


def test_tax_lives_only_in_the_partitioned_table():
    assert FEEDS["tax"].tables == ("records_partitioned",)


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
