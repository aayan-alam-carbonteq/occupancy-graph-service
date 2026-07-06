"""Build a tiny sample graph DB for schema introspection and smoke tests.

This mirrors ``tests/graph_fixtures.write_graph_fixture`` (minimal valid CSVs
for every source) but lives at the package level so it can be used by an
installed console script (e.g. ``occupancy_graph.graphql.export_schema``)
without depending on the ``tests/`` directory, which isn't importable outside
the test run.
"""
from __future__ import annotations

import csv
import tempfile
from pathlib import Path

from occupancy_graph.graphdb.core import build_index


def sample_db(db_path: Path) -> None:
    """Write a minimal but valid graph sqlite DB to ``db_path``."""
    with tempfile.TemporaryDirectory() as tmp:
        cleaned = Path(tmp) / "cleaned"
        _write_sample_cleaned_dir(cleaned)
        build_index(cleaned, db_path)


def _write_sample_cleaned_dir(cleaned: Path) -> None:
    cleaned.mkdir(parents=True, exist_ok=True)
    _write_csv(
        cleaned / "base.csv",
        ["firstname", "middlename", "lastname", "housenumber", "primaryaddress", "state", "zip", "city", "phone", "estimatedincomecode", "homeownerprobabilitymodel", "lengthofresidence", "presenceofchildren", "persongender", "persondateofbirthyear", "persondateofbirthmonth", "persondateofbirthday", "personmaritalstatus", "personeducation", "businessowner", "creditrating", "networth", "homepurchaseprice", "homepurchasedateyear", "homeyearbuilt", "estimatedcurrenthomevaluecode", "mortgageamountinthousands", "mortgagelendername", "deeddateofrefinanceyear", "refinanceamountinthousands", "refinancelendername", "dob", "id"],
        [
            _base_row("p1", "Jane", "Doe", "123 MAIN ST", "40505", "5551112222", "H"),
            _base_row("p2", "John", "Smith", "123 MAIN ST", "40505", "5552223333", "H"),
            _base_row("p3", "John", "Smith", "456 PINE ST", "40505", "5553334444", "R"),
        ],
    )
    _write_csv(
        cleaned / "tax.csv",
        ["id", "tax_id", "address", "addressformal", "housenumber", "city", "state", "zip", "county", "firstname", "lastname", "ownername", "ownercompany", "owneraddressline1", "ownercity", "ownerstate", "ownerzipcode", "residential", "condo", "yearbuilt", "buildingarea", "totalmarketvalue", "totalassessedvalue", "taxvalue", "lendername", "buyeridcode", "recordingdate", "totalliencount", "totallienbalance", "equitycurrentestbal", "ltvcurrentestcombined", "totalfinancinghistcount", "foreclosecode", "forecloserecorddate", "ownerrescount"],
        [{"id": "p1", "tax_id": "x1", "address": "123 MAIN ST", "addressformal": "123 MAIN STREET", "housenumber": "123", "city": "LEXINGTON", "state": "KY", "zip": "40505", "county": "FAYETTE", "firstname": "Jane", "lastname": "Doe", "ownername": "DOE, JANE", "ownercompany": "", "owneraddressline1": "777 MAIL RD", "ownercity": "LEXINGTON", "ownerstate": "KY", "ownerzipcode": "40505", "residential": "True", "condo": "False", "yearbuilt": "1990", "buildingarea": "1000", "totalmarketvalue": "200000", "totalassessedvalue": "200000", "taxvalue": "2000", "lendername": "BANK", "buyeridcode": "", "recordingdate": "20200101", "totalliencount": "1", "totallienbalance": "100000", "equitycurrentestbal": "100000", "ltvcurrentestcombined": "50", "totalfinancinghistcount": "1", "foreclosecode": "", "forecloserecorddate": "", "ownerrescount": "0"}],
    )
    _write_csv(cleaned / "utility.csv", ["first_name", "last_name", "middle_name", "dob", "dod", "address", "city", "county", "state", "zip", "phone"], [{"first_name": "Pat", "last_name": "Tenant", "middle_name": "", "dob": "", "dod": "", "address": "123 MAIN ST", "city": "LEXINGTON", "county": "FAYETTE", "state": "KY", "zip": "40505", "phone": "5557778888"}])
    _write_csv(cleaned / "auto.csv", ["id", "auto_id", "vin", "zip", "city", "make", "model", "year", "phone", "address", "housenumber", "firstname", "lastname"], [{"id": "p1", "auto_id": "a1", "vin": "VIN1", "zip": "40505", "city": "LEXINGTON", "make": "FORD", "model": "F150", "year": "2020", "phone": "5551112222", "address": "456 PINE ST", "housenumber": "456", "firstname": "Jane", "lastname": "Doe"}])
    _write_csv(cleaned / "drive.csv", ["id", "drive_id", "dl_num", "dl_state", "zip", "address", "firstname", "lastname"], [{"id": "p1", "drive_id": "d1", "dl_num": "A12345678", "dl_state": "KY", "zip": "40505", "address": "123 MAIN ST", "firstname": "Janet", "lastname": "Doe"}])
    _write_csv(cleaned / "loan.csv", ["id", "loan_id", "loan_amount", "monthly_income", "month_pay", "own_rent", "employer", "occupation", "address", "zip", "firstname", "lastname"], [{"id": "p1", "loan_id": "l1", "loan_amount": "500", "monthly_income": "3000", "month_pay": "U", "own_rent": "RENT", "employer": "ACME", "occupation": "Manager", "address": "123 MAIN ST", "zip": "40505", "firstname": "Jane", "lastname": "Doe"}])
    _write_csv(cleaned / "voter.csv", ["id", "voter_id", "phone", "mobile", "housenumber", "gender", "email", "zip", "address", "firstname", "lastname"], [{"id": "p1", "voter_id": "v1", "phone": "", "mobile": "5551112222", "housenumber": "123", "gender": "F", "email": "", "zip": "40505", "address": "123 MAIN ST", "firstname": "Jane", "lastname": "Doe"}])
    _write_csv(cleaned / "criminal.csv", ["id", "criminal_id", "category", "offensedesc1", "counts", "sourcename", "county", "arrestdate", "admitteddate", "releasedate", "offensedate", "chargesfileddate", "sentenceyyymmddd", "firstname", "middlename", "lastname", "age", "dob", "dob_day", "dob_month", "dob_year", "address", "city", "state", "zip", "height", "weight", "eye", "hair", "scarsmarks"], [{"id": "p1", "criminal_id": "c1", "category": "Arrests", "offensedesc1": "TEST", "counts": "", "sourcename": "", "county": "FAYETTE", "arrestdate": "20200101", "admitteddate": "20200101", "releasedate": "", "offensedate": "", "chargesfileddate": "", "sentenceyyymmddd": "", "firstname": "Jane", "middlename": "", "lastname": "Doe", "age": "40", "dob": "", "dob_day": "", "dob_month": "", "dob_year": "", "address": "LEXINGTON, KY", "city": "LEXINGTON", "state": "KY", "zip": "40505", "height": "", "weight": "", "eye": "", "hair": "", "scarsmarks": ""}])
    _write_csv(cleaned / "linkedin.csv", ["id", "linkedin_id", "firstname", "lastname", "linkedinurl", "pictureurl", "location_linkedintext", "summary", "premium", "openprofile", "position_index", "position_title", "position_companyname", "position_companyid", "position_companylinkedinurl", "position_current", "position_startedon_year", "position_startedon_month", "position_tenureatcompany_numyears", "position_tenureatcompany_nummonths", "position_tenureatposition_numyears", "position_tenureatposition_nummonths", "position_description"], [{"id": "p1", "linkedin_id": "li1", "firstname": "Jane", "lastname": "Doe", "linkedinurl": "https://linkedin.example/jane", "pictureurl": "", "location_linkedintext": "Lexington", "summary": "", "premium": "false", "openprofile": "false", "position_index": "0", "position_title": "Manager", "position_companyname": "ACME", "position_companyid": "", "position_companylinkedinurl": "", "position_current": "true", "position_startedon_year": "2020", "position_startedon_month": "1", "position_tenureatcompany_numyears": "4", "position_tenureatcompany_nummonths": "1", "position_tenureatposition_numyears": "4", "position_tenureatposition_nummonths": "1", "position_description": ""}])
    _write_csv(cleaned / "trace.csv", ["id", "trace_id", "phone", "cellphone", "email", "email_02", "email_03", "housenumber", "address", "city", "state", "zip", "county", "firstname", "middlename", "lastname", "dob_day", "dob_month", "dob_year", "credit_capacity", "home_built_year", "home_purchase_date", "income_description"], [{"id": "p1", "trace_id": "t1", "phone": "5551112222", "cellphone": "", "email": "jane@example.com", "email_02": "", "email_03": "", "housenumber": "123", "address": "123 MAIN ST", "city": "LEXINGTON", "state": "KY", "zip": "40505", "county": "FAYETTE", "firstname": "Jane", "middlename": "", "lastname": "Doe", "dob_day": "1", "dob_month": "1", "dob_year": "1980", "credit_capacity": "", "home_built_year": "", "home_purchase_date": "", "income_description": ""}])


def _base_row(id_value: str, firstname: str, lastname: str, address: str, zip_code: str, phone: str, homeowner: str) -> dict[str, str]:
    return {
        "firstname": firstname,
        "middlename": "",
        "lastname": lastname,
        "housenumber": address.split()[0],
        "primaryaddress": address,
        "state": "KY",
        "zip": zip_code,
        "city": "Lexington",
        "phone": phone,
        "estimatedincomecode": "",
        "homeownerprobabilitymodel": homeowner,
        "lengthofresidence": "",
        "presenceofchildren": "",
        "persongender": "",
        "persondateofbirthyear": "",
        "persondateofbirthmonth": "",
        "persondateofbirthday": "",
        "personmaritalstatus": "",
        "personeducation": "",
        "businessowner": "",
        "creditrating": "",
        "networth": "",
        "homepurchaseprice": "",
        "homepurchasedateyear": "",
        "homeyearbuilt": "",
        "estimatedcurrenthomevaluecode": "",
        "mortgageamountinthousands": "",
        "mortgagelendername": "",
        "deeddateofrefinanceyear": "",
        "refinanceamountinthousands": "",
        "refinancelendername": "",
        "dob": "",
        "id": id_value,
    }


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
