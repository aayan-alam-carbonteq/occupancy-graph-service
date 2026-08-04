"""The graph is built by REPRODUCING production's mechanism, not its statistics."""
from clone.loader.entity import build_entities, stamped_confidence, stamped_suspicious


def test_only_ssn_bearing_rows_are_linked():
    rows = [
        {"record_id": 1, "source_table": "records_new", "ssn": "900010000", "shape": "loan"},
        {"record_id": 2, "source_table": "records_new", "ssn": None, "shape": "tax"},
        {"record_id": 3, "source_table": "records_legacy", "ssn": None, "shape": "utility"},
    ]
    masters, links = build_entities(rows)
    assert [l["record_id"] for l in links] == [1]
    assert len(masters) == 1


def test_tax_rows_are_never_linked():
    """property_owner has ssn, dob and house_number all 0% -- no blocking key --
    which is exactly why production's entity_links contains none of it."""
    rows = [{"record_id": 9, "source_table": "records_new", "ssn": None, "shape": "tax"}]
    _, links = build_entities(rows)
    assert links == []


def test_shared_ssn_collapses_to_one_entity():
    rows = [
        {"record_id": 1, "source_table": "records_new", "ssn": "900010000", "shape": "loan"},
        {"record_id": 2, "source_table": "records_new", "ssn": "900010000", "shape": "loan"},
    ]
    masters, links = build_entities(rows)
    assert len(masters) == 1
    assert masters[0]["record_count"] == 2
    assert len(links) == 2


def test_record_count_is_emergent_never_stamped():
    """Stamping record_count to production's 2.65 mean would make entity_master
    contradict entity_links, and search_people ORDERS BY record_count."""
    rows = [{"record_id": i, "source_table": "records_new", "ssn": "900010000",
             "shape": "loan"} for i in range(7)]
    masters, links = build_entities(rows)
    assert masters[0]["record_count"] == len(links) == 7


def test_links_carry_productions_match_type_and_confidence():
    rows = [{"record_id": 1, "source_table": "records_new", "ssn": "900010000", "shape": "loan"}]
    _, links = build_entities(rows)
    assert links[0]["match_type"] == "ssn"
    assert 0.95 <= float(links[0]["confidence"]) <= 1.0


def test_hal_ids_are_unique_and_stable():
    rows = [{"record_id": i, "source_table": "records_new", "ssn": f"9000{i:05d}",
             "shape": "loan"} for i in range(50)]
    masters_a, _ = build_entities(rows)
    masters_b, _ = build_entities(rows)
    ids = [m["hal_id"] for m in masters_a]
    assert len(set(ids)) == 50
    assert ids == [m["hal_id"] for m in masters_b]
    assert all(len(i) <= 15 for i in ids)      # entity_master.hal_id is char(15)


def test_stamped_metadata_matches_the_measured_production_distribution():
    confidences = [stamped_confidence(i) for i in range(20_000)]
    assert 39.0 <= sum(confidences) / len(confidences) <= 44.0        # production mean 41.52
    assert sum(1 for c in confidences if abs(c - 40.50) < 0.01) > 2_000  # modal 40.50
    suspicious = sum(stamped_suspicious(i) for i in range(20_000))
    assert 0.28 <= suspicious / 20_000 <= 0.35                        # production 31.4%
