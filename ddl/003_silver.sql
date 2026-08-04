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
