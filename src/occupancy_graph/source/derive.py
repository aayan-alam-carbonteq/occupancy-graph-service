"""Derived-field functions used by the shape manifest.

Every function takes a whole partner row (a mapping of column name -> value, with
`raw_data` already decoded to a dict) and returns a string or None. They must
never raise on malformed input: the partner corpus contains column-shifted rows,
and a projection crash would take down an entire investigation.
"""
from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from datetime import date
from typing import Any

# FIPS state+county -> county name. Only the codes the fixture and our live test
# addresses need; unknown codes return None rather than a guess. The partner
# stores fipsState/fipsCounty, while the contract's `county` field is a name.
_FIPS_COUNTIES: dict[tuple[str, str], str] = {
    ("21", "067"): "FAYETTE",
    ("21", "111"): "JEFFERSON",
    ("44", "007"): "PROVIDENCE",
    ("17", "089"): "KANE",
}

# Tokens that mark an owner as a company, trust or estate rather than a person.
_COMPANY_TOKENS = {
    "LLC", "L L C", "INC", "INCORPORATED", "CORP", "CORPORATION", "CO",
    "LP", "LLP", "LTD", "TRUST", "TRUSTEE", "PROPERTIES", "HOLDINGS",
    "PARTNERS", "PARTNERSHIP", "ASSOCIATES", "ENTERPRISES", "INVESTMENTS",
    "REALTY", "MANAGEMENT", "GROUP", "FOUNDATION", "CHURCH", "ESTATE",
    "BANK", "ASSOCIATION", "AUTHORITY", "COMPANY",
}


def _raw(row: Mapping[str, Any]) -> Mapping[str, Any]:
    value = row.get("raw_data")
    return value if isinstance(value, Mapping) else {}


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def synthetic_id(row: Mapping[str, Any]) -> str | None:
    """Stable per-row id. The partner's record_id is unique across the corpus."""
    record_id = row.get("record_id")
    return None if record_id is None else str(record_id)


def tax_zip5(row: Mapping[str, Any]) -> str | None:
    """ZIP for a property_owner row.

    The `zip` column is 0% populated on these rows; the value lives in
    raw_data.zipCodePlusFour as "40505-1046". On column-shifted rows that key
    holds "True"/"False", so anything that is not five leading digits is refused.
    """
    column = _text(row.get("zip"))
    if re.fullmatch(r"\d{5}", column[:5]) and column:
        return column[:5]
    candidate = _text(_raw(row).get("zipCodePlusFour"))
    match = re.match(r"^(\d{5})", candidate)
    return match.group(1) if match else None


def fips_county(row: Mapping[str, Any]) -> str | None:
    raw = _raw(row)
    state = _text(raw.get("fipsState")).zfill(2)
    county = _text(raw.get("fipsCounty")).zfill(3)
    return _FIPS_COUNTIES.get((state, county))


def company_from_owner_name(row: Mapping[str, Any]) -> str | None:
    """Recover the missing `ownercompany` field from the owner name string.

    The partner has no ownercompany column, but company/trust ownership is
    visible in ownerName. `company_or_trust_owner` is the only consumer, and it
    needs presence, not a parsed entity name — so the whole string is returned.
    """
    owner = _text(_raw(row).get("ownerName"))
    if not owner:
        return None
    tokens = {t for t in re.split(r"[^A-Z]+", owner.upper()) if t}
    return owner if tokens & _COMPANY_TOKENS else None


def house_number(row: Mapping[str, Any]) -> str | None:
    """Leading house number of the free-text address.

    The `house_number` column is 0% populated on every feed the adapter reads,
    so it is always parsed. Addresses like "ESTHER ST" legitimately have none.
    """
    match = re.match(r"^\s*(\d+)\b", _text(row.get("address")))
    return match.group(1) if match else None


def _as_date(value: Any) -> date | None:
    return value if isinstance(value, date) else None


def dob_year(row: Mapping[str, Any]) -> str | None:
    value = _as_date(row.get("dob"))
    return None if value is None else str(value.year)


def dob_month(row: Mapping[str, Any]) -> str | None:
    value = _as_date(row.get("dob"))
    return None if value is None else str(value.month)


def dob_day(row: Mapping[str, Any]) -> str | None:
    value = _as_date(row.get("dob"))
    return None if value is None else str(value.day)


def year_of(column: str) -> Callable[[Mapping[str, Any]], str | None]:
    """Build a function returning the year part of a date column.

    `__name__` MUST be distinguishing: manifest.derived() takes the origin's
    comparable `key` from it, and every closure out of a factory would otherwise
    be named `_fn`, making two derived origins compare equal again.
    """

    def _fn(row: Mapping[str, Any]) -> str | None:
        value = _as_date(row.get(column))
        return None if value is None else str(value.year)

    _fn.__name__ = f"year_of_{column}"
    return _fn
