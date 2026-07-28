"""Normalizers shared by the adapter. Moved out of the deleted graphdb package.

These ran at index-build time under SQLite; they now run at query time. The
replacement table is calibrated against the consuming engine's address matching
and must not be changed casually.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_REPLACEMENTS = {
    "AVENUE": "AVE", "STREET": "ST", "DRIVE": "DR", "ROAD": "RD", "COURT": "CT",
    "LANE": "LN", "BOULEVARD": "BLVD", "PARKWAY": "PKWY", "PLACE": "PL",
    "CIRCLE": "CIR", "TERRACE": "TER", "HIGHWAY": "HWY", "APARTMENT": "APT",
    "SUITE": "STE",
}


@dataclass(frozen=True)
class NormalizeResult:
    value: str
    changed: bool


def normalize_address_value(value: str) -> NormalizeResult:
    original = str(value or "").strip()
    if not original:
        return NormalizeResult(value="", changed=value != "")
    normalized = original.upper()
    normalized = re.sub(r"[^A-Z0-9\s#]", " ", normalized)
    for source, target in _REPLACEMENTS.items():
        normalized = re.sub(rf"\b{source}\b", target, normalized)
    normalized = re.sub(r"\bTRACE\b(?=(?:\s+#|\s+APT|\s+STE|$))", "TRCE", normalized)
    normalized = re.sub(r"\b(?:PK|PRK)\b(?=(?:\s+#|\s+APT|\s+STE|$))", "PARK", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return NormalizeResult(value=normalized, changed=normalized != original)


def normalize_address(value: str | None) -> str:
    return normalize_address_value(value or "").value


def normalize_text(value: str | None) -> str:
    return " ".join((value or "").strip().split()).casefold()


def normalize_phone(value: str | None) -> str:
    return "".join(ch for ch in (value or "") if ch.isdigit())


def name_key(first: str | None, last: str | None) -> str:
    return f"{normalize_text(first)}|{normalize_text(last)}"


def zip5(value: str | None) -> str:
    return (value or "").strip()[:5]
