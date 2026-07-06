from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from occupancy_graph.graphdb._normalize import normalize_address_value


SOURCE_FILES = {
    "base": "base.csv",
    "auto": "auto.csv",
    "criminal": "criminal.csv",
    "drive": "drive.csv",
    "linkedin": "linkedin.csv",
    "loan": "loan.csv",
    "tax": "tax.csv",
    "trace": "trace.csv",
    "utility": "utility.csv",
    "voter": "voter.csv",
}

OPTIONAL_SOURCES: set[str] = set()

OPTIONAL_SOURCE_COLUMNS: dict[str, list[str]] = {}

ID_LINKED_SOURCES = ("auto", "criminal", "drive", "linkedin", "loan", "tax", "trace", "voter")

ADDRESS_EDGE_SPECS: tuple[tuple[str, str, str, str, str | None, str | None, str | None], ...] = (
    ("base", "primaryaddress", "zip", "residence", "city", "state", None),
    ("tax", "address", "zip", "property", "city", "state", "county"),
    ("tax", "owneraddressline1", "ownerzipcode", "owner_mailing", "ownercity", "ownerstate", None),
    ("utility", "address", "zip", "service", "city", "state", "county"),
    ("trace", "address", "zip", "residence", "city", "state", "county"),
    ("auto", "address", "zip", "registration", "city", None, None),
    ("loan", "address", "zip", "residence", "city", None, None),
    ("drive", "address", "zip", "license", None, None, None),
    ("voter", "address", "zip", "registration", None, None, None),
    ("criminal", "address", "zip", "record", "city", "state", None),
)

INDEXED_COLUMNS = (
    "id",
    "__norm_firstname",
    "__norm_lastname",
    "__norm_name_key",
    "__norm_address",
    "__norm_address_zip_key",
    "vin",
    "dl_num",
    "linkedinurl",
)


class GraphDbError(ValueError):
    """Raised when the graph database builder cannot complete."""


@dataclass(frozen=True)
class BuildResult:
    db_path: Path
    source_rows: dict[str, int]
    address_nodes: int
    address_edges: int
    person_entities: int = 0
    name_observations: int = 0
    entity_edges: int = 0


def normalize_text(value: str | None) -> str:
    return " ".join((value or "").strip().split()).casefold()


def normalize_phone(value: str | None) -> str:
    return "".join(ch for ch in (value or "") if ch.isdigit())


def normalize_address(value: str | None) -> str:
    return normalize_address_value(value or "").value


def zip5(value: str | None) -> str:
    return (value or "").strip()[:5]


def build_index(cleaned_dir: Path, db_path: Path) -> BuildResult:
    if not cleaned_dir.exists():
        raise GraphDbError(f"Cleaned directory does not exist: {cleaned_dir}")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()

    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    source_rows: dict[str, int] = {}
    try:
        _create_metadata_table(connection)
        for source, filename in SOURCE_FILES.items():
            csv_path = cleaned_dir / filename
            if not csv_path.exists():
                if source not in OPTIONAL_SOURCES:
                    raise GraphDbError(f"Missing cleaned CSV: {csv_path}")
                rows = _create_empty_source(connection, source, OPTIONAL_SOURCE_COLUMNS[source])
            else:
                rows = _import_csv(connection, source, csv_path)
            source_rows[source] = rows

        address_nodes, address_edges = _build_graph_tables(connection)
        person_entities, name_observations, entity_edges = _build_entity_tables(connection)
        connection.commit()
    finally:
        connection.close()

    return BuildResult(
        db_path=db_path,
        source_rows=source_rows,
        address_nodes=address_nodes,
        address_edges=address_edges,
        person_entities=person_entities,
        name_observations=name_observations,
        entity_edges=entity_edges,
    )


def _create_metadata_table(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE metadata (source TEXT PRIMARY KEY, path TEXT, rows INTEGER, imported_at TEXT, schema_hash TEXT)"
    )


