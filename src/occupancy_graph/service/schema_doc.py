"""What the agent is told about the corpus.

Curated, not introspected. The partner corpus is ONE 144-column table shape,
carried by two relations, with feed identity in the unindexed `source_file`
string. Dumping 144 columns without saying which paths are fast would guarantee
refused queries; naming the paths and the defects is the entire value of this
endpoint.

Every measured number comes from the workspace specs:
docs/superpowers/specs/2026-07-28-engine-partner-db-interface-coverage.md §2-§4,
2026-07-27-partner-records-db-findings.md §3-§7, and
2026-07-27-partner-db-surface-and-use-cases.md §4.

THIS MODULE ALSO OWNS THE REFUSAL HINT. It used to live in sql_hatch.py as its
own hand-written string, and the two lists had ALREADY diverged: the hint named
ssn, phone and email, which the access-path list did not document, and the
access-path list named the entity_links paths, which the hint did not mention.
An agent that reads one and then gets handed the other is being told two
different things about the same database. `HINT` is therefore GENERATED from
ACCESS_PATHS below and re-exported by sql_hatch, and tests/test_schema_doc.py
pins both that the generated string is still Contract C's verbatim text and
that neither list can grow an entry the other lacks.

WHY THE SILVER PATHS CARRY NO hint_key. The pinned hint answers exactly one
question -- "no index supports this predicate on the records tables" -- so it
names records indexes only. Adding entity_links to it would edit a pinned
contract string; leaving the silver paths undocumented in /v1/schema would hide
the traversal the typed surface itself runs. The asymmetry is deliberate, and
the test enforces it in the direction that matters: no RECORDS path may be
absent from the hint.
"""
from __future__ import annotations

from typing import Any

from occupancy_graph.service import limits

TABLES = [
    {
        "name": "public.records_legacy",
        "purpose": "Older feeds: trace (~44%), utility (~26%), SSNxDOB, consumer base. 6.24 B rows.",
        "key_columns": [
            "record_id", "source_file", "first_name", "middle_name", "last_name",
            "dob", "address", "city", "state", "zip", "county", "phone", "mobile",
            "email", "own_rent", "employer", "occupation", "raw_data",
        ],
    },
    {
        "name": "public.records_partitioned",
        "purpose": (
            "Current feeds, partitioned by imported_at: payday loans (loan/drive), "
            "auto, property_owner (tax). 1.4 B rows. Exposed as the view records_new."
        ),
        "key_columns": [
            "record_id", "source_file", "imported_at", "first_name", "last_name",
            "address", "city", "state", "zip", "own_rent", "employer", "occupation",
            "dl_number", "dl_state", "raw_data",
        ],
    },
    {
        "name": "silver.entity_master",
        "purpose": "Partner entity resolution. One row per hal_id. Unreliable -- see caveats.",
        "key_columns": [
            "hal_id", "canonical_first_name", "canonical_last_name",
            "canonical_address_line1", "canonical_city", "canonical_state",
            "canonical_zip", "record_count", "identity_confidence", "is_suspicious",
            "is_merged", "merged_into_hal_id",
        ],
    },
    {
        "name": "silver.entity_links",
        "purpose": "hal_id -> (source_table, record_id). Indexed in both directions.",
        "key_columns": ["hal_id", "source_table", "record_id", "match_type", "confidence"],
    },
]

