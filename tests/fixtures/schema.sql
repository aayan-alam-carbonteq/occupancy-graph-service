-- The records DDL now lives in ../../ddl/ and is shared with the persistent
-- clone (clone/docker-compose.clone.yml), so the two can never drift.
-- conftest.py loads ddl/*.sql before this file. Anything remaining here is
-- fixture-only.

-- The real index set. Tests must exercise the same access paths as production.
CREATE INDEX idx_records_zip ON public.records_legacy (zip) WHERE zip IS NOT NULL;
CREATE INDEX idx_records_lastname_zip_house ON public.records_legacy (last_name, zip, house_number);
CREATE INDEX idx_records_legacy_state_city ON public.records_legacy (upper(state), upper(city));
CREATE INDEX idx_records_first_last ON public.records_legacy (first_name, last_name);

CREATE INDEX ON public.records_new (zip);
CREATE INDEX ON public.records_new (last_name, zip, house_number);
CREATE INDEX ON public.records_new (upper(state), upper(city));
CREATE INDEX ON public.records_new (city, state);
CREATE INDEX ON public.records_new (first_name, last_name);

-- record_id IS indexed on every records relation in production (verified
-- 2026-08-03): records_pkey UNIQUE on records_legacy, a btree on each
-- records_new partition. Earlier specs claimed no index covered it; they were
-- wrong. The real cost on records_legacy is HEAP I/O, not the index -- see
-- source/search.py::rows_for_links.
CREATE UNIQUE INDEX records_pkey ON public.records_legacy (record_id);
CREATE INDEX ON public.records_new (record_id);

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
