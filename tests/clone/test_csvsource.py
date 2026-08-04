from pathlib import Path

from clone.loader.csvsource import read_shape_csv


def test_padded_headers_and_values_are_stripped(tmp_path: Path):
    """base.csv ships space-padded headers ("firstname         ,"). This broke a
    real parse during design -- row["id"] returned None and the shape looked like
    it had no id column at all."""
    p = tmp_path / "base.csv"
    p.write_text("firstname         , lastname , zip  \n" " JANE , DOE , 40505 \n")
    rows = list(read_shape_csv(p))
    assert rows == [{"firstname": "JANE", "lastname": "DOE", "zip": "40505"}]


def test_blank_values_become_empty_strings_not_none(tmp_path: Path):
    p = tmp_path / "x.csv"
    p.write_text("a,b\n1,\n")
    assert list(read_shape_csv(p)) == [{"a": "1", "b": ""}]


def test_rows_are_yielded_lazily_not_materialised(tmp_path: Path):
    """utility.csv is 1.5M rows / 113MB. Materialising every shape at once would
    be gigabytes for no reason."""
    import inspect
    assert inspect.isgeneratorfunction(read_shape_csv)
