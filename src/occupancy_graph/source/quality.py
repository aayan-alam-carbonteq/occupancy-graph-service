"""Defensive gates over known partner-data defects.

All three gates FAIL CLOSED. A dropped row is recoverable; a row whose owner name
was lifted out of the wrong column is not — it would be reported to the model as
fact and reasoned over.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

_BOOLEANS = {"True", "False"}

_OWN_RENT = {
    "RENT": "RENT", "R": "RENT",
    "OWN": "OWN", "O": "OWN",
}

# Trace raw_data degrades badly past the leading fields. Observed validity in
# production: Record_Date 82.6%, Date_Of_Birth_Year 31.9%,
# Home_Owner_Renter_Code 14.1%, Number_of_Bedrooms 1.1%.
_TRACE_PATTERNS: dict[str, re.Pattern[str]] = {
    "Record_Date": re.compile(r"^(19|20)\d{2}(\d{2}){1,2}$"),
    "Date_Of_Birth_Year": re.compile(r"^(19|20)\d{2}$"),
    "Date_Of_Birth_Month": re.compile(r"^\d{1,2}$"),
    "Date_Of_Birth_Day": re.compile(r"^\d{1,2}$"),
    "Home_Built_Year": re.compile(r"^(18|19|20)\d{2}$"),
    "Home_Purchase_Date": re.compile(r"^(19|20)\d{2}(\d{2}){1,2}$"),
    "Home_Owner_Renter_Code": re.compile(r"^[ORH9]$"),
    "Number_of_Bedrooms": re.compile(r"^\d{1,2}$"),
    "Number_of_Bathrooms": re.compile(r"^\d{1,2}(\.\d)?$"),
}


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def tax_row_is_usable(row: Mapping[str, Any]) -> bool:
    """Reject column-shifted property_owner rows.

    Rows containing the `location` GeoJSON point were split on its embedded
    commas by a naive CSV parser upstream, sliding every later field. About 17.5%
    of property_owner rows are affected. Three independent indicators; any one
    failing rejects the row.
    """
    if not isinstance(row, Mapping):
        return False
    raw = row.get("raw_data")
    if not isinstance(raw, Mapping):
        return False
    street_number = _text(raw.get("streetNumber"))
    if street_number in _BOOLEANS or (street_number and not street_number.isdigit()):
        return False
    zip_plus_four = _text(raw.get("zipCodePlusFour"))
    if zip_plus_four and not re.match(r"^\d{5}", zip_plus_four):
        return False
    residential = _text(raw.get("residential"))
    if residential and residential not in _BOOLEANS:
        return False
    # The gate exists to stop a mislabelled owner reaching the model. If there is
    # no ownerName there is nothing to mislabel, so sparse rows are fine. But when
    # an owner name IS present we require corroboration that the row is aligned:
    # `residential` is 100% populated on real property_owner rows, so its absence
    # is itself anomalous. Without this, a shift severe enough to blank all three
    # signals would pass — absence of evidence read as evidence of soundness.
    if _text(raw.get("ownerName")) and residential not in _BOOLEANS:
        return False
    return True


def normalize_own_rent(value: Any) -> str | None:
    """Collapse the observed casings to OWN/RENT.

    Production distribution: RENT 29.3%, OWN 26.8%, "1" 11.6%, plus rent/own/
    Rent/Own/r/o. "1" and anything unrecognised become None rather than a guess.
    """
    text = _text(value).upper()
    return _OWN_RENT.get(text)


def coerce_trace_field(key: str, value: Any) -> str | None:
    """Null out trace raw_data values that do not match their expected shape."""
    text = _text(value)
    if not text:
        return None
    pattern = _TRACE_PATTERNS.get(key)
    if pattern is None:
        return text
    return text if pattern.match(text) else None
