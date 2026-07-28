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
# Every entry MUST be a single alphabetic word: company_from_owner_name()
# tokenizes ownerName with re.split(r"[^A-Z]+", ...), which splits on any
# non-alpha character, so a multi-word entry (e.g. the old "L L C") can never
# equal one of the resulting tokens and is silently unreachable.
_COMPANY_TOKENS = {
    "LLC", "INC", "INCORPORATED", "CORP", "CORPORATION", "CO",
    "LP", "LLP", "LTD", "TRUST", "TRUSTEE", "TR", "PROPERTIES", "HOLDINGS",
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

    An all-digit `zip` column longer than 5 (e.g. a dash-less ZIP+4 like
    "405051046") is truncated to its first 5 digits rather than rejected — that
    is intentional, not a bug: it recovers the same 5-digit ZIP a dashed value
    would have produced.
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

    This is a token-set heuristic, not a parser, and it has known limits that
    are deliberately left alone rather than tuned against hand-picked strings:

    - Multi-word entity forms without a single-word marker are missed, e.g.
      "CITY OF LEXINGTON", "HABITAT FOR HUMANITY".
    - Punctuated abbreviations are missed because the tokenizer splits on any
      non-alpha character, e.g. "ACME L.L.C." tokenizes to {"L", "L", "C"},
      none of which is a whole-word match (and adding "C" as a token would
      false-positive on countless initials, so it stays out).
    - False positives happen when a person's surname is itself a company
      token, e.g. "CHURCH, MARY" (a person surnamed Church) is flagged. This
      is low-cost: the consuming engine still sees the raw ownerName and can
      reason about it directly.
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


def first_raw(*keys: str) -> Callable[[Mapping[str, Any]], str | None]:
    """First non-empty value among several raw_data keys.

    The auto feed ships VIN/Vin/VIN_ID and MAKE/Make across different source
    files; this is upstream inconsistency, not defensive padding.

    `__name__` MUST be distinguishing — see the note on year_of().
    """

    def _fn(row: Mapping[str, Any]) -> str | None:
        raw = _raw(row)
        for key in keys:
            value = _text(raw.get(key))
            if value:
                return value
        return None

    _fn.__name__ = "first_raw_" + "_".join(keys)
    return _fn
