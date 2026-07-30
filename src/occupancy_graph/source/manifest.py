"""The shape manifest — the single source of truth for the record contract.

Each shape lists its fields IN CONTRACT ORDER, mapping the raw column name (the
key the consuming engine reads out of a record's `data`) to where the value comes
from in the partner corpus.

Two rules that are easy to get wrong:

1. Keys here are RAW column names and they reach the wire unchanged — the JSON
   payload carries `first_name`, not `firstName`. They are inconsistent between
   shapes because they are the upstream vendor's CSV headers: `utility` uses
   `first_name`, `trace` uses `firstname`. Reproduce them exactly.

2. Order is part of the contract. A record's keys are serialised in the order
   declared here, so reordering a shape changes what the engine reads.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from occupancy_graph.source import derive

OriginKind = Literal["col", "raw", "derived", "absent"]

# Columns that get a `__norm_<name>` helper when present in a shape. Mirrors the
# old graphdb._helper_columns set exactly; changing it changes which `__norm_*`
# keys a projected row carries (source/project.py).
_NORMALIZABLE = {
    "phone", "mobile", "cellphone", "email", "email_02", "email_03",
    "owneraddressline1", "primaryaddress", "address", "street",
}
_BASE_NORM = ("firstname", "lastname", "name_key", "address", "address_zip_key")


@dataclass(frozen=True)
class FieldOrigin:
    kind: OriginKind
    key: str | None = None
    fn: Callable[[Mapping[str, Any]], Any] | None = field(default=None, compare=False)


def col(key: str) -> FieldOrigin:
    """Value comes straight from a top-level partner column."""
    return FieldOrigin(kind="col", key=key)


def raw(key: str) -> FieldOrigin:
    """Value comes from a key inside the row's raw_data jsonb."""
    return FieldOrigin(kind="raw", key=key)


def derived(fn: Callable[[Mapping[str, Any]], Any]) -> FieldOrigin:
    """Value is computed from the row.

    `fn` receives the WHOLE partner row as a mapping of column name -> value,
    with `raw_data` still NESTED under the "raw_data" key rather than flattened.
    A derived function that needs a raw_data key reaches into it itself.

    `key` carries the function's name so that two derived origins backed by
    different functions are distinguishable by equality. `fn` itself is excluded
    from comparison (compare=False), so without this every derived() origin would
    compare equal to every other and a mis-wiring would be invisible to tests.

    Factory-produced functions (e.g. a future `derive.year_of(column)` or
    `derive.first_raw(*keys)` that returns an inner `_fn`) all share that inner
    name by default, which would collapse back into the same blind spot. Any
    such factory in `derive.py` MUST set `_fn.__name__` to something
    distinguishing (e.g. `f"year_of_{column}"`, `f"first_raw_{'_'.join(keys)}"`)
    before returning it.
    """
    return FieldOrigin(kind="derived", key=getattr(fn, "__name__", None), fn=fn)


def absent() -> FieldOrigin:
    """Declared unavailable in the partner corpus. Always null."""
    return FieldOrigin(kind="absent")


@dataclass(frozen=True)
class ShapeSpec:
    name: str
    graphql_type: str
    collection_field: str
    singular_field: str
    id_linked: bool
    fields: Mapping[str, FieldOrigin]

    @property
    def columns(self) -> tuple[str, ...]:
        return tuple(self.fields)

    @property
    def norm_fields(self) -> tuple[str, ...]:
        extra = [c for c in self.fields if c in _NORMALIZABLE]
        return tuple(dict.fromkeys([*_BASE_NORM, *extra]))


