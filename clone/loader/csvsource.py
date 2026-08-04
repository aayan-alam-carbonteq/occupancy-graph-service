"""Read a shape CSV into stripped dicts.

base.csv is space-padded in both headers and values; the others are not.
Stripping unconditionally costs nothing and removes a whole class of silent
key-miss bug -- a padded header means row["id"] returns None and the shape looks
like it simply has no id column.

Yields lazily: utility.csv alone is 1.5M rows / 113 MB.
"""
from __future__ import annotations

import csv
from collections.abc import Iterator
from pathlib import Path


def read_shape_csv(path: Path) -> Iterator[dict[str, str]]:
    with path.open(newline="", encoding="utf-8", errors="replace") as handle:
        for row in csv.DictReader(handle):
            yield {
                (key or "").strip(): (value or "").strip()
                for key, value in row.items()
                if key is not None
            }
