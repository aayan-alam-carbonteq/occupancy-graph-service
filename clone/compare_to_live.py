"""Diff the clone's /v1/resolve source_counts against the recorded live run.

    .venv/bin/python -m clone.compare_to_live

RUN AS A MODULE, NEVER AS A SCRIPT PATH -- `clone` is not an installed
package, so `python clone/compare_to_live.py` puts only `clone/` on
sys.path, not the repo root, and the `from clone...`-shaped imports below (as
well as this file's own package membership) would not resolve. See
clone/README.md and clone/load.py's identical warning.

THIS SCRIPT DOES NOT ASSERT. It is a measurement instrument, not a test: the
clone and the live partner corpus are known, by construction, to disagree
(see clone/README.md's "What this CANNOT test"). The bar for this script is
that every difference it prints is EXPLAINABLE, not that it is zero.

WHY A DIFFERENCE IS EXPECTED, STATED UP FRONT. Two INDEPENDENT mechanisms,
not one:

1. `records_legacy` is reached through the resident hop
   (source/resolve.py::_scan_legacy_via_residents), not the full zip+address
   scan -- the scan itself does not finish against the live corpus (observed
   ACTIVE at 14+ minutes), which is the entire reason the hop exists. The
   hop's coverage depends on anchor rows: house_number is populated on
   exactly 4 feeds in production (USCRM, SSNxDOB, the 2014 phonebook, and
   Historic Data) but this clone's loader only reproduces 1 of them (USCRM,
   loaded as the `base` feed's records_legacy plan -- see
   clone/loader/feedplan.py). Fewer anchor feeds means fewer residents get
   named, which means fewer of their utility/trace/base rows get hopped to.
   This is the dominant effect on `utility` and `trace`, which live on
   records_legacy alone, and the measured (not guessed) size of it is in
   clone/coverage_experiment.py.

2. EVEN `records_new`-backed shapes (loan, drive, auto, and tax indirectly)
   diverge, and NOT because of the hop -- records_new is reached through the
   ordinary indexed zip+address scan on both clone and live, no hop
   involved. Checked directly against the clone DB for one disagreement
   (1552 SAMARA GLEN WAY, loan 4/10 live): the clone's
   `Payday_Big_2026/payday_ky.csv` simply has 4 rows at that exact address,
   not 10 -- a difference in the SOURCE CSV's row density per address, not
   in how the service reads it. clone/README.md is explicit that this
   loader targets structural and statistical fidelity (identical DDL,
   feed_population.json's calibrated column-population rates), never a
   row-for-row copy of the live corpus at a given address, so this is
   expected too. `tax` additionally inherits shape-1's effects through the
   majority-vote city/state (source/resolve.py::scan_zip_sources), which can
   pick a different city when phase 1 found different rows -- observed here
   as the only shapes where clone > live, not just clone < live.
"""
from __future__ import annotations

import asyncio
import os

import httpx

from occupancy_graph.service.app import create_app
from occupancy_graph.source.bundle import BundleCache
from occupancy_graph.source.pool import PartnerPool

DEFAULT_CLONE_DSN = "postgresql://clone:clone@127.0.0.1:55433/partner_clone"

# The seven shapes /v1/resolve reports in source_counts, in report order.
SHAPES = ("utility", "trace", "base", "loan", "drive", "auto", "tax")

