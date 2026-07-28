from __future__ import annotations

import pytest

from occupancy_graph.normalize import (
    name_key,
    normalize_address,
    normalize_address_value,
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


@pytest.mark.parametrize(
    ("word", "abbrev"),
    [
        ("AVENUE", "AVE"), ("STREET", "ST"), ("DRIVE", "DR"), ("ROAD", "RD"),
        ("COURT", "CT"), ("LANE", "LN"), ("BOULEVARD", "BLVD"), ("PARKWAY", "PKWY"),
        ("PLACE", "PL"), ("CIRCLE", "CIR"), ("TERRACE", "TER"), ("HIGHWAY", "HWY"),
        ("APARTMENT", "APT"), ("SUITE", "STE"),
    ],
)
def test_every_suffix_replacement_is_applied(word, abbrev):
    """All 14 rules are pinned individually. The consuming engine's address
    matching is calibrated against these; graphdb/_normalize.py (the original)
    is deleted in a later task, so this test becomes the only guard."""
    assert normalize_address("123 MAIN " + word) == f"123 MAIN {abbrev}"


@pytest.mark.parametrize("tail", ["", " #4", " APT 4", " STE 4"])
def test_trace_becomes_trce_only_before_a_unit_or_end(tail):
    assert normalize_address("123 OAK TRACE" + tail).startswith("123 OAK TRCE")


def test_trace_is_left_alone_mid_address():
    assert normalize_address("123 TRACE RIDGE RD") == "123 TRACE RIDGE RD"


@pytest.mark.parametrize("abbrev", ["PK", "PRK"])
@pytest.mark.parametrize("tail", ["", " #4", " APT 4", " STE 4"])
def test_pk_and_prk_become_park_only_before_a_unit_or_end(abbrev, tail):
    assert normalize_address(f"123 OAK {abbrev}{tail}").startswith("123 OAK PARK")


def test_pk_is_left_alone_mid_address():
    assert normalize_address("123 PK RIDGE RD") == "123 PK RIDGE RD"
