"""Regenerate clone/profiles/records_catalog.json from the live partner corpus.

records_catalog.json is the anchor for
tests/clone/test_ddl_matches_production.py::test_records_legacy_matches_the_production_catalog
-- it is what our `records_legacy` DDL is checked against, so it must come
from the real thing, not be hand-typed. Last captured 2026-08-04 (144
columns). Re-run this after any confirmed change to `public.records_legacy`'s
column set on the partner side.

Requires live credentials: set PARTNER_DSN (see .env.example) and run

    .venv/bin/python clone/profiles/capture_catalog.py

`default_transaction_read_only` is set the same way PartnerPool sets it
(server_settings, not a SET in a callback -- see source/pool.py's module
docstring for why that distinction matters) even though this script only
ever SELECTs from pg_attribute; a script that touches the partner corpus
should not rely on its own care to stay read-only.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import asyncpg

OUTPUT = Path(__file__).parent / "records_catalog.json"


async def main() -> None:
    dsn = os.environ.get("PARTNER_DSN")
    if not dsn:
        raise SystemExit("PARTNER_DSN is not set -- see .env.example")

    conn = await asyncpg.connect(
        dsn,
        server_settings={"default_transaction_read_only": "on", "statement_timeout": "60000"},
    )
    try:
        rows = await conn.fetch("""
            SELECT a.attname, format_type(a.atttypid, a.atttypmod) AS typ
            FROM pg_attribute a WHERE a.attrelid='public.records_legacy'::regclass
              AND a.attnum>0 AND NOT a.attisdropped ORDER BY a.attnum""")
    finally:
        await conn.close()

    columns = [[r["attname"], r["typ"]] for r in rows]
    OUTPUT.write_text(json.dumps(columns, indent=2) + "\n")
    print(f"wrote {len(columns)} columns to {OUTPUT}")


if __name__ == "__main__":
    asyncio.run(main())
