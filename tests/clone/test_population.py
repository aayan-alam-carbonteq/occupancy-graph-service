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
    """ssn on the payday feed is the ONLY partial rate the loader still applies.

    dob/phone/email deliberately have no target any more: the Lexington CSVs are
    a SUBSET of the partner corpus (verified 2026-08-05 -- the same people, at
    the same address, in the same feeds, matching field for field), so a CSV
    value IS production's value and sampling it can only move the clone away
    from production. See feed_population.json's `_comment`."""
    targets = load_targets()
    kept = sum(keep_value("loan", "ssn", f"row{i}", targets) for i in range(10_000))
    assert 93.0 <= kept / 100 <= 98.0     # target 95.8%


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
    """property_owner has ssn and house_number at 0% -- no blocking key at all,
    which is exactly why production's entity_links contains no tax rows.

    Only the loader-controlled columns are asserted: dob/phone are carried
    verbatim from the CSV now, so the loader has no say in them."""
    targets = load_targets()
    for column in ("ssn", "house_number"):
        assert not keep_value("tax", column, "row1", targets)
