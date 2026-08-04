-- The records DDL now lives in ../../ddl/ and is shared with the persistent
-- clone (clone/docker-compose.clone.yml), so the two can never drift.
-- conftest.py loads ddl/*.sql before this file. Anything remaining here is
-- fixture-only.

-- The full production index set now lives in ddl/002_indexes.sql (loaded
-- before this file -- see the numeric-prefix contract in tests/conftest.py).

CREATE SCHEMA silver;

CREATE TABLE silver.entity_master (
  hal_id text PRIMARY KEY,
  canonical_first_name text, canonical_last_name text,
  canonical_address_line1 text, canonical_city text, canonical_state text,
  canonical_zip text, canonical_dob date, canonical_phone text,
  canonical_email text, canonical_ssn text,
  record_count integer, identity_confidence numeric,
  is_suspicious boolean, anomaly_flags jsonb,
  is_merged boolean, merged_into_hal_id text
);

CREATE TABLE silver.entity_links (
  hal_id text, source_table text, record_id bigint,
  match_type text, confidence numeric
);
CREATE INDEX ON silver.entity_links (record_id, source_table);
CREATE INDEX ON silver.entity_links (hal_id);