# ORDER IS LOAD-BEARING: HINT is joined from the hint_keys in this order, and
# Contract C pins the order they appear in. Reordering these entries rewrites
# the refusal hint.
#
# `hint_key` is the token this path appears as in a refusal, and it is on the
# wire on purpose -- it is what lets the agent map "Indexed paths: ... ssn ..."
# back to the row of this table that explains what ssn actually costs.
ACCESS_PATHS = [
    {
        "predicate": "zip = $1 AND address ILIKE 'N STREET%'",
        "table": "public.records_partitioned",
        "index": "per-partition zip btree",
        "measured": "173 ms warm, 24 k rows examined",
        "hint_key": "zip",
    },
    {
        "predicate": "zip = $1 AND address ILIKE 'N STREET%'",
        "table": "public.records_legacy",
        "index": "idx_records_zip",
        "measured": "1.30 s warm / 32 s cold, 272 916 rows examined",
        "hint_key": "zip",
    },
    {
        "predicate": "last_name = $1 AND zip = $2",
        "table": "public.records_legacy",
        "index": "idx_records_lastname_zip_house",
        "measured": "1.0 ms warm / 222 ms cold, 50 rows examined",
        "hint_key": "(last_name, zip, house_number)",
    },
    {
        "predicate": "upper(state) = $1 AND upper(city) = $2 AND address ILIKE 'N STREET%'",
        "table": "public.records_partitioned",
        "index": "(upper(state), upper(city))",
        "measured": "613 ms warm / 53 s cold, 151 507 rows examined. The ONLY path to tax.",
        "hint_key": "(upper(state), upper(city))",
    },
    {
        "predicate": "ssn = $1",
        "table": "public.records_legacy and public.records_partitioned",
        "index": "ssn (also ssn2) on both corpora",
        "measured": (
            "Not separately timed. Indexed on records_legacy and on every "
            "records_partitioned partition; ssn is 95.7% populated on the payday "
            "partition and 0% on property_owner rows."
        ),
        "hint_key": "ssn",
    },
    {
        "predicate": "phone = $1",
        "table": "public.records_legacy and public.records_partitioned",
        "index": "phone (also mobile) on both corpora",
        "measured": (
            "Not separately timed. Population is the limit, not the index: phone "
            "is ~75% on trace, ~46% on utility, ~29% on auto."
        ),
        "hint_key": "phone",
    },
    {
        "predicate": "lower(email) = lower($1)",
        "table": "public.records_legacy and public.records_partitioned",
        "index": "lower(email) on records_legacy; email on each partition",
        "measured": (
            "Not separately timed; email is 95.9% populated on the payday "
            "partition. Write it as lower(email) = lower($1): records_legacy "
            "indexes the FUNCTION, so a bare email = $1 misses that index."
        ),
        "hint_key": "email",
    },
    {
        "predicate": "hal_id = $1",
        "table": "silver.entity_links",
        "index": "entity_links(hal_id)",
        "measured": "215 ms",
        "hint_key": None,
    },
    {
        "predicate": "record_id = $1 AND source_table = $2",
        "table": "silver.entity_links",
        "index": "entity_links(source_table, record_id), UNIQUE",
        "measured": "81 ms",
        "hint_key": None,
    },
]

CAVEATS = [
    # The three pinned by Contract C, verbatim and first.
    "house_number and zip are 0% populated on property_owner rows",
    "~17.5% of property_owner rows are column-shifted",
    "imported_at is a load date, not an observation date",
    # The rest, all measured.
    "There is NO index on the free-text address, on latitude/longitude, on "
    "source_file, or on raw_data. Predicating on any of them scans.",
    "source_file is the only feed identity and it is unindexed -- always pair it "
    "with an indexed predicate, never use it as the driving condition.",
    "records_partitioned's partitions are separate DATASETS, not time slices: "
    "p20251201 e-commerce, p20260101 consumer marketing, p20260201 payday loans "
    "(931 M), p20260301 property_owner tax plus auto (233 M). Constraining "
    "imported_at picks a feed, not a date range.",
    "silver.entity_master is 17.9% is_suspicious, identity_confidence is modal at "
    "40.50 (27.5% of rows, mass in the 34-70 band, NOT a maximum), 45% of "
    "entities have record_count = 1, and is_merged is false everywhere: the "
    "partner computes its merges and never applies them. One real person is "
    "routinely several hal_ids. Discount it accordingly.",
    "property_owner rows have ssn, dob and house_number 0% populated, so they carry "
    "no blocking key and are ABSENT from silver.entity_links entirely.",
    "record_id is not indexed on the records tables; looking rows up by it scans. "
    "Reach rows through silver.entity_links, and never make record_id the driving "
    "predicate.",
    "own_rent arrives as RENT/OWN/rent/own/Rent/Own/r/o and the meaningless '1' "
    "(11.6%). Normalize before comparing.",
    "trace raw_data degrades past Record_Date (valid 82.6%): Date_Of_Birth_Year "
    "31.9%, Home_Owner_Renter_Code 14.1%, Number_of_Bedrooms 1.1%. Trust the "
    "mapped top-level columns.",
    "utility rows carry no raw_data at all (0%) and no date field of any kind.",
    "Never COUNT(*) a records table and never query one without both a LIMIT and "
    "an indexed predicate: the two relations hold 6.24 B and 1.4 B rows over 3.7 TB.",
    "There is NO voter, criminal or linkedin data anywhere in this corpus. The "
    "voter/DMV/tax names in records_demo are 29 hand-seeded synthetic rows -- the "
    "partner's roadmap, not their inventory.",
]


def _build_hint() -> str:
    """The refusal hint, joined from the paths above in order.

    dict.fromkeys de-duplicates while preserving order: `zip` documents two
    relations with different costs and must appear once in the hint.
    """
    keys = dict.fromkeys(
        path["hint_key"] for path in ACCESS_PATHS if path["hint_key"]
    )
    return "No index supports this predicate. Indexed paths: " + "; ".join(keys) + "."


HINT = _build_hint()


def schema_document() -> dict[str, Any]:
    return {
        "tables": TABLES,
        "access_paths": ACCESS_PATHS,
        "caveats": CAVEATS,
        "limits": {
            "max_rows": limits.max_sql_rows(),
            "max_plan_cost": limits.max_plan_cost(),
            "max_records_seqscan_cost": limits.max_records_seqscan_cost(),
            "statement_timeout_ms": limits.sql_timeout_ms(),
        },
    }
