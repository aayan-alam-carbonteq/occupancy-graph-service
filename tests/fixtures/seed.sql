-- Subject address: 123 MAIN ST, LEXINGTON, KY 40505

-- utility: no raw_data at all (matches the real feed, which is 0% raw_data)
INSERT INTO public.records_legacy
  (record_id, source_file, imported_at, first_name, middle_name, last_name, dob, dod,
   address, city, county, state, zip, phone)
VALUES
  (1001, 'Export Utility Stripped Down/Utility_ky/Utility_ky.csv', '2025-12-11',
   'Pat', '', 'Tenant', '1985-04-02', NULL,
   '123 MAIN ST', 'LEXINGTON', 'FAYETTE', 'KY', '40505', '5557778888');

-- trace: valid Record_Date, plus garbage in the tail fields (Number_of_Bedrooms is
-- valid on only ~1% of real rows, so the coercion path must be exercised)
INSERT INTO public.records_legacy
  (record_id, source_file, imported_at, first_name, middle_name, last_name,
   address, city, state, zip, county, phone, mobile, email, raw_data)
VALUES
  (1002, 'Trace Skipping Oct 2025/trace_ky.csv', '2025-12-10',
   'Jane', '', 'Doe', '123 MAIN ST', 'LEXINGTON', 'KY', '40505', 'FAYETTE',
   '5551112222', '', 'jane@example.com',
   '{"Record_Date": "20240115", "Date_Of_Birth_Day": "1", "Date_Of_Birth_Month": "1",
     "Date_Of_Birth_Year": "1980", "Credit_Capacity": "", "Home_Built_Year": "1990",
     "Home_Purchase_Date": "20180627", "Income_Description": "",
     "Number_of_Bedrooms": "GARBAGE", "Email_02": "", "Email_03": ""}'::jsonb),
  (1003, 'Trace Skipping Oct 2025/trace_ky.csv', '2025-12-10',
   'John', '', 'Smith', '123 MAIN ST', 'LEXINGTON', 'KY', '40505', 'FAYETTE',
   '5552223333', '', '',
   '{"Record_Date": "NOTADATE"}'::jsonb);

-- base: consumer/marketing. mortgage_amount is ALREADY IN THOUSANDS.
-- home_owner_probability is an H/R/9 code, not a probability.
INSERT INTO public.records_legacy
  (record_id, source_file, imported_at, first_name, middle_name, last_name,
   house_number, address, city, state, zip, phone, dob,
   estimated_income, home_owner_probability, length_of_residence,
   presence_of_children, gender, marital_status, education, business_owner,
   credit_rating, net_worth, home_purchase_price, home_purchase_date,
   home_year_built, estimated_home_value, mortgage_amount, mortgage_lender,
   refinance_date, refinance_amount, refinance_lender)
VALUES
  (1004, '2026.1-USCRM/uscrm_ky.csv', '2025-12-08',
   'Jane', 'A', 'Doe', '123', '123 MAIN ST', 'LEXINGTON', 'KY', '40505',
   '5551112222', '1980-01-01',
   'K', 'H', 6, 'Y', 'F', 'M', '4', false, 'A', 'J',
   209, '2018-06-27', 1990, 'L', 171, 'USAA FED SAV BK',
   '2021-03-01', 132, 'QUICKEN');

-- ANCHOR rows: the feeds that populate house_number in production (SSNxDOB,
-- phonebook, historic) and that the resident-hop scan anchors on. Deliberately
-- NOT shape feeds -- shapes_for_row() returns () for them -- because that is
-- the production structure: house_number is NULL on utility/trace rows, so
-- those are reachable only by hopping through a resident's anchor row. One
-- anchor per resident surname at the subject address; without these, the
-- utility row (Tenant) and second trace row (Smith) are invisible to the hop,
-- which is exactly the coverage property the shim documents.
INSERT INTO public.records_legacy
  (record_id, source_file, imported_at, first_name, last_name,
   house_number, address, city, state, zip)
VALUES
  (1901, 'SSNxDOB/ssnxdob_ky.csv', '2025-11-17',
   'Jane', 'Doe', '123', '123 MAIN ST', 'LEXINGTON', 'KY', '40505'),
  (1902, 'SSNxDOB/ssnxdob_ky.csv', '2025-11-17',
   'John', 'Smith', '123', '123 MAIN ST', 'LEXINGTON', 'KY', '40505'),
  (1903, '2014 US Phonebook/phonebook_ky.csv', '2025-11-17',
   'Pat', 'Tenant', '123', '123 MAIN ST', 'LEXINGTON', 'KY', '40505');

