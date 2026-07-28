"""Derived-field functions. Filled in by Task 6."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def synthetic_id(row: Mapping[str, Any]) -> str:
    raise NotImplementedError


def tax_zip5(row: Mapping[str, Any]) -> str | None:
    raise NotImplementedError


def fips_county(row: Mapping[str, Any]) -> str | None:
    raise NotImplementedError


def company_from_owner_name(row: Mapping[str, Any]) -> str | None:
    raise NotImplementedError
