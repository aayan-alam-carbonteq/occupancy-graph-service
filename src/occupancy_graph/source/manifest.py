"""The shape manifest — the single source of truth for the GraphQL contract.

Each shape lists its fields IN CONTRACT ORDER, mapping the raw column name (the
key the consuming engine reads out of SourceRecord.data) to where the value comes
from in the partner corpus.

Two rules that are easy to get wrong:

1. Keys here are RAW column names, not GraphQL field names. Strawberry camelCases
   them (`first_name` -> `firstName`, `dob_day` -> `dobDay`). The engine reads the
   raw names. They are inconsistent between shapes because they are the upstream
   vendor's CSV headers: `utility` uses `first_name`, `trace` uses `firstname`.
   Reproduce them exactly.

2. Order is part of the SDL. Reordering a shape changes schema.graphql and breaks
   the contract test.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from occupancy_graph.source import derive

OriginKind = Literal["col", "raw", "derived", "absent"]

# Columns that get a `__norm_<name>` helper when present in a shape. Mirrors the
# old graphdb._helper_columns set exactly; changing it changes the *Norm types.
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

SHAPES: dict[str, ShapeSpec] = {"tax": TAX}