def _import_csv(connection: sqlite3.Connection, source: str, path: Path) -> int:
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise GraphDbError(f"CSV has no header row: {path}")
        columns = list(reader.fieldnames)
        helper_columns = _helper_columns(columns)
        all_columns = [*columns, *helper_columns]
        connection.execute(f"DROP TABLE IF EXISTS {_q(source)}")
        connection.execute(
            f"CREATE TABLE {_q(source)} ({', '.join(_q(column) + ' TEXT' for column in all_columns)})"
        )
        rows = 0
        insert_sql = (
            f"INSERT INTO {_q(source)} ({', '.join(_q(column) for column in all_columns)}) "
            f"VALUES ({', '.join('?' for _ in all_columns)})"
        )
        for row in reader:
            enriched = dict(row)
            enriched.update(_helper_values(row, helper_columns, source))
            connection.execute(insert_sql, [enriched.get(column, "") for column in all_columns])
            rows += 1
        _create_source_indexes(connection, source, all_columns)
        connection.execute(
            "INSERT INTO metadata (source, path, rows, imported_at, schema_hash) VALUES (?, ?, ?, ?, ?)",
            (source, str(path), rows, datetime.now(UTC).isoformat(timespec="seconds"), _schema_hash(columns)),
        )
        return rows


def _create_empty_source(connection: sqlite3.Connection, source: str, columns: list[str]) -> int:
    helper_columns = _helper_columns(columns)
    all_columns = [*columns, *helper_columns]
    connection.execute(f"DROP TABLE IF EXISTS {_q(source)}")
    connection.execute(
        f"CREATE TABLE {_q(source)} ({', '.join(_q(column) + ' TEXT' for column in all_columns)})"
    )
    _create_source_indexes(connection, source, all_columns)
    connection.execute(
        "INSERT INTO metadata (source, path, rows, imported_at, schema_hash) VALUES (?, ?, ?, ?, ?)",
        (source, "", 0, datetime.now(UTC).isoformat(timespec="seconds"), _schema_hash(columns)),
    )
    return 0


def _helper_columns(columns: list[str]) -> list[str]:
    helpers = ["__norm_firstname", "__norm_lastname", "__norm_name_key", "__norm_address", "__norm_address_zip_key"]
    for column in columns:
        if column in {
            "phone",
            "mobile",
            "cellphone",
            "email",
            "email_02",
            "email_03",
            "owneraddressline1",
            "primaryaddress",
            "address",
            "street",
        }:
            helpers.append(f"__norm_{column}")
    return list(dict.fromkeys(helpers))


def _helper_values(row: dict[str, str], helpers: list[str], source: str) -> dict[str, str]:
    first = row.get("firstname", row.get("first_name", ""))
    last = row.get("lastname", row.get("last_name", ""))
    input_address = (row.get("input_address") or "").split(",", 1)[0]
    address = row.get("primaryaddress") or row.get("address") or row.get("street") or input_address
    zip_code = zip5(row.get("zip") or row.get("ownerzipcode"))
    values: dict[str, str] = {
        "__norm_firstname": normalize_text(first),
        "__norm_lastname": normalize_text(last),
        "__norm_name_key": f"{normalize_text(first)}|{normalize_text(last)}",
        "__norm_address": normalize_address(address),
        "__norm_address_zip_key": f"{normalize_address(address)}|{zip_code}",
    }
    for helper in helpers:
        if helper.startswith("__norm_") and helper not in values:
            column = helper.removeprefix("__norm_")
            value = row.get(column, "")
            if column in {"phone", "mobile", "cellphone"}:
                values[helper] = normalize_phone(value)
            elif column.startswith("email"):
                values[helper] = normalize_text(value)
            else:
                values[helper] = normalize_address(value)
    return values


def _create_source_indexes(connection: sqlite3.Connection, source: str, columns: list[str]) -> None:
    for column in INDEXED_COLUMNS:
        if column in columns:
            connection.execute(
                f"CREATE INDEX idx_graph_{source}_{column.strip('_')} ON {_q(source)} ({_q(column)})"
            )
    for column in columns:
        if column.startswith("__norm_") and column not in INDEXED_COLUMNS:
            connection.execute(
                f"CREATE INDEX idx_graph_{source}_{column.strip('_')} ON {_q(source)} ({_q(column)})"
            )


