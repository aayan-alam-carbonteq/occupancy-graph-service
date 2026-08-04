"""source_file and imported_at are not cosmetic: feeds.py selects rows by
source_file LIKE, and bounds the tax scan to one partition by imported_at. A row
whose feed identity is wrong is invisible to the service that loaded it."""
from occupancy_graph.source.feeds import FEEDS, _like_to_regex

from clone.loader.feedplan import FEED_PLANS, plan_for


def test_every_zip_shape_has_a_plan():
    for shape in ("utility", "trace", "base", "loan", "auto", "tax"):
        assert plan_for(shape), f"{shape} has no feed plan"


def test_every_plans_source_file_matches_its_own_feeds_pattern():
    """The round trip that would have caught the records_partitioned bug."""
    for plan in FEED_PLANS:
        patterns = [_like_to_regex(p) for p in FEEDS[plan.shape].patterns]
        assert any(rx.match(plan.source_file) for rx in patterns), (
            f"{plan.shape}: {plan.source_file!r} matches none of "
            f"{FEEDS[plan.shape].patterns}")


def test_plans_target_the_table_feeds_py_declares():
    for plan in FEED_PLANS:
        assert plan.table in FEEDS[plan.shape].tables


def test_tax_lands_inside_the_partition_feeds_py_bounds_it_to():
    """feeds.py bounds tax to [2026-03-01, 2026-04-01). Outside it the assessor
    rows exist but no query can see them."""
    tax = [p for p in FEED_PLANS if p.shape == "tax"]
    assert tax
    for plan in tax:
        assert plan.imported_at.startswith("2026-03")


def test_loan_and_drive_share_one_payday_feed():
    """Production has no drive feed: drive IS loan-with-a-licence, the same
    physical rows distinguished by dl_number IS NOT NULL."""
    assert {p.source_file for p in FEED_PLANS if p.shape == "loan"}
    assert not [p for p in FEED_PLANS if p.shape == "drive"]


def test_base_spans_both_roots_like_production():
    tables = {p.table for p in FEED_PLANS if p.shape == "base"}
    assert tables == {"records_legacy", "records_new"}


def test_records_legacy_plans_have_no_imported_at():
    """records_legacy is not partitioned; only records_new routes by date."""
    for plan in FEED_PLANS:
        if plan.table == "records_legacy":
            assert plan.imported_at is None