-- payday: serves BOTH the loan shape and the drive shape (same physical row).
-- own_rent carries every casing observed in production.
INSERT INTO public.records_new
  (record_id, source_file, imported_at, first_name, last_name, address, city, state, zip,
   own_rent, employer, occupation, dl_number, dl_state, raw_data)
VALUES
  (2001, 'Payday_Big_1/Payday_Big_1.csv', '2026-02-10', 'Jane', 'Doe',
   '123 MAIN ST', 'LEXINGTON', 'KY', '40505', 'RENT', 'ACME', 'Manager',
   'A12345678', 'KY',
   '{"loan_amount": "500", "monthly_income": "3000", "month_pay": "U",
     "address_years": "3", "address_months": "4", "registration_date": "20250901"}'::jsonb),
  (2002, 'Payday_Big_1/Payday_Big_1.csv', '2026-02-10', 'John', 'Smith',
   '456 PINE ST', 'LEXINGTON', 'KY', '40505', 'own', 'BETA', 'Driver', NULL, NULL,
   '{"loan_amount": "900", "monthly_income": "4100", "month_pay": "B"}'::jsonb),
  (2003, 'Payday_Big_2/Payday_Big_2.csv', '2026-02-10', 'A', 'Aa', '1 A ST', 'X', 'KY', '40599', 'OWN',  NULL, NULL, NULL, NULL, '{}'::jsonb),
  (2004, 'Payday_Big_2/Payday_Big_2.csv', '2026-02-10', 'B', 'Bb', '2 B ST', 'X', 'KY', '40599', 'rent', NULL, NULL, NULL, NULL, '{}'::jsonb),
  (2005, 'Payday_Big_2/Payday_Big_2.csv', '2026-02-10', 'C', 'Cc', '3 C ST', 'X', 'KY', '40599', 'Rent', NULL, NULL, NULL, NULL, '{}'::jsonb),
  (2006, 'Payday_Big_2/Payday_Big_2.csv', '2026-02-10', 'D', 'Dd', '4 D ST', 'X', 'KY', '40599', 'Own',  NULL, NULL, NULL, NULL, '{}'::jsonb),
  (2007, 'Payday_Big_2/Payday_Big_2.csv', '2026-02-10', 'E', 'Ee', '5 E ST', 'X', 'KY', '40599', 'r',    NULL, NULL, NULL, NULL, '{}'::jsonb),
  (2008, 'Payday_Big_2/Payday_Big_2.csv', '2026-02-10', 'F', 'Ff', '6 F ST', 'X', 'KY', '40599', 'o',    NULL, NULL, NULL, NULL, '{}'::jsonb),
  (2009, 'Payday_Big_2/Payday_Big_2.csv', '2026-02-10', 'G', 'Gg', '7 G ST', 'X', 'KY', '40599', '1',    NULL, NULL, NULL, NULL, '{}'::jsonb);

-- auto: three key casings across files, as the real feed ships them
INSERT INTO public.records_new
  (record_id, source_file, imported_at, first_name, last_name, address, city, state, zip, phone, raw_data)
VALUES
  (3001, 'AvengerAuto-verified/AvengerAuto.csv', '2026-03-05', 'Jane', 'Doe',
   '123 MAIN ST', 'LEXINGTON', 'KY', '40505', '5551112222',
   '{"VIN": "VIN1", "MAKE": "FORD", "MODEL": "F150", "MODEL_YEAR": "2020"}'::jsonb),
  (3002, 'auto-verified/auto_update_january-2025.csv', '2026-03-05', 'John', 'Smith',
   '123 MAIN ST', 'LEXINGTON', 'KY', '40505', NULL,
   '{"Vin": "VIN2", "Make": "HONDA", "Model": "CIVIC", "Year": "2019"}'::jsonb);

-- property_owner (tax). NOTE: zip and house_number columns are NULL, as in production.
-- Row 4001 is CLEAN and describes an absentee owner (mailing address in another state).
INSERT INTO public.records_new
  (record_id, source_file, imported_at, first_name, last_name, address, city, state, raw_data)
