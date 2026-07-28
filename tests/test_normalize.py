from __future__ import annotations

from occupancy_graph.normalize import (
    name_key,
    normalize_address,
    normalize_phone,
    normalize_text,
)


def test_normalize_address_expands_and_uppercases():
    assert normalize_address("123 Main Street") == "123 MAIN ST"
    assert normalize_address("  456  Pine   Avenue ") == "456 PINE AVE"


def test_normalize_address_is_stable_on_already_normal_input():
    assert normalize_address("123 MAIN ST") == "123 MAIN ST"


def test_normalize_phone_keeps_digits_only():
    assert normalize_phone("(555) 111-2222") == "5551112222"


def test_name_key_is_first_pipe_last_casefolded():
    assert name_key("Jane", "Doe") == "jane|doe"
    assert name_key(None, "Doe") == "|doe"
