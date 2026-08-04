-- Production's index set, dumped from pg_indexes on the live corpus 2026-08-04.
-- Reproduced in FULL because the clone exists to observe ACCESS PATHS: latency
-- does not transfer from a local clone (everything fits in RAM), but plan shape
-- does, and a missing index silently changes the plan the experiments are meant
-- to observe.
--
-- Loaded after 001_records.sql -- see the numeric-prefix contract in
-- tests/conftest.py. pg_trgm is required by the last_name GIN indexes.
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ---- records_legacy (14) ----------------------------------------------------
CREATE UNIQUE INDEX records_pkey ON public.records_legacy USING btree (record_id);
CREATE INDEX idx_records_zip ON public.records_legacy USING btree (zip) WHERE zip IS NOT NULL;
CREATE INDEX idx_records_lastname_zip_house ON public.records_legacy USING btree (last_name, zip, house_number);
CREATE INDEX idx_records_legacy_zip_house ON public.records_legacy USING btree (zip, house_number);
CREATE INDEX idx_records_legacy_state_city ON public.records_legacy USING btree (upper(state), upper(city));
CREATE INDEX idx_records_first_last ON public.records_legacy USING btree (first_name, last_name)
  WHERE first_name IS NOT NULL AND last_name IS NOT NULL;
CREATE INDEX idx_records_last_name_trgm ON public.records_legacy USING gin (last_name gin_trgm_ops)
  WHERE last_name IS NOT NULL;
CREATE INDEX idx_records_dob ON public.records_legacy USING btree (dob) WHERE dob IS NOT NULL;
CREATE INDEX idx_records_email ON public.records_legacy USING btree (lower(email)) WHERE email IS NOT NULL;
CREATE INDEX idx_records_email2 ON public.records_legacy USING btree (lower(email2)) WHERE email2 IS NOT NULL;
CREATE INDEX idx_records_mobile ON public.records_legacy USING btree (mobile) WHERE mobile IS NOT NULL;
CREATE INDEX idx_records_phone ON public.records_legacy USING btree (phone) WHERE phone IS NOT NULL;
CREATE INDEX idx_records_ssn ON public.records_legacy USING btree (ssn) WHERE ssn IS NOT NULL;
CREATE INDEX idx_records_ssn2 ON public.records_legacy USING btree (ssn2) WHERE ssn2 IS NOT NULL;

-- ---- records_new: declared on the PARENT so every partition inherits --------
-- Production declares these per-partition (18 each). Declaring on the parent
-- produces the same per-partition indexes and cannot skip a partition by
-- accident, which hand-maintained per-partition DDL can.
CREATE INDEX ON public.records_new USING btree (record_id);
CREATE INDEX ON public.records_new USING btree (zip);
CREATE INDEX ON public.records_new USING btree (address_id);
CREATE INDEX ON public.records_new USING btree (city, state);
CREATE INDEX ON public.records_new USING btree (upper(state), upper(city));
CREATE INDEX ON public.records_new USING btree (last_name, zip, house_number);
CREATE INDEX ON public.records_new USING btree (first_name, last_name);
CREATE INDEX ON public.records_new USING gin (last_name gin_trgm_ops);
CREATE INDEX ON public.records_new USING gin (tsv_name);
CREATE INDEX ON public.records_new USING btree (dob) WHERE dob IS NOT NULL;
CREATE INDEX ON public.records_new USING btree (phone);
CREATE INDEX ON public.records_new USING btree (mobile);
CREATE INDEX ON public.records_new USING btree (ssn);
CREATE INDEX ON public.records_new USING btree (ssn2);
CREATE INDEX ON public.records_new USING btree (email);
CREATE INDEX ON public.records_new USING btree (lower(email));
CREATE INDEX ON public.records_new USING btree (email2);
CREATE INDEX ON public.records_new USING btree (lower(email2));