VALUES
  (4001, 'property_owner_49/property_owner_49.csv', '2026-03-05', 'JANE', 'DOE',
   '123 MAIN ST', 'LEXINGTON', 'KY',
   '{"ownerName": "DOE, JANE ANN", "ownerAddressLine1": "777 FAR AWAY DR",
     "ownerCity": "AURORA", "ownerState": "IL", "ownerZipCode": "60504",
     "ownerResCount": "1", "ownerParcelCount": "2",
     "residential": "True", "condo": "False", "buildingArea": "1134.0",
     "totalMarketValue": "195000.0", "totalAssessedValue": "195000.0",
     "taxValue": "2122.77", "lenderName": "USAA FED SAV BK", "buyerIDCode": "ID",
     "recordingDate": "20180627", "totalLienCount": "1", "totalLienBalance": "185250.0",
     "equityCurrentEstBal": "118158.0", "LTVCurrentEstCombined": "61.0564",
     "totalFinancingHistCount": "1", "forecloseCode": "", "forecloseRecordDate": "",
     "addressFormal": "123 MAIN STREET", "streetNumber": "123",
     "zipCodePlusFour": "40505-1046", "fipsState": "21", "fipsCounty": "067"}'::jsonb);

-- 4002 is an LLC owner: derived `ownercompany` must be populated.
INSERT INTO public.records_new
  (record_id, source_file, imported_at, first_name, last_name, address, city, state, raw_data)
VALUES
  (4002, 'property_owner_49/property_owner_49.csv', '2026-03-05', '', 'ACME',
   '123 MAIN ST', 'LEXINGTON', 'KY',
   '{"ownerName": "ACME HOLDINGS LLC", "ownerAddressLine1": "1 CORP PLZ",
     "ownerCity": "CHICAGO", "ownerState": "IL", "ownerZipCode": "60601",
     "residential": "True", "condo": "False", "streetNumber": "123",
     "zipCodePlusFour": "40505-2000", "fipsState": "21", "fipsCounty": "067",
     "ownerResCount": "0", "totalLienCount": "0"}'::jsonb);

-- 4003 is COLUMN-SHIFTED: the embedded GeoJSON split on its commas and every
-- subsequent field slid. streetNumber holds a boolean, ownerZipCode holds a state.
-- The adapter must DROP this row and count it.
INSERT INTO public.records_new
  (record_id, source_file, imported_at, first_name, last_name, address, city, state, raw_data)
VALUES
  (4003, 'property_owner_37/property_owner_37.csv', '2026-03-05', 'FREDERICK', 'V',
   '123 MAIN ST', 'LEXINGTON', 'KY',
   '{"ownerName": "PENGROVE ST", "ownerCity": "PO BOX 6531",
     "ownerState": "PROVIDENCE", "ownerZipCode": "RI", "ownerResCount": "029406531",
     "residential": "''coordinates'': [-71.4538454389609", "condo": "41.789]}",
     "streetNumber": "False", "zipCodePlusFour": "True", "street": "02920-6733",
     "cityUSPS": "44 PENGROVE ST"}'::jsonb);

-- silver: Jane Doe resolves to one entity; the trace + payday rows link to her.
-- HAL0003 is a SECOND non-merged DOE at a different address. It exists so name
-- search has more surname matches than a one-row page can hold: without it,
-- total and len(page) are both 1 and a hardcoded len(rows) would pass for a real
-- count(*) OVER (). HAL0004 is a MERGED DOE -- the graph records its merges but
-- never applies them, so both sides stay in entity_master and only `is_merged`
-- keeps a superseded duplicate out of the results.
INSERT INTO silver.entity_master
  (hal_id, canonical_first_name, canonical_last_name, canonical_address_line1,
   canonical_city, canonical_state, canonical_zip, record_count,
   identity_confidence, is_suspicious, is_merged, merged_into_hal_id)
VALUES
  ('HAL0001', 'JANE', 'DOE', '123 MAIN ST', 'LEXINGTON', 'KY', '40505', 3, 40.50, false, false, NULL),
  ('HAL0002', 'JOHN', 'SMITH', '456 PINE ST', 'LEXINGTON', 'KY', '40505', 1, 88.00, true,  false, NULL),
  ('HAL0003', 'RICHARD', 'DOE', '88 ELM ST', 'LEXINGTON', 'KY', '40507', 2, 61.25, false, false, NULL),
  ('HAL0004', 'MARY', 'DOE', '123 MAIN ST', 'LEXINGTON', 'KY', '40505', 1, 33.00, false, true, 'HAL0001');

