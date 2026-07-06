from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any


def quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


class GraphDatabase:
    def __init__(self, db_path: Path) -> None:
        if not db_path.exists():
            raise FileNotFoundError(f"Graph SQLite DB does not exist: {db_path}")
        self.db_path = db_path
        uri = f"file:{db_path.resolve()}?mode=ro"
        self.connection = sqlite3.connect(uri, uri=True, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA query_only = ON")

    def close(self) -> None:
        self.connection.close()

    def fetch_all(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        return list(self.connection.execute(sql, params))

    def fetch_one(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        return self.connection.execute(sql, params).fetchone()

    def fetch_value(self, sql: str, params: tuple[Any, ...] = ()) -> Any:
        row = self.fetch_one(sql, params)
        return None if row is None else row[0]

    def table_columns(self, table: str) -> set[str]:
        rows = self.connection.execute(f"PRAGMA table_info({quote(table)})").fetchall()
        return {row[1] for row in rows}