# Recorded 2026-08-03 against the real partner corpus (PARTNER_DSN pointed at
# production), via the same POST /v1/resolve path this script drives against
# the clone. This is a frozen measurement, not something re-derived here --
# the whole point of this script is to compare against it.
LIVE = {
    "1104 SPRING RUN RD":   ("40514", {"utility": 1,  "trace": 3,  "base": 1, "loan": 0,  "drive": 0,  "auto": 2, "tax": 1}),
    "1552 SAMARA GLEN WAY": ("40515", {"utility": 3,  "trace": 20, "base": 3, "loan": 10, "drive": 3,  "auto": 0, "tax": 1}),
    "548 RHODORA RDG":      ("40517", {"utility": 6,  "trace": 4,  "base": 9, "loan": 7,  "drive": 7,  "auto": 0, "tax": 1}),
    "2812 RED LEAF DR":     ("40509", {"utility": 0,  "trace": 8,  "base": 4, "loan": 0,  "drive": 0,  "auto": 2, "tax": 2}),
    "849 W MAXWELL ST":     ("40508", {"utility": 6,  "trace": 18, "base": 3, "loan": 3,  "drive": 3,  "auto": 3, "tax": 6}),
    "535 LONE OAK DR":      ("40503", {"utility": 4,  "trace": 2,  "base": 2, "loan": 0,  "drive": 0,  "auto": 0, "tax": 1}),
    "1000 TURNBERRY LN":    ("40515", {"utility": 0,  "trace": 10, "base": 2, "loan": 12, "drive": 12, "auto": 0, "tax": 1}),
    "1004 SPRING RUN RD":   ("40514", {"utility": 16, "trace": 22, "base": 2, "loan": 0,  "drive": 0,  "auto": 3, "tax": 1}),
    "1057 SPRING RUN RD":   ("40514", {"utility": 8,  "trace": 7,  "base": 2, "loan": 0,  "drive": 0,  "auto": 1, "tax": 1}),
    "1101 WELDON CT":       ("40515", {"utility": 2,  "trace": 11, "base": 3, "loan": 1,  "drive": 1,  "auto": 0, "tax": 1}),
    "115 WABASH DR":        ("40503", {"utility": 5,  "trace": 13, "base": 2, "loan": 22, "drive": 4,  "auto": 0, "tax": 1}),
    "1332 OX HILL DR":      ("40517", {"utility": 9,  "trace": 0,  "base": 1, "loan": 0,  "drive": 0,  "auto": 0, "tax": 1}),
}


def _fmt_cell(clone: int, live: int) -> str:
    """`clone/live`, flagged with `*` when they disagree -- a difference is
    expected, but each one must still be visible to read, not buried."""
    marker = "*" if clone != live else " "
    return f"{clone:>3}/{live:<3}{marker}"


async def _resolve_one(client: httpx.AsyncClient, address: str, zip_code: str) -> dict[str, int]:
    response = await client.post("/v1/resolve", json={"address": address, "zip": zip_code})
    response.raise_for_status()
    return response.json()["source_counts"]


async def main() -> None:
    # The service learns its database EXCLUSIVELY from PARTNER_DSN (see
    # source/pool.py::PartnerPool.from_env and clone/README.md's "Point the
    # graph service at it") -- no service code path branches on "am I talking
    # to the clone". Setting it here, rather than requiring the caller to
    # export it, is what lets this script be run with nothing but the clone
    # container up.
    clone_dsn = os.environ.get("CLONE_DSN", DEFAULT_CLONE_DSN)
    os.environ["PARTNER_DSN"] = clone_dsn

    pool = await PartnerPool.create(clone_dsn)
    try:
        app = create_app(pool=pool, cache=BundleCache(pool))
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://clone") as client:
            header = f"{'address':<22} {'zip':<5} " + " ".join(f"{s:^8}" for s in SHAPES)
            print(header)
            print("-" * len(header))

            clone_totals = {shape: 0 for shape in SHAPES}
            live_totals = {shape: 0 for shape in SHAPES}
            for address, (zip_code, live_counts) in LIVE.items():
                clone_counts = await _resolve_one(client, address, zip_code)
                cells = " ".join(
                    _fmt_cell(clone_counts.get(shape, 0), live_counts[shape])
                    for shape in SHAPES
                )
                print(f"{address:<22} {zip_code:<5} {cells}")
                for shape in SHAPES:
                    clone_totals[shape] += clone_counts.get(shape, 0)
                    live_totals[shape] += live_counts[shape]

            print("-" * len(header))
            total_cells = " ".join(
                _fmt_cell(clone_totals[shape], live_totals[shape]) for shape in SHAPES
            )
            print(f"{'TOTAL':<22} {'':<5} {total_cells}")
    finally:
        await pool.close()

    print(
        "\n`clone/live`, `*` marks a disagreement. Differences are EXPECTED, not "
        "bugs. Primary cause: this clone reproduces only 1 of production's 4 "
        "house_number-bearing anchor feeds (USCRM), so the resident hop that "
        "records_legacy-backed shapes (utility, trace, base) depend on names "
        "fewer residents here than it would live -- measured (not guessed) in "
        "clone/coverage_experiment.py. Secondary cause, independent of the hop: "
        "the clone's Lexington CSVs are a statistically-calibrated sample, not a "
        "row-for-row copy of the live corpus, so even records_new-backed shapes "
        "(loan, drive, auto) that bypass the hop entirely can disagree, and "
        "`tax` additionally moves with phase-1's majority-vote city/state. See "
        "this module's docstring for the evidence behind both."
    )


if __name__ == "__main__":
    asyncio.run(main())
