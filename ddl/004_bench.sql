-- THE ORACLE. Deliberately OUTSIDE the partner surface: nothing in
-- src/occupancy_graph reads this schema, and the clone would still be faithful
-- if it were dropped.
--
-- It records the TRUE person clusters the loader computed, so correctness tests
-- can assert exactly which records a hal_id must return -- something production
-- can never tell us, because we do not know its ground truth. Keeping it in its
-- own schema is what stops ground truth from leaking into the surface under
-- test.
CREATE SCHEMA IF NOT EXISTS bench;

CREATE TABLE bench.true_person (
  person_id bigint PRIMARY KEY,
  synthetic_ssn char(9) NOT NULL,
  first_name text, last_name text,
  address text, zip text,
  record_count integer NOT NULL
);

CREATE TABLE bench.true_person_record (
  person_id bigint NOT NULL,
  source_table text NOT NULL,
  record_id bigint NOT NULL,
  shape text NOT NULL,
  PRIMARY KEY (source_table, record_id)
);

CREATE INDEX ON bench.true_person_record (person_id);
