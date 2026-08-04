"""Column population is behaviour, not decoration.

packet_gates.ts gates SIX OF SEVEN packets on field presence, so a clone with
phone at 90% where production has 41.9% runs packets production would skip, and
every coverage number drifts.
"""
from clone.loader.population import keep_value, load_targets


def test_zero_percent_columns_are_always_dropped():
    """utility.ssn is 0.0% in production. This is what makes the entity graph
    link only payday rows: no ssn, no blocking key, no link."""
    targets = load_targets()
    assert not any(keep_value("utility", "ssn", f"row{i}", targets) for i in range(200))


def test_hundred_percent_columns_are_always_kept():
    targets = load_targets()
    assert all(keep_value("base", "house_number", f"row{i}", targets) for i in range(200))


def test_partial_columns_land_near_the_target_rate():
    targets = load_targets()
    kept = sum(keep_value("trace", "phone", f"row{i}", targets) for i in range(10_000))
    assert 74.0 <= kept / 100 <= 78.0     # target 76.0%


def test_sampling_is_deterministic_not_random():
    """Reruns must be byte-identical or nothing measured against the clone is
    comparable to the previous run."""
    targets = load_targets()
    first = [keep_value("trace", "phone", f"row{i}", targets) for i in range(500)]
    second = [keep_value("trace", "phone", f"row{i}", targets) for i in range(500)]
    assert first == second


def test_unlisted_columns_are_kept_untouched():
    """Absence of a target means 'never measured', not 'production has none'."""
    targets = load_targets()
    assert keep_value("utility", "address", "row1", targets) is True


def test_tax_carries_none_of_the_blocking_keys():
    """property_owner has ssn, dob and house_number all 0% -- no blocking key at
    all, which is exactly why production's entity_links contains no tax rows."""
    targets = load_targets()
    for column in ("ssn", "dob", "phone", "house_number"):
        assert not keep_value("tax", column, "row1", targets)
