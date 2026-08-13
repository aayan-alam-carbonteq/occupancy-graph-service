-- The three address indexes the partner built at our ask, dumped from
-- pg_indexes on the live corpus 2026-08-11 and verified `indisvalid`.
--
-- WHY THIS IS 005 AND NOT PART OF 002_indexes.sql, which is where every other
-- index lives. These are EXPRESSION indexes on `silver.s5_street_norm`, and the
-- ddl/ loader applies files in sorted order (tests/conftest.py: "every ddl/ file
-- must exceed every file it depends on"). The function is created in
-- 003_silver.sql, which sorts AFTER 002 -- so an address index declared in 002
-- fails with "function silver.s5_street_norm(text) does not exist". Splitting
-- them out states that dependency in the filename instead of leaving it as a
-- trap for whoever next edits 002.
--
-- WHY THE CLONE NEEDS THEM AT ALL. src/occupancy_graph/source/resolve.py emits
-- `silver.s5_street_norm(address) LIKE silver.s5_street_norm($n) || '%'` for
-- every address predicate. Without these indexes the fixture still ANSWERS
-- those queries -- correctly, and fast, because it fits in RAM -- so the suite
-- would stay green while the plan diverged completely from production's. The
-- clone exists to observe access paths; an index it silently lacks is exactly
-- the failure it is supposed to catch.
--
-- `text_pattern_ops` is what makes the prefix LIKE an index condition rather
-- than a filter: the default collation does not sort in a way that lets the
-- planner rewrite `LIKE 'PREFIX%'` into a range scan. Reproduce it exactly.

-- ---- records_legacy ---------------------------------------------------------
CREATE INDEX idx_records_legacy_zip_normaddr
  ON public.records_legacy
  USING btree (zip, silver.s5_street_norm(address) text_pattern_ops)
  WHERE zip IS NOT NULL;

-- ---- records_new: declared on the PARENT so every partition inherits ---------
-- Production declares this per-partition (all five, plus the default). The
-- parent declaration produces the same per-partition indexes and cannot miss
-- one; see 002_indexes.sql for the full reasoning on parent-vs-partition DDL.
CREATE INDEX idx_records_new_zip_normaddr
  ON public.records_new
  USING btree (zip, silver.s5_street_norm(address) text_pattern_ops)
  WHERE zip IS NOT NULL;

-- ---- the assessor partial index ---------------------------------------------
-- PARTIAL, and the predicate is load-bearing: `source_file LIKE
-- 'property_owner%'` is the identical literal that feeds.py's tax feed_clause
-- emits, which is how the planner proves the index applies to our query. Change
-- either side and the tax scan silently reverts to the 19 s - 241 s path.
--
-- Production carries this on records_partitioned_p20260301 alone -- the one
-- partition holding the property_owner feed -- so it is declared here on that
-- partition directly rather than on the parent, matching production's shape.
-- state leads city: ~8,000 distinct cities repeat across states, so city alone
-- prunes poorly (the partner's own correction to our proposed ordering).
CREATE INDEX idx_p20260301_property_owner_addr
  ON public.records_partitioned_p20260301
  USING btree (upper(state), upper(city), silver.s5_street_norm(address) text_pattern_ops)
  WHERE source_file LIKE 'property_owner%';