-- Blocking keys for the four entities above. search_people no longer queries
-- entity_master by name -- it cannot, production has no name index there (see
-- source/search.py) -- so it resolves names through THESE keys and then reaches
-- entity_master by primary key. Without them, name search finds nobody.
--
-- key_value is 'LAST|FIRST|YYYY-MM-DD', production's exact format, because the
-- lookup is a byte-range over that string: a key written 'FIRST|LAST|...' or
-- lower-cased would fall outside the range the code computes and the search
-- would silently return empty.
--
-- HAL0004 (MARY DOE) gets a key like everyone else. She is is_merged=true, and
-- the point is that the JOIN's `is_merged IS NOT TRUE` is what excludes her --
-- not her absence from the key table, which would make the test pass for the
-- wrong reason.
INSERT INTO silver.unique_keys (key_id, key_type, key_value, hal_id)
VALUES
  (9001, 'name_dob', 'DOE|JANE|1980-04-01',    'HAL0001'),
  (9002, 'name_dob', 'SMITH|JOHN|1975-11-20',  'HAL0002'),
  (9003, 'name_dob', 'DOE|RICHARD|1962-02-11', 'HAL0003'),
  (9004, 'name_dob', 'DOE|MARY|1988-07-30',    'HAL0004'),
  -- Shares the prefix 'DOE' but not the 'DOE|' separator boundary -- the exact
  -- case the range upper bound has to exclude.
  (9005, 'name_dob', 'DOEHRING|ANNA|1991-01-05', NULL);

INSERT INTO silver.entity_links (hal_id, source_table, record_id, match_type, confidence)
VALUES
  ('HAL0001', 'records_legacy', 1002, 'name_dob', 0.90),
  ('HAL0001', 'records_legacy', 1004, 'phone',    0.85),
  ('HAL0001', 'records_new',    2001, 'name_dob', 0.90),
  ('HAL0002', 'records_legacy', 1003, 'name_dob', 0.70);

-- OWNER-ELSEWHERE: the case the hal: traversal exists for.
-- Jane Doe is the owner of record at 123 MAIN ST (tax row 4001 mails to Aurora,
-- IL), and her ER entity ALSO links a payday row at a different street in a
-- different ZIP. Without this row the fixture could only express the degenerate
-- case -- one entity, three rows, all at the single address already resolved --
-- which is not what the engine consumes.
--
-- ZIP 41042 is deliberately outside the 40505 subject address, so the address
-- scan (`WHERE zip = $1`) can never see this row. It is reachable ONLY through
-- entity_links, which is the whole point, and it keeps the 123 MAIN ST bundle's
-- loan/drive counts at 1 so no address-path assertion moves.
INSERT INTO public.records_new
  (record_id, source_file, imported_at, first_name, last_name, address, city, state, zip,
   own_rent, employer, occupation, dl_number, dl_state, raw_data)
VALUES
  (2010, 'Payday_Big_1/Payday_Big_1.csv', '2026-02-10', 'Jane', 'Doe',
   '742 EVERGREEN TER', 'FLORENCE', 'KY', '41042', 'RENT', 'GLOBEX', 'Manager',
   'B98765432', 'KY',
   '{"loan_amount": "750", "monthly_income": "3000", "month_pay": "U"}'::jsonb);

-- Attributed at a HIGHER confidence than any other HAL0001 link, so
-- records_for_hal_id returns it FIRST and rows_for_links therefore fetches its
-- physical table FIRST. That puts the natural fetch order (2010, 1002, 1004,
-- 2001) OUT of record_id order on purpose: it is what makes the handler's sort
-- observable. Without the sort the two-row `loan` block leads with 2010.
-- `records_partitioned` (not `records_new`) so the third PHYSICAL_TABLES entry
-- is exercised against a real database rather than only against fakes.
INSERT INTO silver.entity_links (hal_id, source_table, record_id, match_type, confidence)
VALUES ('HAL0001', 'records_partitioned', 2010, 'name_address', 0.95);

-- Links the COLUMN-SHIFTED property_owner row (4003) so the hal: path's
-- tax_row_is_usable gate executes in its REJECTING direction.
-- SYNTHETIC: production entity_links contains NO property_owner rows at all
-- (0 of 200 sampled) -- see the note on the test that consumes this.
INSERT INTO silver.entity_links (hal_id, source_table, record_id, match_type, confidence)
VALUES ('HAL0002', 'records_partitioned', 4003, 'name_address', 0.55);

ANALYZE;
