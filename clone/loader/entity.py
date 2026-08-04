"""Build silver.entity_master / entity_links the way production builds them: by
blocking on ssn.

Production's entity_links is 100% match_type='ssn' (sampled), and feed_id_coverage
shows ssn exists almost only in the payday feeds (Payday_Big 95.8%, PD loan_master
77%, 24mm 100%) -- it is 0.0% on utility, trace, base, auto and property_owner
(tax). That single fact CAUSES both the 97.5:2.5 records_new:records_legacy link
skew AND tax's total absence from the graph. So this module does exactly what
production does -- link ONLY rows that carry an ssn -- and lets that skew and
that absence EMERGE from the data, rather than being imposed by a rule that
special-cases tax or records_legacy. (In this clone the effect is even more
extreme than production's 97.5:2.5: `clone/profiles/feed_population.json` gives
ssn a nonzero target on `loan` alone, and loan is routed to records_new only
-- see feedplan.py -- so every link here is records_new, 0 records_legacy.)

Two classes of field on entity_master, and conflating them is a correctness bug:

  STAMPED    identity_confidence, is_suspicious, is_merged. Drawn, by a
             deterministic hash of the entity's ordinal, to match production's
             MEASURED distribution (mean 41.52, modal 40.50, 31.4% suspicious)
             -- source/search.py's discounting logic needs a realistic spread
             to exercise, not a constant every row shares.
  EMERGENT   record_count. This MUST equal the number of entity_links rows
             this module just built for that hal_id -- never stamped to
             production's 2.65 mean, because search_people ORDERS BY
             record_count and a stamped value would make entity_master
             contradict its own links.

hal_id assignment is a plain ordinal over the SORTED set of distinct ssns, not
a hash: stable (the same input rows produce the same ids on every run),
collision-free by construction (each distinct ssn gets exactly one ordinal),
and always <= 15 characters ("HAL" + 12 digits), matching entity_master.hal_id's
char(15).
"""
from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from typing import Any

MATCH_TYPE = "ssn"
# Production's sampled links average confidence 0.99. entity_links.confidence is
# numeric(3,2) -- 3 significant digits, 2 after the point -- which 0.99 fits.
LINK_CONFIDENCE = 0.99

_MODAL_CONFIDENCE = 40.50           # search.py: "MODAL at 40.50 -- 27.5% of rows"
_CONFIDENCE_FLOOR = 34.0            # search.py: "spread across the 34-70 band"
_CONFIDENCE_SPREAD = 36.0           # 34.0 + 36.0 = 70.0, the band's top
_MODAL_SHARE = 2_750                # /10_000 -> 27.5% sit exactly on the mode
_SUSPICIOUS_SHARE = 314             # /1_000 -> 31.4%, production's measured rate
_MERGED_SHARE = 20                  # /1_000 -> 2%, "a small minority"
# Shapes the non-modal 72.5% toward the low end of the band. A uniform draw
# across [34, 70] alone would carry a mean of ~52 -- well above production's
# measured 41.52 -- because it gives the long tail up to 70 as much weight as
# the mass near the mode. For frac uniform on [0, 1), frac ** _SPREAD_SKEW has
# E[] = 1 / (_SPREAD_SKEW + 1); 3.5 is the value that lands the OVERALL
# weighted mean (27.5% at 40.50, 72.5% skewed-low across the band) at 41.52,
# pinned by test_stamped_metadata_matches_the_measured_production_distribution.
_SPREAD_SKEW = 3.5


def _hash_unit(seed: str, salt: str, modulus: int) -> int:
    """A deterministic, uniform-ish int in [0, modulus) -- never RNG.

    The clone must be reproducible: the same ordinal has to draw the same
    stamped value on every run, or nothing measured against the entity graph
    stays comparable from one load to the next.
    """
    digest = hashlib.blake2b(f"{salt}|{seed}".encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big") % modulus


def stamped_confidence(person_id: int) -> float:
    """identity_confidence: modal at 40.50, the rest spread across 34-70.

    See the module-level _SPREAD_SKEW comment for why the spread is skewed
    toward its low end rather than uniform -- a uniform draw pulls the mean to
    ~52, well above production's measured 41.52.
    """
    bucket = _hash_unit(str(person_id), "conf", 10_000)
    if bucket < _MODAL_SHARE:
        return _MODAL_CONFIDENCE
    frac = (bucket - _MODAL_SHARE) / (10_000 - _MODAL_SHARE)
    return round(_CONFIDENCE_FLOOR + (frac ** _SPREAD_SKEW) * _CONFIDENCE_SPREAD, 2)


def stamped_suspicious(person_id: int) -> bool:
    """is_suspicious at production's measured 31.4% rate."""
    return _hash_unit(str(person_id), "susp", 1_000) < _SUSPICIOUS_SHARE


def stamped_merged(person_id: int) -> bool:
    """is_merged on a small minority of entities.

    Production computes merges and NEVER applies them -- both sides of a merge
    stay resident in entity_master, which is exactly what exercises
    search_people's `is_merged IS NOT TRUE` filter. This clone has no genuine
    duplicate-entity pairs to merge (one ssn is exactly one entity, by
    construction of build_entities), so the flag is stamped standalone rather
    than wired to a real counterpart; what matters for the consumer is that
    some rows carry it and search_people must filter them out.
    """
    return _hash_unit(str(person_id), "merge", 1_000) < _MERGED_SHARE


def _most_populated(members: list[Mapping[str, Any]]) -> Mapping[str, Any]:
    """The cluster member with the most non-empty fields -- the canonical
    record production's own selection logic would tend to prefer."""
    return max(members, key=lambda member: sum(1 for value in member.values() if value))


def build_entities(
    rows: Iterable[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Block on ssn exactly as production does; return (masters, links).

    A row with no ssn is simply never linked -- there is no special-case rule
    that excludes tax or prefers records_new, because none is needed: both are
    absent/skewed in the result purely as a CONSEQUENCE of ssn coverage, the
    same way they are in production.

    `rows` need only carry record_id, source_table and ssn to be linkable;
    first_name/last_name/address/city/state/zip are read if present (via
    `.get`) to build the canonical identity but are not required.
    """
    by_ssn: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        ssn = row.get("ssn")
        if ssn:
            by_ssn.setdefault(ssn, []).append(row)

    masters: list[dict[str, Any]] = []
    links: list[dict[str, Any]] = []
    for ordinal, (ssn, members) in enumerate(sorted(by_ssn.items())):
        hal_id = f"HAL{ordinal:012d}"
        for member in members:
            links.append({
                "hal_id": hal_id,
                "source_table": member["source_table"],
                "record_id": member["record_id"],
                "match_type": MATCH_TYPE,
                "confidence": LINK_CONFIDENCE,
            })
        best = _most_populated(members)
        masters.append({
            "hal_id": hal_id,
            "canonical_ssn": ssn,
            "canonical_first_name": best.get("first_name"),
            "canonical_last_name": best.get("last_name"),
            "canonical_address_line1": best.get("address"),
            "canonical_city": best.get("city"),
            "canonical_state": best.get("state"),
            "canonical_zip": best.get("zip"),
            "canonical_source_table": best["source_table"],
            "canonical_record_id": best["record_id"],
            "record_count": len(members),                    # EMERGENT -- see module docstring
            "identity_confidence": stamped_confidence(ordinal),
            "is_suspicious": stamped_suspicious(ordinal),
            "is_merged": stamped_merged(ordinal),
        })
    return masters, links
