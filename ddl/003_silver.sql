-- The partner's entity-resolution graph, dumped from the live corpus 2026-08-04.
--
-- Reproduced at production's real widths and types, not a simplification: the
-- service discounts rows by identity_confidence and is_suspicious, so a schema
-- that cannot carry those values cannot exercise the discounting logic at all.
CREATE SCHEMA IF NOT EXISTS silver;

CREATE TABLE silver.entity_master (
  hal_id char(15) PRIMARY KEY,
  canonical_first_name varchar(100), canonical_last_name varchar(100),
  canonical_ssn char(9), canonical_email varchar(255), canonical_phone char(10),
  canonical_address_line1 varchar(255), canonical_city varchar(100),
  canonical_state char(2), canonical_zip varchar(10), canonical_dob date,
  record_count integer,
  first_seen_at timestamptz, last_seen_at timestamptz,
  created_at timestamptz, updated_at timestamptz,
  is_merged boolean, merged_into_hal_id char(15),
  canonical_source_table varchar(50), canonical_record_id bigint,
  canonical_selection_score numeric(5,2), canonical_selection_evidence jsonb,
  anomaly_flags jsonb, is_suspicious boolean,
  identity_confidence numeric(5,2), corroboration_evidence jsonb
);

CREATE TABLE silver.entity_links (
  id bigserial,
  hal_id char(15) NOT NULL,
  source_table varchar(50) NOT NULL,
  record_id bigint NOT NULL,
  match_type varchar(50),
  confidence numeric(3,2),
  created_at timestamptz DEFAULT now()
);

-- Indexed BOTH ways in production and both directions are used: by hal_id
-- (measured 215 ms live) for person -> records, and UNIQUE on
-- (source_table, record_id) (81 ms) for records -> person. source/search.py
-- depends on both.
CREATE INDEX ON silver.entity_links USING btree (hal_id);
CREATE UNIQUE INDEX ON silver.entity_links USING btree (source_table, record_id);
CREATE INDEX ON silver.entity_master USING btree (upper(canonical_last_name));

-- ---- silver.unique_keys ------------------------------------------------------
-- The partner's blocking keys, PARTITIONED BY key_type exactly as production
-- is (verified 2026-08-11: unique_keys_name_dob 996 M, _phone 512 M, _ssn 325 M,
-- _email 261 M, _name_house_zip 258 M). Only the partitions this service reads
-- are declared -- adding empty ones would model a topology without exercising it.
--
-- THE UNIQUE INDEX ON key_value IS THE POINT. search.py::search_people resolves
-- a name to hal_ids by RANGE-scanning it ('LAST|FIRST|' .. 'LAST|FIRST}'),
-- because entity_master has no name index and scanning it does not complete.
-- Without this index here the fixture answers the same query by seq-scanning a
-- handful of rows -- green tests over a plan production cannot run.
--
-- NOTE the opclass is deliberately DEFAULT, matching production. That is why
-- search_people uses explicit >= / < bounds rather than LIKE: a prefix LIKE
-- needs text_pattern_ops and, without it, seq-scans (measured live: 18 s vs
-- 70 ms for the equivalent range).
CREATE TABLE silver.unique_keys (
  key_id bigint,
  key_type varchar(32) NOT NULL,
  key_value varchar(255) NOT NULL,
  hal_id char(15),
  created_at timestamptz,
  ssn_token text
) PARTITION BY LIST (key_type);

CREATE TABLE silver.unique_keys_name_dob
  PARTITION OF silver.unique_keys FOR VALUES IN ('name_dob');
CREATE TABLE silver.unique_keys_name_house_zip
  PARTITION OF silver.unique_keys FOR VALUES IN ('name_house_zip');

CREATE UNIQUE INDEX idx_unique_keys_name_dob_value
  ON silver.unique_keys_name_dob USING btree (key_value) INCLUDE (key_id);
CREATE UNIQUE INDEX idx_unique_keys_nhz_value
  ON silver.unique_keys_name_house_zip USING btree (key_value) INCLUDE (key_id);

-- ---- silver.s5_street_norm --------------------------------------------------
-- Dumped VERBATIM from `pg_get_functiondef` on the live corpus 2026-08-11 (only
-- CREATE FUNCTION -> CREATE OR REPLACE FUNCTION is unchanged, and it already
-- was). This is not a helper we own: it is the partner's, and the three address
-- indexes are built ON it, so the clone must carry the identical definition or
-- local plans diverge from production's for reasons that have nothing to do
-- with the query under test.
--
-- IMMUTABLE is what makes it index-able at all; PARALLEL SAFE and the exact
-- replacement list are equally load-bearing. Do not "tidy" the nesting: any
-- change to the output changes the indexed key, and every stored key would have
-- to be rebuilt for the index to remain correct.
CREATE OR REPLACE FUNCTION silver.s5_street_norm(t text)
 RETURNS text
 LANGUAGE sql
 IMMUTABLE PARALLEL SAFE
AS $function$
  SELECT nullif(trim(
    regexp_replace(regexp_replace(regexp_replace(regexp_replace(regexp_replace(regexp_replace(
    regexp_replace(regexp_replace(regexp_replace(regexp_replace(regexp_replace(regexp_replace(
    regexp_replace(upper(trim(coalesce(t,''))),'\s+',' ','g'),
    '\ySTREET\y','ST','g'), '\yAVENUE\y','AVE','g'),  '\yROAD\y','RD','g'),
    '\yDRIVE\y','DR','g'),  '\yLANE\y','LN','g'),      '\yBOULEVARD\y','BLVD','g'),
    '\yCOURT\y','CT','g'),  '\yPLACE\y','PL','g'),     '\yTERRACE\y','TER','g'),
    '\yPARKWAY\y','PKWY','g'), '\yHIGHWAY\y','HWY','g'),'\yCIRCLE\y','CIR','g')
  ),'')
$function$;
