"""Person clustering, synthetic SSNs, and the drive<->loan join key.

WHY UNION-FIND, NOT GROUPING BY (name, address): the clone exists to support
the engine's "owner elsewhere" heuristic, whose entire job is finding an
owner's records at OTHER addresses. If we grouped rows by (name, address),
every person who moved would become two separate entities and that traversal
would return nothing. Trace has ~1.84 addresses per person (max 17), so
movers are common, not an edge case. Union-find with transitive closure keeps
one person one entity across every address they have ever carried.

WHY A SYNTHETIC SSN: production's entity graph blocks on SSN (100% of sampled
links are match_type='ssn'), and SSN exists almost only on the payday feeds.
That single fact causes both the 97.5:2.5 records_new:records_legacy link
skew AND tax's total absence from the graph. We reproduce the CAUSE --
synthetic SSNs populated only where production has them -- and let the
profile emerge from there, rather than hand-tuning link rates to match.

WHY THE DRIVE JOIN KEY IS (address, zip, first, last), NOT id: measured on the
real CSVs, (id, first, last) matches only 29.8% of drive rows to a loan row,
while (address, zip, first, last) matches 55.4%. `id` is not a cross-shape
key -- it is a synthetic per-row id (see manifest.py's derive.synthetic_id),
so the SAME id string collides across shapes for unrelated people: cd076219
is GARY HILES in loan.csv and BRIANNA HILES in drive.csv. It is only reliable
WITHIN a shape.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

# The blocking keys tried for every row that carries a surname, most confident
# first. Confidence order matters only in that a stronger signal (dob) should
# not be starved of the chance to link two nodes a weaker signal (address)
# already separated into different components -- every match still gets
# unioned in, so a row can link on more than one key and the components merge
# transitively (test_transitive_closure_links_a_chain: A-B share a phone,
# B-C share a dob, and A ends up with C through B even though A and C share
# nothing directly).
_DOB, _PHONE, _ADDRESS = "dob", "phone", "address"

# 900-999 is an area range the SSA has never issued (areas above 772 were
# never assigned to a state before the 2011 randomization, and 900-999 were
# carved out from the start for ITINs/other non-SSN uses). A synthetic SSN
# drawn from it can therefore never collide with a real person's SSN, no
# matter how the corpus grows.
_SSN_AREA_LOW = 900
_SSN_AREA_SPAN = 100  # 900..999 inclusive
# person_id decomposes as area-offset (2 digits) | group (2 digits) |
# serial (4 digits) -- a direct radix split of person_id, not a hash. That
# makes the mapping a bijection on person_id in [0, 100_000_000), which is
# what guarantees distinctness rather than merely making collisions unlikely
# (a hash into the same ~1e8-value space would carry a real birthday-bound
# collision risk at corpus scale).
_SSN_CAPACITY = _SSN_AREA_SPAN * 1_000_000

_NON_DIGIT = re.compile(r"\D+")


def synthetic_ssn(person_id: int) -> str:
    """Deterministic 9-digit synthetic SSN in the never-issued 900-999 area.

    Distinct person_ids in [0, 100_000_000) always produce distinct SSNs --
    this is an exact radix decomposition of person_id, not a hash, so there
    is no birthday-bound collision risk at any corpus size we will ever load.
    """
    if not 0 <= person_id < _SSN_CAPACITY:
        raise ValueError(f"person_id {person_id} out of synthetic SSN capacity "
                          f"(0..{_SSN_CAPACITY - 1})")
    area = _SSN_AREA_LOW + person_id % _SSN_AREA_SPAN
    remainder = person_id // _SSN_AREA_SPAN
    group, serial = remainder % 100, remainder // 100
    return f"{area:03d}{group:02d}{serial:04d}"


def DRIVE_JOIN_KEY(row: Mapping[str, Any]) -> tuple[str, str, str, str]:
    """(address, zip, first, last) -- see the module docstring for why this
    beats `id` as the drive<->loan join key. zip is kept as-is: it is already
    a clean digit string out of csvsource, and unlike address/name it has no
    casing to normalize."""
    return (
        (row.get("address") or "").strip().upper(),
        row.get("zip") or "",
        (row.get("firstname") or "").strip().upper(),
        (row.get("lastname") or "").strip().upper(),
    )


class PersonIndex:
    """Union-find over rows, blocked by (first, last, dob/phone/address+zip).

    A row with no surname never joins a cluster -- an empty last name is not
    a weaker signal, it is NO signal, and letting empty-string surnames match
    each other would silently merge every unrelated no-surname row in the
    corpus into one giant fake person.
    """

    def __init__(self) -> None:
        self._parent: list[int] = []
        self._rank: list[int] = []
        # blocking key -> the first node seen with that key. Later nodes with
        # an equal key union with this anchor; the anchor itself never moves,
        # so lookups stay O(1) instead of chasing a chain of prior anchors.
        self._blocks: dict[tuple[str, ...], int] = {}

    def add(self, *, first: str, last: str, address: str, zip: str,
            dob: str, phone: str) -> int:
        node = len(self._parent)
        self._parent.append(node)
        self._rank.append(0)

        last_n = (last or "").strip().upper()
        if not last_n:
            return node  # no surname -- never blocks, never joins a cluster

        first_n = (first or "").strip().upper()
        dob_n = (dob or "").strip()
        phone_n = _NON_DIGIT.sub("", phone or "")
        address_n = (address or "").strip().upper()
        zip_n = (zip or "").strip()

        keys: list[tuple[str, ...]] = []
        if dob_n:
            keys.append((_DOB, first_n, last_n, dob_n))
        if phone_n:
            keys.append((_PHONE, first_n, last_n, phone_n))
        if address_n and zip_n:
            keys.append((_ADDRESS, first_n, last_n, address_n, zip_n))

        for key in keys:
            anchor = self._blocks.get(key)
            if anchor is None:
                self._blocks[key] = node
            else:
                self._union(node, anchor)

        return node

    def person_of(self, node: int) -> int:
        return self._find(node)

    def person_ids(self) -> Mapping[int, int]:
        """node -> dense person id, assigned in first-seen (node) order.

        Dense and stable rather than exposing raw union-find roots: roots are
        an implementation detail of union order and path compression, not a
        contract callers should depend on run to run.
        """
        dense: dict[int, int] = {}
        result: dict[int, int] = {}
        for node in range(len(self._parent)):
            root = self._find(node)
            person_id = dense.setdefault(root, len(dense))
            result[node] = person_id
        return result

    def _find(self, x: int) -> int:
        root = x
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[x] != root:
            self._parent[x], x = root, self._parent[x]
        return root

    def _union(self, a: int, b: int) -> None:
        ra, rb = self._find(a), self._find(b)
        if ra == rb:
            return
        if self._rank[ra] < self._rank[rb]:
            ra, rb = rb, ra
        self._parent[rb] = ra
        if self._rank[ra] == self._rank[rb]:
            self._rank[ra] += 1
