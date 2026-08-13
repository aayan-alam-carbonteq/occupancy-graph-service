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