TAX = ShapeSpec(
    name="tax",
    graphql_type="TaxRecord",
    collection_field="taxes",
    singular_field="tax",
    id_linked=True,
    fields={
        "id": derived(derive.synthetic_id),
        "tax_id": derived(derive.synthetic_id),
        "address": col("address"),
        "addressformal": raw("addressFormal"),
        "housenumber": raw("streetNumber"),
        "city": col("city"),
        "state": col("state"),
        # The `zip` COLUMN is 0% populated on property_owner rows; the value lives
        # in raw_data.zipCodePlusFour as "40505-1046".
        "zip": derived(derive.tax_zip5),
        "county": derived(derive.fips_county),
        "firstname": col("first_name"),
        "lastname": col("last_name"),
        "ownername": raw("ownerName"),
        # No ownercompany column exists upstream. The entity-owner signal is inside
        # ownerName, and company_or_trust_owner is the only consumer.
        "ownercompany": derived(derive.company_from_owner_name),
        "owneraddressline1": raw("ownerAddressLine1"),
        "ownercity": raw("ownerCity"),
        "ownerstate": raw("ownerState"),
        "ownerzipcode": raw("ownerZipCode"),
        "residential": raw("residential"),
        "condo": raw("condo"),
        # property_owner has no yearBuilt key. base.homeyearbuilt carries the
        # signal with honest provenance; the property_tax_context gate lists
        # yearbuilt as one of fifteen alternatives, so it is not load-bearing.
        "yearbuilt": absent(),
        "buildingarea": raw("buildingArea"),
        "totalmarketvalue": raw("totalMarketValue"),
        "totalassessedvalue": raw("totalAssessedValue"),
        "taxvalue": raw("taxValue"),
        "lendername": raw("lenderName"),
        "buyeridcode": raw("buyerIDCode"),
        "recordingdate": raw("recordingDate"),
        "totalliencount": raw("totalLienCount"),
        "totallienbalance": raw("totalLienBalance"),
        "equitycurrentestbal": raw("equityCurrentEstBal"),
        "ltvcurrentestcombined": raw("LTVCurrentEstCombined"),
        "totalfinancinghistcount": raw("totalFinancingHistCount"),
        "foreclosecode": raw("forecloseCode"),
        "forecloserecorddate": raw("forecloseRecordDate"),
        "ownerrescount": raw("ownerResCount"),
    },
)

UTILITY = ShapeSpec(
    name="utility", graphql_type="UtilityRecord",
    collection_field="utilities", singular_field="utility", id_linked=False,
    fields={
        # The utility feed carries NO raw_data at all (0% in production) and no
        # date field of any kind. Every field is a plain column.
        "first_name": col("first_name"),
        "last_name": col("last_name"),
        "middle_name": col("middle_name"),
        "dob": col("dob"),
        "dod": col("dod"),
        "address": col("address"),
        "city": col("city"),
        "county": col("county"),
        "state": col("state"),
        "zip": col("zip"),
        "phone": col("phone"),
    },
)

TRACE = ShapeSpec(
    name="trace", graphql_type="TraceRecord",
    collection_field="traces", singular_field="trace", id_linked=True,
    fields={
        "id": derived(derive.synthetic_id),
        "trace_id": derived(derive.synthetic_id),
        "phone": col("phone"),
        "cellphone": col("mobile"),
        "email": col("email"),
        "email_02": raw("Email_02"),
        "email_03": raw("Email_03"),
        "housenumber": derived(derive.house_number),
        "address": col("address"),
        "city": col("city"),
        "state": col("state"),
        "zip": col("zip"),
        "county": col("county"),
        "firstname": col("first_name"),
        "middlename": col("middle_name"),
        "lastname": col("last_name"),
        "dob_day": raw("Date_Of_Birth_Day"),
        "dob_month": raw("Date_Of_Birth_Month"),
        "dob_year": raw("Date_Of_Birth_Year"),
        "credit_capacity": raw("Credit_Capacity"),
        "home_built_year": raw("Home_Built_Year"),
        "home_purchase_date": raw("Home_Purchase_Date"),
        "income_description": raw("Income_Description"),
    },
)

LOAN = ShapeSpec(
    name="loan", graphql_type="LoanRecord",
    collection_field="loans", singular_field="loan", id_linked=True,
    fields={
        "id": derived(derive.synthetic_id),
        "loan_id": derived(derive.synthetic_id),
        "loan_amount": raw("loan_amount"),
        "monthly_income": raw("monthly_income"),
        "month_pay": raw("month_pay"),
        "own_rent": col("own_rent"),  # normalized in quality.py
        "employer": col("employer"),
        "occupation": col("occupation"),
        "address": col("address"),
        "zip": col("zip"),
        "firstname": col("first_name"),
        "lastname": col("last_name"),
    },
)

