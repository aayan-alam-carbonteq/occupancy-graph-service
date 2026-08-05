"""Row-level parity: do the clone's rows MATCH production's, field for field?

    PARTNER_DSN=... CLONE_DSN=... .venv/bin/python -m clone.parity_check

THE MEASUREMENT THAT SETTLES WHAT THE CLONE IS. The Lexington CSVs turned out to
be a SUBSET OF THE PARTNER CORPUS, not an independent extraction -- so the right
question is not "do the aggregates look similar" but "is this row the same row".

Measured 2026-08-05 (2 addresses, 4 surnames, utility + trace):

    clone rows that exist IDENTICALLY in production   83.3%  (15/18)
    production rows the clone reproduces              46.9%  (15/32)

Read them as two different facts. The first says what we hold is genuinely
production's data. The second says we hold less than half of what production
has at these addresses -- the missing anchor feeds (2014 US Phonebook, Historic
Data, SSNxDOB) plus the subset itself.

The residual ~17% of clone rows that do not match exactly are most likely
date-coercion differences: utility.csv mixes YYYYMMDD and MMDDYYYY and carries
outright garbage, and clone/load.py drops what it cannot parse rather than
guessing. Worth chasing if row parity needs to go higher.

Production is queried ONLY over the indexed (last_name, zip) path plus an
address prefix -- never a scan. Names are read for comparison; no bulk PII is
printed or stored.
"""

import asyncio, os, collections, asyncpg

ADDR = [("1104 SPRING","40514"),("1057 SPRING","40514")]
FEEDS = ("Export Utility Stripped Down", "Trace Skipping Oct 2025")

def key(r):
    return (str(r["feed"]), (r["first_name"] or "").strip().upper(),
            str(r["dob"] or ""), (r["phone"] or "").strip())

async def main():
    cl = await asyncpg.connect(os.environ["CLONE_DSN"])
    pr = await asyncpg.connect(os.environ["PARTNER_DSN"], server_settings={
        "default_transaction_read_only": "on", "statement_timeout": "400000"})
    try:
        tot_c = tot_p = tot_match = 0
        print(f"{'address':<16}{'surname':<14}{'clone':>7}{'prod':>7}{'matched':>9}")
        print("-"*54)
        for prefix, zc in ADDR:
            names = [r["last_name"] for r in await cl.fetch(
                """SELECT last_name FROM public.records_legacy
                   WHERE zip=$1 AND address ILIKE $2 AND last_name IS NOT NULL
                     AND split_part(source_file,'/',1) = ANY($3::text[])
                   GROUP BY last_name ORDER BY count(*) DESC LIMIT 2""",
                zc, prefix + "%", list(FEEDS))]
            for nm in names:
                c_rows = await cl.fetch(
                    """SELECT split_part(source_file,'/',1) feed, first_name, dob, phone
                       FROM public.records_legacy
                       WHERE last_name=$1 AND zip=$2 AND address ILIKE $3
                         AND split_part(source_file,'/',1) = ANY($4::text[])""",
                    nm, zc, prefix + "%", list(FEEDS))
                # NO source_file predicate here: split_part() is unindexable and
                # forces a heap filter production cannot afford. Fetch on the pure
                # (last_name, zip) index + address prefix, filter feeds in Python.
                p_all = await pr.fetch(
                    """SELECT source_file, first_name, dob, phone
                       FROM public.records_legacy
                       WHERE last_name=$1 AND zip=$2 AND address ILIKE $3 LIMIT 400""",
                    nm, zc, prefix + "%")
                p_rows = [{"feed": r["source_file"].split("/")[0], "first_name": r["first_name"],
                           "dob": r["dob"], "phone": r["phone"]}
                          for r in p_all if r["source_file"].split("/")[0] in FEEDS]
                cc = collections.Counter(key(r) for r in c_rows)
                pc = collections.Counter(key(r) for r in p_rows)
                matched = sum((cc & pc).values())
                tot_c += len(c_rows); tot_p += len(p_rows); tot_match += matched
                print(f"{prefix:<16}{nm[:13]:<14}{len(c_rows):>7}{len(p_rows):>7}{matched:>9}")
        print("-"*54)
        print(f"{'TOTAL':<30}{tot_c:>7}{tot_p:>7}{tot_match:>9}")
        if tot_c: print(f"\nclone rows that exist IDENTICALLY in production: {100*tot_match/tot_c:.1f}%")
        if tot_p: print(f"production rows the clone reproduces:             {100*tot_match/tot_p:.1f}%")
    finally:
        await cl.close(); await pr.close()

asyncio.run(main())
