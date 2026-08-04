"""Buffer rows per table, then COPY them in, in the RECORDED column order.

`COPY` (and asyncpg's `copy_records_to_table`) is POSITIONAL: it has no column
names on the wire, only a column list handed alongside the records. If that
list drifts from the real catalog -- someone renames a column here, the
partner adds one there -- values land in the WRONG COLUMNS silently. No
error, no exception, just a `first_name` value sitting in `middle_name`.

So the column order is READ from clone/profiles/records_catalog.json (144
entries, captured from the live corpus by capture_catalog.py) rather than
hand-kept in this file, for the same reason mapping.py reads manifest.py
instead of restating it: a copy here could disagree with the copy the DDL and
the capture script both derive from, and nothing would catch it.
"""
from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Mapping
from pathlib import Path
from typing import Any

CATALOG = Path(__file__).parents[1] / "profiles" / "records_catalog.json"


def _load_columns() -> list[str]:
    return [name for name, _typ in json.loads(CATALOG.read_text())]


class RecordBatch:
    """Rows buffered per table, positioned against the catalog column order."""

    def __init__(self) -> None:
        # A fresh list per instance: self.columns is handed straight to
        # conn.copy_records_to_table as the positional column list, and it
        # must not be a shared mutable object another RecordBatch could
        # reorder out from under this one.
        self.columns: list[str] = _load_columns()
        self._index: dict[str, int] = {name: i for i, name in enumerate(self.columns)}
        self._rows: dict[str, list[list[Any]]] = defaultdict(list)

    def add(self, *, record_id: int, table: str, source_file: str,
            imported_at: str | None, columns: Mapping[str, Any],
            raw_data: Mapping[str, Any]) -> None:
        row: list[Any] = [None] * len(self.columns)
        row[self._index["record_id"]] = record_id
        row[self._index["source_file"]] = source_file
        row[self._index["imported_at"]] = imported_at

        for name, value in columns.items():
            if name not in self._index:
                # A typo'd column name that silently vanished would be
                # indistinguishable from a genuinely absent value -- exactly
                # the failure COPY's positionality makes invisible. Raise
                # loudly instead of dropping it.
                raise KeyError(f"unknown records column {name!r} -- not in {CATALOG}")
            row[self._index[name]] = value

        # utility carries NO raw_data at all in production (0%). Writing {}
        # for that case would misrepresent "absent" as "present but empty",
        # so an empty mapping stays NULL rather than becoming "{}".
        if raw_data:
            row[self._index["raw_data"]] = json.dumps(dict(raw_data))

        self._rows[table].append(row)

    def rows(self, table: str) -> list[list[Any]]:
        return self._rows.get(table, [])

    async def flush(self, conn) -> int:
        """COPY every buffered table into `conn`, then clear the buffer.

        One copy_records_to_table call per table -- COPY targets exactly one
        relation, so records_legacy and records_new rows can never share a
        call even though they share this batch and this column list.
        """
        total = 0
        for table, rows in self._rows.items():
            if not rows:
                continue
            await conn.copy_records_to_table(
                table, schema_name="public", columns=self.columns, records=rows,
            )
            total += len(rows)
        self._rows.clear()
        return total
