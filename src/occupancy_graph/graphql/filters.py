from __future__ import annotations

from enum import Enum
from typing import Any

import strawberry

from occupancy_graph.graphql.db import quote
from occupancy_graph.graphql.registry import TableSchema


@strawberry.input
class StringFilterInput:
    eq: str | None = None
    ne: str | None = None
    contains: str | None = None
    gt: str | None = None
    lt: str | None = None
    is_null: bool | None = None
    in_: list[str] | None = strawberry.field(default=None, name="in")


@strawberry.enum
class OrderDirection(Enum):
    ASC = "ASC"
    DESC = "DESC"


@strawberry.input
class OrderByInput:
    field: str
    direction: OrderDirection = OrderDirection.ASC


def resolve_column(schema: TableSchema, field: str) -> str | None:
    if field in schema.columns:
        return field
    norm_name = f"__norm_{field}"
    if norm_name in schema.norm_columns:
        return norm_name
    return None


def build_where_clause(schema: TableSchema, where: Any | None) -> tuple[str, list[Any]]:
    if where is None:
        return "", []
    clauses: list[str] = []
    values: list[Any] = []
    data = _input_to_dict(where)
    for field, filter_value in data.items():
        if filter_value is None:
            continue
        column = resolve_column(schema, field)
        if column is None:
            continue
        filter_data = _input_to_dict(filter_value)
        column_sql = quote(column)
        if filter_data.get("is_null") is True:
            clauses.append(f"({column_sql} IS NULL OR TRIM({column_sql}) = '')")
            continue
        if filter_data.get("is_null") is False:
            clauses.append(f"({column_sql} IS NOT NULL AND TRIM({column_sql}) != '')")
        if "eq" in filter_data and filter_data["eq"] is not None:
            clauses.append(f"{column_sql} = ?")
            values.append(filter_data["eq"])
        if "ne" in filter_data and filter_data["ne"] is not None:
            clauses.append(f"{column_sql} != ?")
            values.append(filter_data["ne"])
        if "contains" in filter_data and filter_data["contains"] is not None:
            clauses.append(f"{column_sql} LIKE ?")
            values.append(f"%{filter_data['contains']}%")
        if "gt" in filter_data and filter_data["gt"] is not None:
            clauses.append(f"{column_sql} > ?")
            values.append(filter_data["gt"])
        if "lt" in filter_data and filter_data["lt"] is not None:
            clauses.append(f"{column_sql} < ?")
            values.append(filter_data["lt"])
        if "in_" in filter_data and filter_data["in_"]:
            placeholders = ", ".join("?" for _ in filter_data["in_"])
            clauses.append(f"{column_sql} IN ({placeholders})")
            values.extend(filter_data["in_"])
    if not clauses:
        return "", []
    return " WHERE " + " AND ".join(clauses), values


def build_order_clause(schema: TableSchema, order_by: list[OrderByInput] | None) -> str:
    if not order_by:
        return ""
    parts: list[str] = []
    for item in order_by:
        column = resolve_column(schema, item.field)
        if column is None:
            continue
        direction = "DESC" if item.direction == OrderDirection.DESC else "ASC"
        parts.append(f"{quote(column)} {direction}")
    if not parts:
        return ""
    return " ORDER BY " + ", ".join(parts)


def _input_to_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if hasattr(value, "__dict__"):
        return {key: val for key, val in vars(value).items() if not key.startswith("_")}
    return {}
