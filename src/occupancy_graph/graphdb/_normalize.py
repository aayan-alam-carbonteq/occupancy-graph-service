"""Vendored address normalizer (copied from occupancy-engine data_tools).

Kept local so the graph service depends on nothing from occupancy-engine.
This is a stable primitive; a golden-value parity test guards drift.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


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
    replacements = {
        "AVENUE": "AVE",
        "STREET": "ST",
        "DRIVE": "DR",
        "ROAD": "RD",
        "COURT": "CT",
        "LANE": "LN",
        "BOULEVARD": "BLVD",
        "PARKWAY": "PKWY",
        "PLACE": "PL",
        "CIRCLE": "CIR",
        "TERRACE": "TER",
        "HIGHWAY": "HWY",
        "APARTMENT": "APT",
        "SUITE": "STE",
    }
    for source, target in replacements.items():
        normalized = re.sub(rf"\b{source}\b", target, normalized)
    normalized = re.sub(r"\bTRACE\b(?=(?:\s+#|\s+APT|\s+STE|$))", "TRCE", normalized)
    normalized = re.sub(r"\b(?:PK|PRK)\b(?=(?:\s+#|\s+APT|\s+STE|$))", "PARK", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return NormalizeResult(value=normalized, changed=normalized != original)