DRIVE = ShapeSpec(
    name="drive", graphql_type="DriveRecord",
    collection_field="drives", singular_field="drive", id_linked=True,
    fields={
        # There is no DMV feed in the partner corpus. These are payday-loan rows
        # that happen to carry a licence number — the SAME physical rows the loan
        # shape reads. See the spec: policy.ts currently weights `drive` at 1.15
        # as if it were an independent source, which double-counts. Fixing that
        # is engine-side and out of scope here.
        "id": derived(derive.synthetic_id),
        "drive_id": derived(derive.synthetic_id),
        "dl_num": col("dl_number"),
        "dl_state": col("dl_state"),
        "zip": col("zip"),
        "address": col("address"),
        "firstname": col("first_name"),
        "lastname": col("last_name"),
    },
)

AUTO = ShapeSpec(
    name="auto", graphql_type="AutoRecord",
    collection_field="autos", singular_field="auto", id_linked=True,
    fields={
        "id": derived(derive.synthetic_id),
        "auto_id": derived(derive.synthetic_id),
        # The auto feed ships three casings of the same keys across its files.
        "vin": derived(derive.first_raw("VIN", "Vin", "VIN_ID")),
        "zip": col("zip"),
        "city": col("city"),
        "make": derived(derive.first_raw("MAKE", "Make")),
        "model": derived(derive.first_raw("MODEL", "Model")),
        "year": derived(derive.first_raw("MODEL_YEAR", "YEAR", "Year")),
        "phone": col("phone"),
        "address": col("address"),
        "housenumber": derived(derive.house_number),
        "firstname": col("first_name"),
        "lastname": col("last_name"),
    },
)

BASE = ShapeSpec(
    name="base", graphql_type="BaseRecord",
    collection_field="baseRecords", singular_field="baseRecord", id_linked=True,
    fields={
        "firstname": col("first_name"),
        "middlename": col("middle_name"),
        "lastname": col("last_name"),
        "housenumber": derived(derive.house_number),
        "primaryaddress": col("address"),
        "state": col("state"),
        "zip": col("zip"),
        "city": col("city"),
        "phone": col("phone"),
        "estimatedincomecode": col("estimated_income"),
        # An H/R/9 code (homeowner / renter / unknown), NOT a probability.
        "homeownerprobabilitymodel": col("home_owner_probability"),
        "lengthofresidence": col("length_of_residence"),
        "presenceofchildren": col("presence_of_children"),
        "persongender": col("gender"),
        "persondateofbirthyear": derived(derive.dob_year),
        "persondateofbirthmonth": derived(derive.dob_month),
        "persondateofbirthday": derived(derive.dob_day),
        "personmaritalstatus": col("marital_status"),
        "personeducation": col("education"),
        "businessowner": col("business_owner"),
        "creditrating": col("credit_rating"),
        "networth": col("net_worth"),
        "homepurchaseprice": col("home_purchase_price"),
        "homepurchasedateyear": derived(derive.year_of("home_purchase_date")),
        "homeyearbuilt": col("home_year_built"),
        "estimatedcurrenthomevaluecode": col("estimated_home_value"),
        # Already in thousands upstream (observed 3-598, avg 171). Direct mapping.
        "mortgageamountinthousands": col("mortgage_amount"),
        "mortgagelendername": col("mortgage_lender"),
        "deeddateofrefinanceyear": derived(derive.year_of("refinance_date")),
        "refinanceamountinthousands": col("refinance_amount"),
        "refinancelendername": col("refinance_lender"),
        "dob": col("dob"),
        "id": derived(derive.synthetic_id),
    },
)

# Order matters: it is the order of the `records_by_source` keys on the wire
# (service/records.ALL_SHAPES is `tuple(SHAPES)`). This is the old SOURCE_FILES
# order with voter/criminal/linkedin removed.
SHAPES: dict[str, ShapeSpec] = {
    "base": BASE, "auto": AUTO, "drive": DRIVE,
    "loan": LOAN, "tax": TAX, "trace": TRACE, "utility": UTILITY,
}