def _build_graph_tables(connection: sqlite3.Connection) -> tuple[int, int]:
    connection.execute("DROP TABLE IF EXISTS address_edges")
    connection.execute("DROP TABLE IF EXISTS addresses")
    connection.execute(
        """
        CREATE TABLE addresses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            norm_address TEXT NOT NULL,
            zip5 TEXT NOT NULL,
            street_number TEXT,
            street_name TEXT,
            unit TEXT,
            city TEXT,
            state TEXT,
            county TEXT,
            UNIQUE(norm_address, zip5)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE address_edges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            address_id INTEGER NOT NULL,
            source TEXT NOT NULL,
            source_rowid INTEGER NOT NULL,
            role TEXT NOT NULL,
            FOREIGN KEY(address_id) REFERENCES addresses(id)
        )
        """
    )

    _populate_addresses(connection)
    address_nodes = connection.execute("SELECT COUNT(*) FROM addresses").fetchone()[0]
    _populate_address_edges(connection)
    address_edges = connection.execute("SELECT COUNT(*) FROM address_edges").fetchone()[0]

    connection.execute("CREATE INDEX idx_addresses_norm_zip ON addresses (norm_address, zip5)")
    connection.execute("CREATE INDEX idx_address_edges_address_id ON address_edges (address_id)")
    connection.execute(
        "CREATE INDEX idx_address_edges_source_row ON address_edges (source, source_rowid, role)"
    )
    connection.execute("INSERT INTO metadata (source, path, rows, imported_at, schema_hash) VALUES (?, ?, ?, ?, ?)", (
        "addresses",
        "",
        address_nodes,
        datetime.now(UTC).isoformat(timespec="seconds"),
        _schema_hash(["norm_address", "zip5", "street_number", "street_name", "unit", "city", "state", "county"]),
    ))
    connection.execute("INSERT INTO metadata (source, path, rows, imported_at, schema_hash) VALUES (?, ?, ?, ?, ?)", (
        "address_edges",
        "",
        address_edges,
        datetime.now(UTC).isoformat(timespec="seconds"),
        _schema_hash(["address_id", "source", "source_rowid", "role"]),
    ))
    return address_nodes, address_edges


def _populate_addresses(connection: sqlite3.Connection) -> None:
    for source, address_col, zip_col, _role, city_col, state_col, county_col in ADDRESS_EDGE_SPECS:
        if not _table_exists(connection, source):
            continue
        norm_col = _norm_column_for_address(address_col)
        if norm_col not in _table_columns(connection, source):
            continue
        city_expr = _q(city_col) if city_col and city_col in _table_columns(connection, source) else "NULL"
        state_expr = _q(state_col) if state_col and state_col in _table_columns(connection, source) else "NULL"
        county_expr = _q(county_col) if county_col and county_col in _table_columns(connection, source) else "NULL"
        zip_expr = f"SUBSTR(TRIM({_q(zip_col)}), 1, 5)" if zip_col in _table_columns(connection, source) else "''"
        connection.execute(
            f"""
            INSERT OR IGNORE INTO addresses (norm_address, zip5, city, state, county)
            SELECT DISTINCT
                {_q(norm_col)},
                {zip_expr},
                {city_expr},
                {state_expr},
                {county_expr}
            FROM {_q(source)}
            WHERE TRIM({_q(norm_col)}) != ''
            """
        )
    connection.execute(
        """
        UPDATE addresses
        SET
            street_number = CASE
                WHEN norm_address GLOB '[0-9]* *' THEN SUBSTR(norm_address, 1, INSTR(norm_address, ' ') - 1)
                ELSE ''
            END,
            street_name = CASE
                WHEN norm_address GLOB '[0-9]* *' THEN SUBSTR(norm_address, INSTR(norm_address, ' ') + 1)
                ELSE norm_address
            END,
            unit = ''
        """
    )


