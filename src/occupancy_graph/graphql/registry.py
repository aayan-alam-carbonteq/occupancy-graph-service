from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from occupancy_graph.graphdb.core import ID_LINKED_SOURCES, SOURCE_FILES


GRAPHQL_TYPE_NAMES = {
    "base": "BaseRecord",
    "auto": "AutoRecord",
    "criminal": "CriminalRecord",
    "drive": "DriveRecord",
    "linkedin": "LinkedInRecord",
    "loan": "LoanRecord",
    "tax": "TaxRecord",
    "trace": "TraceRecord",
    "utility": "UtilityRecord",
    "voter": "VoterRecord",
}

COLLECTION_FIELD_NAMES = {
    "base": "baseRecords",
    "auto": "autos",
    "criminal": "criminals",
    "drive": "drives",
    "linkedin": "linkedins",
    "loan": "loans",
    "tax": "taxes",
    "trace": "traces",
    "utility": "utilities",
    "voter": "voters",
}

SINGULAR_FIELD_NAMES = {
    "base": "baseRecord",
    "auto": "auto",
    "criminal": "criminal",
    "drive": "drive",
    "linkedin": "linkedin",
    "loan": "loan",
    "tax": "tax",
    "trace": "trace",
    "utility": "utility",
    "voter": "voter",
}

ADDRESS_ROLES_BY_SOURCE: dict[str, dict[str, str]] = {
    "base": {"residence": "addresses"},
    "tax": {"property": "propertyAddresses", "owner_mailing": "ownerMailingAddresses"},
    "utility": {"service": "addresses"},
    "trace": {"residence": "addresses"},
    "auto": {"registration": "addresses"},
    "loan": {"residence": "addresses"},
    "drive": {"license": "addresses"},
    "voter": {"registration": "addresses"},
    "criminal": {"record": "addresses"},
}

ID_LINKED_RELATIONS: dict[str, list[tuple[str, str]]] = {
    "base": [
        ("tax", "taxRecords"),
        ("trace", "traceRecords"),
        ("auto", "autoRecords"),
        ("loan", "loanRecords"),
        ("drive", "driveRecords"),
        ("voter", "voterRecords"),
        ("criminal", "criminalRecords"),
        ("linkedin", "linkedinRecords"),
    ],
}

ADDRESS_COLLECTION_RELATIONS: dict[str, list[tuple[str, str, str]]] = {
    "addresses": [
        ("base", "residence", "residents"),
        ("utility", "service", "utilityRecords"),
        ("tax", "property", "taxProperties"),
        ("tax", "owner_mailing", "ownerMailingOf"),
        ("trace", "residence", "traceRecords"),
        ("auto", "registration", "autoRecords"),
        ("loan", "residence", "loanRecords"),
        ("drive", "license", "driveRecords"),
        ("voter", "registration", "voterRecords"),
        ("criminal", "record", "criminalRecords"),
    ],
}


@dataclass(frozen=True)
class TableSchema:
    name: str
    graphql_type: str
    collection_field: str
    singular_field: str
    columns: tuple[str, ...]
    norm_columns: tuple[str, ...]
    filter_columns: tuple[str, ...]
    id_linked: bool


class SchemaRegistry:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.tables: dict[str, TableSchema] = {}
        self._load()

    def _load(self) -> None:
        uri = f"file:{self.db_path.resolve()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
        try:
            for source in SOURCE_FILES:
                if source not in GRAPHQL_TYPE_NAMES:
                    continue
                columns = self._table_columns(connection, source)
                if not columns:
                    continue
                raw_columns = tuple(column for column in columns if not column.startswith("__"))
                norm_columns = tuple(column for column in columns if column.startswith("__"))
                public_norm_columns = tuple(
                    column.removeprefix("__norm_") for column in norm_columns if column.startswith("__norm_")
                )
                filter_columns = tuple(dict.fromkeys([*raw_columns, *public_norm_columns]))
                self.tables[source] = TableSchema(
                    name=source,
                    graphql_type=GRAPHQL_TYPE_NAMES[source],
                    collection_field=COLLECTION_FIELD_NAMES[source],
                    singular_field=SINGULAR_FIELD_NAMES[source],
                    columns=raw_columns,
                    norm_columns=norm_columns,
                    filter_columns=filter_columns,
                    id_linked=source == "base" or source in ID_LINKED_SOURCES,
                )
        finally:
            connection.close()

    def source_tables(self) -> list[TableSchema]:
        return [self.tables[name] for name in SOURCE_FILES if name in self.tables]

    def get(self, source: str) -> TableSchema:
        if source not in self.tables:
            raise KeyError(f"Unknown source table: {source}")
        return self.tables[source]

    @staticmethod
    def _table_columns(connection: sqlite3.Connection, table: str) -> list[str]:
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        if rows is None:
            return []
        info = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
        return [row[1] for row in info]
