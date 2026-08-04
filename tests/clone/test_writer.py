import json

from clone.loader.writer import RecordBatch


def test_batch_emits_every_catalog_column_in_order():
    """COPY is positional: a column list that drifts from the catalog writes
    values into the wrong columns silently."""
    batch = RecordBatch()
    assert len(batch.columns) == 144
    batch.add(record_id=1, table="records_legacy",
              source_file="Export Utility Stripped Down/x.csv", imported_at=None,
              columns={"first_name": "PAT", "zip": "40505"}, raw_data={})
    row = batch.rows("records_legacy")[0]
    assert len(row) == len(batch.columns)
    assert row[batch.columns.index("first_name")] == "PAT"
    assert row[batch.columns.index("zip")] == "40505"
    assert row[batch.columns.index("last_name")] is None
    assert row[batch.columns.index("record_id")] == 1


def test_raw_data_is_serialised_as_json():
    batch = RecordBatch()
    batch.add(record_id=2, table="records_new", source_file="Payday_Big_2026/x.csv",
              imported_at="2026-02-15", columns={}, raw_data={"Email_02": "a@b.c"})
    row = batch.rows("records_new")[0]
    assert json.loads(row[batch.columns.index("raw_data")]) == {"Email_02": "a@b.c"}


def test_empty_raw_data_is_null_not_an_empty_object():
    """utility carries NO raw_data at all in production (0%). An empty {} would
    misrepresent that as 'present but empty'."""
    batch = RecordBatch()
    batch.add(record_id=3, table="records_legacy", source_file="Export Utility Stripped Down/x.csv",
              imported_at=None, columns={}, raw_data={})
    assert batch.rows("records_legacy")[0][batch.columns.index("raw_data")] is None


def test_rows_are_kept_per_table():
    batch = RecordBatch()
    batch.add(record_id=1, table="records_legacy", source_file="a", imported_at=None,
              columns={}, raw_data={})
    batch.add(record_id=2, table="records_new", source_file="b", imported_at="2026-02-15",
              columns={}, raw_data={})
    assert len(batch.rows("records_legacy")) == 1
    assert len(batch.rows("records_new")) == 1


def test_an_unknown_column_is_rejected_not_silently_dropped():
    """A typo'd column name that silently vanished would be indistinguishable
    from a genuinely absent value -- exactly the failure COPY's positionality
    makes invisible."""
    import pytest
    batch = RecordBatch()
    with pytest.raises(KeyError, match="not_a_real_column"):
        batch.add(record_id=1, table="records_legacy", source_file="a", imported_at=None,
                  columns={"not_a_real_column": "x"}, raw_data={})
