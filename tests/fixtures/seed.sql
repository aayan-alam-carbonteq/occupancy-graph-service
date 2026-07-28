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

-- payday: serves BOTH the loan shape and the drive shape (same physical row).
-- own_rent carries every casing observed in production.
INSERT INTO public.records_partitioned
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
INSERT INTO public.records_partitioned
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
INSERT INTO public.records_partitioned
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
INSERT INTO public.records_partitioned
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
INSERT INTO public.records_partitioned
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
INSERT INTO silver.entity_master
  (hal_id, canonical_first_name, canonical_last_name, canonical_address_line1,
   canonical_city, canonical_state, canonical_zip, record_count,
   identity_confidence, is_suspicious, is_merged)
VALUES
  ('HAL0001', 'JANE', 'DOE', '123 MAIN ST', 'LEXINGTON', 'KY', '40505', 3, 40.50, false, false),
  ('HAL0002', 'JOHN', 'SMITH', '456 PINE ST', 'LEXINGTON', 'KY', '40505', 1, 88.00, true,  false);

INSERT INTO silver.entity_links (hal_id, source_table, record_id, match_type, confidence)
VALUES
  ('HAL0001', 'records_legacy', 1002, 'name_dob', 0.90),
  ('HAL0001', 'records_legacy', 1004, 'phone',    0.85),
  ('HAL0001', 'records_new',    2001, 'name_dob', 0.90),
  ('HAL0002', 'records_legacy', 1003, 'name_dob', 0.70);

ANALYZE;
