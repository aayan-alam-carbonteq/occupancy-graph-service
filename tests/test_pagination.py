"""Limit/offset parsing and the {total_count, has_more, <key>} block shape.

A malformed limit is a 400, never a silent default: the engine would otherwise
silently receive a different page size than it asked for.
"""
from __future__ import annotations

import pytest

from occupancy_graph.service.pagination import Page, page_params, paginate

ITEMS = [f"row{i}" for i in range(10)]


def test_default_limit_and_offset_when_absent():
    page = page_params({})
    assert page == Page(limit=25, offset=0)


def test_limit_is_capped_at_the_maximum():
    assert page_params({"limit": "9999"}).limit == 200


def test_limit_below_one_is_raised_to_one():
    assert page_params({"limit": "0"}).limit == 1
    assert page_params({"limit": "-5"}).limit == 1


def test_negative_offset_is_clamped_to_zero():
    assert page_params({"offset": "-3"}).offset == 0


def test_a_non_integer_limit_is_rejected_not_silently_defaulted():
    with pytest.raises(ValueError, match="limit"):
        page_params({"limit": "ten"})


def test_a_caller_supplied_default_limit_is_honoured():
    assert page_params({}, default_limit=10).limit == 10


def test_paginate_reports_total_and_has_more():
    block = paginate(ITEMS, Page(limit=3, offset=0))
    assert block == {"total_count": 10, "has_more": True, "records": ["row0", "row1", "row2"]}


def test_paginate_last_page_has_no_more():
    block = paginate(ITEMS, Page(limit=3, offset=9))
    assert block == {"total_count": 10, "has_more": False, "records": ["row9"]}


def test_paginate_uses_the_requested_collection_key_and_survives_a_past_end_offset():
    block = paginate(ITEMS, Page(limit=5, offset=50), key="people")
    assert block == {"total_count": 10, "has_more": False, "people": []}
