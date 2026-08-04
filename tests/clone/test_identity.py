from clone.loader.identity import DRIVE_JOIN_KEY, PersonIndex, synthetic_ssn


def test_union_find_joins_two_addresses_through_a_shared_phone():
    """THE property owner-elsewhere traversal depends on. Grouping by
    (name, address) would split every mover into separate entities and the
    traversal would return nothing. Trace has ~1.84 addresses per person, max 17."""
    idx = PersonIndex()
    a = idx.add(first="JANE", last="DOE", address="1 A ST", zip="40505", dob="", phone="5551112222")
    b = idx.add(first="JANE", last="DOE", address="9 B RD", zip="40505", dob="", phone="5551112222")
    assert idx.person_of(a) == idx.person_of(b)


def test_different_people_at_one_address_stay_separate():
    idx = PersonIndex()
    a = idx.add(first="JANE", last="DOE", address="1 A ST", zip="40505", dob="", phone="")
    b = idx.add(first="JOHN", last="SMITH", address="1 A ST", zip="40505", dob="", phone="")
    assert idx.person_of(a) != idx.person_of(b)


def test_dob_links_across_addresses_when_phone_is_absent():
    idx = PersonIndex()
    a = idx.add(first="JANE", last="DOE", address="1 A ST", zip="40505", dob="1980-01-01", phone="")
    b = idx.add(first="JANE", last="DOE", address="9 B RD", zip="40515", dob="1980-01-01", phone="")
    assert idx.person_of(a) == idx.person_of(b)


def test_transitive_closure_links_a_chain():
    """A--B share a phone, B--C share a dob: A and C must land together even
    though they share nothing directly. That is what union-find buys over
    pairwise matching."""
    idx = PersonIndex()
    a = idx.add(first="JANE", last="DOE", address="1 A ST", zip="40505", dob="", phone="5551112222")
    b = idx.add(first="JANE", last="DOE", address="9 B RD", zip="40505", dob="1980-01-01", phone="5551112222")
    c = idx.add(first="JANE", last="DOE", address="7 C AVE", zip="40517", dob="1980-01-01", phone="")
    assert idx.person_of(a) == idx.person_of(c)


def test_a_row_with_no_surname_never_joins_a_cluster():
    idx = PersonIndex()
    a = idx.add(first="", last="", address="1 A ST", zip="40505", dob="", phone="5551112222")
    b = idx.add(first="", last="", address="1 A ST", zip="40505", dob="", phone="5551112222")
    assert idx.person_of(a) != idx.person_of(b)


def test_synthetic_ssn_uses_the_never_issued_900_range():
    """900-999 area numbers are never issued by the SSA, so a synthetic SSN can
    never collide with a real person's."""
    for person_id in (0, 1, 12345, 999_999):
        ssn = synthetic_ssn(person_id)
        assert len(ssn) == 9 and ssn.isdigit()
        assert 900 <= int(ssn[:3]) <= 999


def test_synthetic_ssn_is_stable_and_distinct():
    assert synthetic_ssn(42) == synthetic_ssn(42)
    assert len({synthetic_ssn(i) for i in range(5_000)}) == 5_000


def test_person_ids_are_dense_and_stable():
    idx = PersonIndex()
    a = idx.add(first="A", last="ONE", address="1 X", zip="1", dob="", phone="")
    b = idx.add(first="B", last="TWO", address="2 Y", zip="2", dob="", phone="")
    ids = idx.person_ids()
    assert sorted(set(ids.values())) == [0, 1]
    assert ids == idx.person_ids()          # stable across calls


def test_drive_join_key_is_person_and_address_not_id():
    """`id` is NOT a cross-shape key: cd076219 is GARY HILES in loan and
    BRIANNA HILES in drive. Measured match rates: (id,first,last) 29.8%,
    (addr,zip,first,last) 55.4%."""
    row = {"id": "cd076219", "address": "934 Dayton Ave", "zip": "40505",
           "firstname": "gary", "lastname": "hiles"}
    assert DRIVE_JOIN_KEY(row) == ("934 DAYTON AVE", "40505", "GARY", "HILES")
