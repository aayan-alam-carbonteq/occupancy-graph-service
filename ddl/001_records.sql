-- Shared DDL: loaded by BOTH the throwaway test fixture (tests/conftest.py) and
-- the persistent clone added in Task 4. It lives here precisely so the clone
-- cannot be hand-duplicated and drift -- this fixture already modelled a
-- topology production does not have once, and 548 tests passed while it did.
-- Column list mirrors public.records_new exactly (144 columns).

DROP SCHEMA IF EXISTS silver CASCADE;
DROP VIEW IF EXISTS public.records_new CASCADE;      -- pre-2026-08-03 fixture shape
DROP TABLE IF EXISTS public.records_partitioned CASCADE;  -- ditto; never existed in prod
DROP TABLE IF EXISTS public.records_new CASCADE;
DROP TABLE IF EXISTS public.records_legacy CASCADE;

CREATE TABLE public.records_legacy (
  record_id bigint, source_file text, source_row integer, imported_at timestamptz,
  ssn char(9), ssn2 char(9), first_name text, middle_name text, last_name text,
  suffix text, title text, dob date, dod date, gender char(1), age integer,
  inferred_age integer, marital_status char(1), aka_name1 text, aka_name2 text,
  aka_name3 text, alt_dob1 date, alt_dob2 date, alt_dob3 date,
  spouse_first_name text, spouse_middle_name text, spouse_last_name text,
  spouse_dob date, address_id text, house_number text, pre_direction text,
  street_name text, street_suffix text, post_direction text, unit_designator text,
  unit_number text, address text, address2 text, city text, state text, zip text,
  zip4 text, county text, msa text, county_code text, carrier_route text,
  latitude double precision, longitude double precision, timezone text,
  census_tract text, census_block text, phone text, mobile text, email text,
  email2 text, dwelling_type text, length_of_residence integer,
  home_owner_probability text, own_rent text, home_year_built integer,
  home_swimming_pool boolean, air_conditioning boolean, home_heat text,
  home_purchase_price integer, home_purchase_date date, estimated_home_value text,
  mortgage_amount integer, mortgage_lender text, mortgage_rate double precision,
  mortgage_rate_type text, mortgage_loan_type text, refinance_date date,
  refinance_amount integer, refinance_lender text, refinance_rate_type text,
  refinance_loan_type text, mortgage_amount_2nd integer,
  census_median_home_value integer, census_median_income integer,
  loan_to_value integer, estimated_income text, credit_rating text, net_worth text,
  num_credit_lines integer, occupation_group text, occupation text, education text,
  ethnic_code text, ethnic_group text, language_code text, religion_code text,
  hispanic_country_code text, num_persons integer, num_adults integer,
  generations integer, presence_of_children text, num_children integer,
  single_parent boolean, senior_in_household boolean,
  young_adult_in_household boolean, veteran_in_household boolean,
  business_owner boolean, soho_indicator boolean, employer text, hire_date date,
  has_credit_card boolean, has_bank_card boolean, has_premium_card boolean,
  has_amex boolean, has_investments boolean, investments_stocks boolean,
  investments_real_estate boolean, mail_responder boolean, online_purchaser boolean,
  mail_order_buyer boolean, catalog_buyer boolean, charitable_donor boolean,
  political_donor boolean, religious_donor boolean, auto_enthusiast boolean,
  book_reader boolean, computer_owner boolean, cooking_enthusiast boolean,
  diy_enthusiast boolean, exercise_enthusiast boolean, gardener boolean,
  golf_enthusiast boolean, home_decorating boolean, outdoor_enthusiast boolean,
  photography boolean, traveler boolean, has_pets boolean, has_cats boolean,
  has_dogs boolean, ip_address text, url text, datasource text, dnc text,
  dl_number text, dl_state text, individual_id text, living_unit_id text,
  rd_id text, raw_data jsonb, tsv_name tsvector
);

-- PRODUCTION TOPOLOGY, verified against the live corpus 2026-08-03.
-- `records_new` IS the partitioned parent (relkind 'p', RANGE on imported_at).
-- Its partitions are the relations named `records_partitioned_*`. There is NO
-- relation called `public.records_partitioned` and NO view.
--
-- This fixture previously had the relationship INVERTED -- it built
-- records_partitioned as the parent and records_new as a view over it. That is
-- how source/feeds.py came to name records_partitioned for five of seven shapes
-- (every one of which would have raised `relation does not exist` against
-- production) while 548 tests passed. A fixture that models a topology the
-- production database does not have cannot fail on the difference.
CREATE TABLE public.records_new (LIKE public.records_legacy INCLUDING DEFAULTS)
  PARTITION BY RANGE (imported_at);

-- Production has FIVE partitions. p20251201 holds 2019.2_USA_Consumer_LF and
-- 24mm loan-txt; p20260101 holds 2019.2_USA_Consumer_LF and PD loan_master.
-- Their absence here is why the fixture could not represent the base feed's
-- records_new half.
CREATE TABLE public.records_partitioned_p20251201
  PARTITION OF public.records_new
  FOR VALUES FROM ('2025-12-01') TO ('2026-01-01');

CREATE TABLE public.records_partitioned_p20260101
  PARTITION OF public.records_new
  FOR VALUES FROM ('2026-01-01') TO ('2026-02-01');

CREATE TABLE public.records_partitioned_p20260201
  PARTITION OF public.records_new
  FOR VALUES FROM ('2026-02-01') TO ('2026-03-01');

CREATE TABLE public.records_partitioned_p20260301
  PARTITION OF public.records_new
  FOR VALUES FROM ('2026-03-01') TO ('2026-04-01');

CREATE TABLE public.records_partitioned_default
  PARTITION OF public.records_new DEFAULT;