def _populate_address_edges(connection: sqlite3.Connection) -> None:
    for source, address_col, zip_col, role, _city_col, _state_col, _county_col in ADDRESS_EDGE_SPECS:
        if not _table_exists(connection, source):
            continue
        norm_col = _norm_column_for_address(address_col)
        columns = _table_columns(connection, source)
        if norm_col not in columns:
            continue
        zip_expr = f"SUBSTR(TRIM({_q(zip_col)}), 1, 5)" if zip_col in columns else "''"
        connection.execute(
            f"""
            INSERT INTO address_edges (address_id, source, source_rowid, role)
            SELECT
                addresses.id,
                ?,
                {_q(source)}.rowid,
                ?
            FROM {_q(source)}
            JOIN addresses
              ON addresses.norm_address = {_q(source)}.{_q(norm_col)}
             AND addresses.zip5 = {zip_expr}
            WHERE TRIM({_q(source)}.{_q(norm_col)}) != ''
            """,
            (source, role),
        )


def _norm_column_for_address(address_col: str) -> str:
    return f"__norm_{address_col}"


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    rows = connection.execute(f"PRAGMA table_info({_q(table)})").fetchall()
    return {row[1] for row in rows}


def _q(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _schema_hash(columns: list[str]) -> str:
    return hashlib.sha256(json.dumps(columns).encode("utf-8")).hexdigest()


def _build_entity_tables(connection: sqlite3.Connection) -> tuple[int, int, int]:
    for table in (
        "entity_edges",
        "property_entities",
        "organization_entities",
        "vehicle_entities",
        "contact_entities",
        "name_observations",
        "person_entities",
    ):
        connection.execute(f"DROP TABLE IF EXISTS {_q(table)}")

    connection.execute(
        """
        CREATE TABLE person_entities (
            id TEXT PRIMARY KEY,
            base_rowid INTEGER NOT NULL,
            firstname TEXT,
            middlename TEXT,
            lastname TEXT,
            full_name TEXT,
            norm_name_key TEXT,
            primary_address_id INTEGER
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE name_observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            person_id TEXT,
            full_name TEXT,
            firstname TEXT,
            middlename TEXT,
            lastname TEXT,
            norm_name_key TEXT,
            source TEXT NOT NULL,
            source_rowid INTEGER NOT NULL,
            confidence REAL NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE contact_entities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            contact_type TEXT NOT NULL,
            value TEXT NOT NULL,
            UNIQUE(contact_type, value)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE vehicle_entities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            vin TEXT NOT NULL UNIQUE
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE organization_entities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            norm_name TEXT NOT NULL,
            org_type TEXT NOT NULL,
            UNIQUE(norm_name, org_type)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE property_entities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            property_key TEXT NOT NULL UNIQUE,
            source TEXT NOT NULL,
            source_rowid INTEGER NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE entity_edges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_type TEXT NOT NULL,
            from_id TEXT NOT NULL,
            to_type TEXT NOT NULL,
            to_id TEXT NOT NULL,
            relation_type TEXT NOT NULL,
            source TEXT NOT NULL,
            source_rowid INTEGER NOT NULL,
            confidence REAL NOT NULL,
            evidence_role TEXT NOT NULL
        )
        """
    )

    _populate_person_entities(connection)
    _populate_name_observations(connection)
    _populate_contact_entities(connection)
    _populate_vehicle_entities(connection)
    _populate_organization_entities(connection)
    _populate_property_entities(connection)
    _populate_entity_edges(connection)

    for table, columns in {
        "person_entities": ("id", "norm_name_key", "primary_address_id"),
        "name_observations": ("person_id", "norm_name_key", "source", "source_rowid"),
        "contact_entities": ("contact_type", "value"),
        "vehicle_entities": ("vin",),
        "organization_entities": ("norm_name", "org_type"),
        "property_entities": ("property_key", "source", "source_rowid"),
        "entity_edges": ("from_type", "from_id", "to_type", "to_id", "relation_type", "source"),
    }.items():
        for column in columns:
            connection.execute(f"CREATE INDEX idx_{table}_{column} ON {_q(table)} ({_q(column)})")

    person_entities = connection.execute("SELECT COUNT(*) FROM person_entities").fetchone()[0]
    name_observations = connection.execute("SELECT COUNT(*) FROM name_observations").fetchone()[0]
    entity_edges = connection.execute("SELECT COUNT(*) FROM entity_edges").fetchone()[0]
    for table, rows, columns in (
        ("person_entities", person_entities, ["id", "base_rowid", "norm_name_key"]),
        ("name_observations", name_observations, ["person_id", "full_name", "source", "source_rowid"]),
        ("entity_edges", entity_edges, ["from_type", "from_id", "to_type", "to_id", "relation_type"]),
    ):
        connection.execute(
            "INSERT INTO metadata (source, path, rows, imported_at, schema_hash) VALUES (?, ?, ?, ?, ?)",
            (table, "", rows, datetime.now(UTC).isoformat(timespec="seconds"), _schema_hash(columns)),
        )
    return person_entities, name_observations, entity_edges


def _populate_person_entities(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        INSERT INTO person_entities (id, base_rowid, firstname, middlename, lastname, full_name, norm_name_key, primary_address_id)
        SELECT
            base.id,
            base.rowid,
            base.firstname,
            base.middlename,
            base.lastname,
            TRIM(base.firstname || ' ' || COALESCE(NULLIF(base.middlename, ''), '') || ' ' || base.lastname),
            base.__norm_name_key,
            address_edges.address_id
        FROM base
        LEFT JOIN address_edges
          ON address_edges.source = 'base'
         AND address_edges.source_rowid = base.rowid
         AND address_edges.role = 'residence'
        WHERE TRIM(base.id) != ''
        """
    )


def _populate_name_observations(connection: sqlite3.Connection) -> None:
    for source in SOURCE_FILES:
        if not _table_exists(connection, source):
            continue
        columns = _table_columns(connection, source)
        first_col = "firstname" if "firstname" in columns else "first_name" if "first_name" in columns else None
        last_col = "lastname" if "lastname" in columns else "last_name" if "last_name" in columns else None
        if not first_col or not last_col or "__norm_name_key" not in columns:
            continue
        middle_expr = _q("middlename") if "middlename" in columns else _q("middle_name") if "middle_name" in columns else "''"
        person_expr = _q("id") if "id" in columns else "NULL"
        connection.execute(
            f"""
            INSERT INTO name_observations (person_id, full_name, firstname, middlename, lastname, norm_name_key, source, source_rowid, confidence)
            SELECT
                {person_expr},
                TRIM({_q(first_col)} || ' ' || COALESCE(NULLIF({middle_expr}, ''), '') || ' ' || {_q(last_col)}),
                {_q(first_col)},
                {middle_expr},
                {_q(last_col)},
                __norm_name_key,
                ?,
                rowid,
                CASE WHEN {person_expr} IS NOT NULL AND TRIM({person_expr}) != '' THEN 1.0 ELSE 0.55 END
            FROM {_q(source)}
            WHERE TRIM(__norm_name_key) != '|'
            """,
            (source,),
        )


def _populate_contact_entities(connection: sqlite3.Connection) -> None:
    contact_columns = {"phone": "phone", "mobile": "phone", "cellphone": "phone", "email": "email", "email_02": "email", "email_03": "email"}
    for source in SOURCE_FILES:
        if not _table_exists(connection, source):
            continue
        columns = _table_columns(connection, source)
        for column, contact_type in contact_columns.items():
            norm_column = f"__norm_{column}"
            if norm_column not in columns:
                continue
            connection.execute(
                f"""
                INSERT OR IGNORE INTO contact_entities (contact_type, value)
                SELECT DISTINCT ?, {_q(norm_column)}
                FROM {_q(source)}
                WHERE TRIM({_q(norm_column)}) != ''
                """,
                (contact_type,),
            )


def _populate_vehicle_entities(connection: sqlite3.Connection) -> None:
    if _table_exists(connection, "auto") and "vin" in _table_columns(connection, "auto"):
        connection.execute("INSERT OR IGNORE INTO vehicle_entities (vin) SELECT DISTINCT vin FROM auto WHERE TRIM(vin) != ''")


def _populate_organization_entities(connection: sqlite3.Connection) -> None:
    specs = (
        ("loan", "employer", "employer"),
        ("tax", "lendername", "lender"),
        ("tax", "ownercompany", "owner_company"),
        ("linkedin", "position_companyname", "employer"),
    )
    for source, column, org_type in specs:
        if _table_exists(connection, source) and column in _table_columns(connection, source):
            connection.execute(
                f"""
                INSERT OR IGNORE INTO organization_entities (name, norm_name, org_type)
                SELECT DISTINCT {_q(column)}, lower(trim({_q(column)})), ?
                FROM {_q(source)}
                WHERE TRIM({_q(column)}) != ''
                """,
                (org_type,),
            )


def _populate_property_entities(connection: sqlite3.Connection) -> None:
    if _table_exists(connection, "tax"):
        connection.execute(
            """
            INSERT OR IGNORE INTO property_entities (property_key, source, source_rowid)
            SELECT DISTINCT 'tax:' || rowid, 'tax', rowid
            FROM tax
            WHERE TRIM(COALESCE(address, '')) != ''
            """
        )


def _populate_entity_edges(connection: sqlite3.Connection) -> None:
    _insert_address_entity_edges(connection)
    _insert_contact_edges(connection)
    _insert_vehicle_edges(connection)


def _insert_address_entity_edges(connection: sqlite3.Connection) -> None:
    role_map = {
        "residence": "RESIDENCE",
        "property": "PROPERTY",
        "owner_mailing": "OWNER_MAILING",
        "service": "SERVICE",
        "registration": "REGISTRATION",
        "license": "LICENSE",
        "record": "RECORD",
    }
    union_sql = "\nUNION ALL ".join(
        f"SELECT '{source}' AS source, rowid, id FROM {_q(source)}"
        for source in ("base", *ID_LINKED_SOURCES)
        if _table_exists(connection, source) and "id" in _table_columns(connection, source)
    )
    if not union_sql:
        return
    for role, relation in role_map.items():
        connection.execute(
            f"""
            INSERT INTO entity_edges (from_type, from_id, to_type, to_id, relation_type, source, source_rowid, confidence, evidence_role)
            SELECT 'person', src.id, 'address', CAST(address_edges.address_id AS TEXT), ?, address_edges.source, address_edges.source_rowid,
                   CASE WHEN address_edges.source = 'base' THEN 1.0 ELSE 0.8 END, address_edges.role
            FROM address_edges
            JOIN ({union_sql}) AS src
              ON src.source = address_edges.source
             AND src.rowid = address_edges.source_rowid
            WHERE address_edges.role = ? AND TRIM(src.id) != ''
            """,
            (relation, role),
        )


def _insert_contact_edges(connection: sqlite3.Connection) -> None:
    contact_columns = {"phone": "phone", "mobile": "phone", "cellphone": "phone", "email": "email", "email_02": "email", "email_03": "email"}
    for source in ("base", *ID_LINKED_SOURCES):
        if not _table_exists(connection, source):
            continue
        columns = _table_columns(connection, source)
        if "id" not in columns:
            continue
        for column, contact_type in contact_columns.items():
            norm_column = f"__norm_{column}"
            if norm_column not in columns:
                continue
            connection.execute(
                f"""
                INSERT INTO entity_edges (from_type, from_id, to_type, to_id, relation_type, source, source_rowid, confidence, evidence_role)
                SELECT 'person', {_q(source)}.id, 'contact', CAST(contact_entities.id AS TEXT), 'CONTACT', ?, {_q(source)}.rowid, 0.9, ?
                FROM {_q(source)}
                JOIN contact_entities
                  ON contact_entities.contact_type = ?
                 AND contact_entities.value = {_q(source)}.{_q(norm_column)}
                WHERE TRIM({_q(source)}.id) != '' AND TRIM({_q(source)}.{_q(norm_column)}) != ''
                """,
                (source, column, contact_type),
            )


def _insert_vehicle_edges(connection: sqlite3.Connection) -> None:
    if not _table_exists(connection, "auto") or "vin" not in _table_columns(connection, "auto"):
        return
    connection.execute(
        """
        INSERT INTO entity_edges (from_type, from_id, to_type, to_id, relation_type, source, source_rowid, confidence, evidence_role)
        SELECT 'person', auto.id, 'vehicle', CAST(vehicle_entities.id AS TEXT), 'VEHICLE', 'auto', auto.rowid, 0.95, 'vin'
        FROM auto
        JOIN vehicle_entities ON vehicle_entities.vin = auto.vin
        WHERE TRIM(auto.id) != '' AND TRIM(auto.vin) != ''
        """
    )
