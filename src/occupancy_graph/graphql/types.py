from __future__ import annotations

import dataclasses
from enum import Enum
from typing import Any

import strawberry
from strawberry.scalars import JSON
from strawberry.types import Info

from occupancy_graph.graphdb.core import normalize_address, normalize_text
from occupancy_graph.graphql.db import GraphDatabase, quote
from occupancy_graph.graphql.filters import (
    OrderByInput,
    StringFilterInput,
    build_order_clause,
    build_where_clause,
)
from occupancy_graph.graphql.guardrails import clamp_limit, clamp_offset
from occupancy_graph.graphql.registry import ADDRESS_COLLECTION_RELATIONS, ADDRESS_ROLES_BY_SOURCE, SchemaRegistry, TableSchema


@strawberry.enum
class Source(Enum):
    BASE = "base"
    TAX = "tax"
    UTILITY = "utility"
    TRACE = "trace"
    AUTO = "auto"
    LOAN = "loan"
    DRIVE = "drive"
    VOTER = "voter"
    CRIMINAL = "criminal"
    LINKEDIN = "linkedin"


@strawberry.enum
class AddressAssociationRole(Enum):
    RESIDENCE = "RESIDENCE"
    SERVICE_ADDRESS = "SERVICE"
    LICENSE_ADDRESS = "LICENSE"
    REGISTRATION_ADDRESS = "REGISTRATION"
    MAILING_ADDRESS = "OWNER_MAILING"
    RECORD_ADDRESS = "RECORD"
    TRACE_ADDRESS = "TRACE"


@strawberry.enum
class PropertyAddressRole(Enum):
    SITUS_ADDRESS = "property"
    MAILING_ADDRESS = "owner_mailing"


@strawberry.enum
class PropertyPersonRole(Enum):
    OWNER = "owner"
    BUYER = "buyer"
    BORROWER = "borrower"
    TAXPAYER = "taxpayer"
    RECORDED_PARTY = "recorded_party"


@strawberry.enum
class PropertyOrganizationRole(Enum):
    OWNER_ENTITY = "owner_entity"
    LENDER = "lender"
    SERVICER = "servicer"


@strawberry.enum
class OrganizationPersonRole(Enum):
    EMPLOYER = "employer"
    AFFILIATION = "affiliation"


@strawberry.type
class PageInfo:
    total_count: int
    has_more: bool
    limit: int
    offset: int


def _page(total_count: int, limit: int | None, offset: int | None) -> PageInfo:
    actual_limit = clamp_limit(limit)
    actual_offset = clamp_offset(offset)
    return PageInfo(
        total_count=total_count,
        has_more=actual_offset + actual_limit < total_count,
        limit=actual_limit,
        offset=actual_offset,
    )


def _page_overfetch(total_count: int, has_more: bool, limit: int | None, offset: int | None) -> PageInfo:
    """Build a PageInfo with an explicit ``has_more`` (from an over-fetch by one).

    Unlike ``_page``, ``has_more`` is exact and ``total_count`` is a best-effort
    lower bound rather than the exact count, avoiding a full COUNT(*) scan.
    """
    return PageInfo(
        total_count=total_count,
        has_more=has_more,
        limit=clamp_limit(limit),
        offset=clamp_offset(offset),
    )


@strawberry.type
class SourceRecord:
    source: Source
    table: str
    rowid: int
    record_id: str | None
    summary: str
    data: JSON


@strawberry.type
class Address:
    id: int
    norm_address: str
    zip5: str
    street_number: str | None
    street_name: str | None
    unit: str | None
    city: str | None
    state: str | None
    county: str | None

    @strawberry.field
    def canonical(self) -> str:
        return f"{self.norm_address}, {self.zip5}" if self.zip5 else self.norm_address

    @strawberry.field
    def full_address(self) -> str:
        return self.canonical()

    @strawberry.field
    def normalized_address(self) -> str:
        return self.norm_address

    @strawberry.field
    def zip(self) -> str | None:
        return self.zip5 or None

    @strawberry.field
    def person_associations(
        self,
        info: Info,
        source: Source | None = None,
        role: AddressAssociationRole | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> "PersonAddressAssociationConnection":
        return _person_address_associations_for_address(
            info.context.db,
            self.id,
            source=_source_value(source),
            role=_address_relation_value(role),
            limit=limit,
            offset=offset,
        )

    @strawberry.field
    def property_associations(
        self,
        info: Info,
        source: Source | None = None,
        role: PropertyAddressRole | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> "PropertyAddressAssociationConnection":
        return _property_address_associations_for_address(
            info.context.db,
            self.id,
            source=_source_value(source),
            role=_property_address_role_value(role),
            limit=limit,
            offset=offset,
        )

    @strawberry.field
    def source_records(
        self,
        info: Info,
        source: Source | None = None,
        role: AddressAssociationRole | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> "SourceRecordConnection":
        return _source_records_for_address(
            info.context.db,
            self.id,
            source=_source_value(source),
            role=_address_edge_role_value(role),
            limit=limit,
            offset=offset,
        )

    @strawberry.field
    def residents(self, info: Info, limit: int | None = None, offset: int | None = None) -> "PersonConnection":
        return _people_for_address(info.context.db, self.id, relation_types=["RESIDENCE"], limit=limit, offset=offset)

    @strawberry.field
    def people(self, info: Info, relation_types: list[str] | None = None, limit: int | None = None, offset: int | None = None) -> "PersonConnection":
        return _people_for_address(info.context.db, self.id, relation_types=relation_types, limit=limit, offset=offset)

    @strawberry.field
    def base_records(self, info: Info, limit: int | None = None, offset: int | None = None) -> "SourceRecordConnection":
        return _records_for_address_connection(info, "base", "residence", self.id, limit, offset)

    @strawberry.field
    def utility_records(self, info: Info, limit: int | None = None, offset: int | None = None) -> "SourceRecordConnection":
        return _records_for_address_connection(info, "utility", "service", self.id, limit, offset)

    @strawberry.field
    def tax_properties(self, info: Info, limit: int | None = None, offset: int | None = None) -> "SourceRecordConnection":
        return _records_for_address_connection(info, "tax", "property", self.id, limit, offset)

    @strawberry.field
    def owner_mailing_of(self, info: Info, limit: int | None = None, offset: int | None = None) -> "SourceRecordConnection":
        return _records_for_address_connection(info, "tax", "owner_mailing", self.id, limit, offset)

    @strawberry.field
    def trace_records(self, info: Info, limit: int | None = None, offset: int | None = None) -> "SourceRecordConnection":
        return _records_for_address_connection(info, "trace", "residence", self.id, limit, offset)

    @strawberry.field
    def auto_records(self, info: Info, limit: int | None = None, offset: int | None = None) -> "SourceRecordConnection":
        return _records_for_address_connection(info, "auto", "registration", self.id, limit, offset)

    @strawberry.field
    def loan_records(self, info: Info, limit: int | None = None, offset: int | None = None) -> "SourceRecordConnection":
        return _records_for_address_connection(info, "loan", "residence", self.id, limit, offset)

    @strawberry.field
    def drive_records(self, info: Info, limit: int | None = None, offset: int | None = None) -> "SourceRecordConnection":
        return _records_for_address_connection(info, "drive", "license", self.id, limit, offset)

    @strawberry.field
    def voter_records(self, info: Info, limit: int | None = None, offset: int | None = None) -> "SourceRecordConnection":
        return _records_for_address_connection(info, "voter", "registration", self.id, limit, offset)

    @strawberry.field
    def criminal_records(self, info: Info, limit: int | None = None, offset: int | None = None) -> "SourceRecordConnection":
        return _records_for_address_connection(info, "criminal", "record", self.id, limit, offset)


@strawberry.type
class Person:
    id: str
    firstname: str | None
    middlename: str | None
    lastname: str | None
    full_name: str | None
    norm_name_key: str | None
    base_rowid: int
    primary_address_id: int | None

    @strawberry.field
    def name(self) -> str | None:
        return self.full_name

    @strawberry.field
    def names(self, info: Info, limit: int | None = None, offset: int | None = None) -> "NameObservationConnection":
        nodes, total = _name_observations_for_person(info.context.db, self.id, limit, offset)
        return NameObservationConnection(page_info=_page(total, limit, offset), nodes=nodes)

    @strawberry.field
    def address_associations(
        self,
        info: Info,
        source: Source | None = None,
        role: AddressAssociationRole | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> "PersonAddressAssociationConnection":
        return _person_address_associations_for_person(
            info.context.db,
            self.id,
            source=_source_value(source),
            role=_address_relation_value(role),
            limit=limit,
            offset=offset,
        )

    @strawberry.field
    def property_associations(
        self,
        info: Info,
        source: Source | None = None,
        role: PropertyPersonRole | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> "PropertyPersonAssociationConnection":
        return _property_person_associations_for_person(
            info.context.db,
            self.id,
            source=_source_value(source),
            role=_property_person_role_value(role),
            limit=limit,
            offset=offset,
        )

    @strawberry.field
    def organization_associations(
        self,
        info: Info,
        source: Source | None = None,
        role: OrganizationPersonRole | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> "PersonOrganizationAssociationConnection":
        return _person_organization_associations(
            info.context.db,
            self.id,
            source=_source_value(source),
            role=_organization_person_role_value(role),
            limit=limit,
            offset=offset,
        )

    @strawberry.field
    def contact_associations(self, info: Info, source: Source | None = None, limit: int | None = None, offset: int | None = None) -> "PersonContactAssociationConnection":
        return _person_contact_associations(info.context.db, self.id, source=_source_value(source), limit=limit, offset=offset)

    @strawberry.field
    def vehicle_associations(self, info: Info, source: Source | None = None, limit: int | None = None, offset: int | None = None) -> "PersonVehicleAssociationConnection":
        return _person_vehicle_associations(info.context.db, self.id, source=_source_value(source), limit=limit, offset=offset)

    @strawberry.field
    def source_records(self, info: Info, source: Source | None = None, limit: int | None = None, offset: int | None = None) -> "SourceRecordConnection":
        if source is None:
            return _all_source_records_for_person(info.context.db, self.id, limit=limit, offset=offset)
        return _records_for_person_connection(info, _source_value(source), self.id, limit, offset)

    @strawberry.field
    def addresses(self, info: Info, relation_types: list[str] | None = None, limit: int | None = None, offset: int | None = None) -> "AddressConnection":
        return _addresses_for_person(info.context.db, self.id, relation_types=relation_types, limit=limit, offset=offset)

    @strawberry.field
    def contacts(self, info: Info, limit: int | None = None, offset: int | None = None) -> "ContactConnection":
        return _contacts_for_person(info.context.db, self.id, limit, offset)

    @strawberry.field
    def vehicles(self, info: Info, limit: int | None = None, offset: int | None = None) -> "VehicleConnection":
        return _vehicles_for_person(info.context.db, self.id, limit, offset)

    @strawberry.field
    def edges(self, info: Info, relation_types: list[str] | None = None, limit: int | None = None, offset: int | None = None) -> "EntityEdgeConnection":
        return _edges_for_person(info.context.db, self.id, relation_types=relation_types, limit=limit, offset=offset)

    @strawberry.field
    def base_records(self, info: Info, limit: int | None = None, offset: int | None = None) -> "SourceRecordConnection":
        return _records_for_person_connection(info, "base", self.id, limit, offset)

    @strawberry.field
    def tax_records(self, info: Info, limit: int | None = None, offset: int | None = None) -> "SourceRecordConnection":
        return _records_for_person_connection(info, "tax", self.id, limit, offset)

    @strawberry.field
    def trace_records(self, info: Info, limit: int | None = None, offset: int | None = None) -> "SourceRecordConnection":
        return _records_for_person_connection(info, "trace", self.id, limit, offset)

    @strawberry.field
    def auto_records(self, info: Info, limit: int | None = None, offset: int | None = None) -> "SourceRecordConnection":
        return _records_for_person_connection(info, "auto", self.id, limit, offset)

    @strawberry.field
    def loan_records(self, info: Info, limit: int | None = None, offset: int | None = None) -> "SourceRecordConnection":
        return _records_for_person_connection(info, "loan", self.id, limit, offset)

    @strawberry.field
    def drive_records(self, info: Info, limit: int | None = None, offset: int | None = None) -> "SourceRecordConnection":
        return _records_for_person_connection(info, "drive", self.id, limit, offset)

    @strawberry.field
    def voter_records(self, info: Info, limit: int | None = None, offset: int | None = None) -> "SourceRecordConnection":
        return _records_for_person_connection(info, "voter", self.id, limit, offset)

    @strawberry.field
    def criminal_records(self, info: Info, limit: int | None = None, offset: int | None = None) -> "SourceRecordConnection":
        return _records_for_person_connection(info, "criminal", self.id, limit, offset)

    @strawberry.field
    def linkedin_records(self, info: Info, limit: int | None = None, offset: int | None = None) -> "SourceRecordConnection":
        return _records_for_person_connection(info, "linkedin", self.id, limit, offset)


@strawberry.input
class PersonWhereInput:
    id: StringFilterInput | None = None
    firstname: StringFilterInput | None = None
    lastname: StringFilterInput | None = None
    full_name: StringFilterInput | None = None
    norm_name_key: StringFilterInput | None = None


@strawberry.type
class NameObservation:
    id: int
    person_id: str | None
    full_name: str | None
    firstname: str | None
    middlename: str | None
    lastname: str | None
    norm_name_key: str | None
    source: str
    source_rowid: int
    confidence: float

    @strawberry.field
    def person(self, info: Info) -> Person | None:
        if not self.person_id:
            return None
        return fetch_person(info.context.db, self.person_id)


@strawberry.type
class Contact:
    id: int
    contact_type: str
    value: str


@strawberry.type
class Vehicle:
    id: int
    vin: str

    @strawberry.field
    def person_associations(self, info: Info, source: Source | None = None, limit: int | None = None, offset: int | None = None) -> "PersonVehicleAssociationConnection":
        return _vehicle_person_associations(info.context.db, self.id, source=_source_value(source), limit=limit, offset=offset)


@strawberry.type
class Organization:
    id: int
    name: str
    organization_type: str

    @strawberry.field
    def person_associations(
        self,
        info: Info,
        role: OrganizationPersonRole | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> "PersonOrganizationAssociationConnection":
        return _organization_person_associations(
            info.context.db,
            self.id,
            role=_organization_person_role_value(role),
            limit=limit,
            offset=offset,
        )

    @strawberry.field
    def property_associations(
        self,
        info: Info,
        role: PropertyOrganizationRole | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> "PropertyOrganizationAssociationConnection":
        return _property_organization_associations_for_organization(
            info.context.db,
            self.id,
            role=_property_organization_role_value(role),
            limit=limit,
            offset=offset,
        )


@strawberry.type
class Property:
    id: int
    property_key: str
    source: Source
    source_rowid: int

    @strawberry.field
    def source_records(self, info: Info, source: Source | None = None, limit: int | None = None, offset: int | None = None) -> "SourceRecordConnection":
        del limit, offset
        property_source = _source_value(source) if source is not None else self.source.value
        if property_source != self.source.value:
            return SourceRecordConnection(page_info=_page(0, 0, 0), nodes=[])
        record = fetch_source_record(info.context.db, property_source, self.source_rowid)
        nodes = [record] if record is not None else []
        return SourceRecordConnection(page_info=_page(len(nodes), len(nodes), 0), nodes=nodes)

    @strawberry.field
    def addresses(
        self,
        info: Info,
        role: PropertyAddressRole | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> "PropertyAddressAssociationConnection":
        return _property_address_associations_for_property(
            info.context.db,
            self,
            role=_property_address_role_value(role),
            limit=limit,
            offset=offset,
        )

    @strawberry.field
    def people(
        self,
        info: Info,
        role: PropertyPersonRole | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> "PropertyPersonAssociationConnection":
        return _property_person_associations_for_property(
            info.context.db,
            self,
            role=_property_person_role_value(role),
            limit=limit,
            offset=offset,
        )

    @strawberry.field
    def organizations(
        self,
        info: Info,
        role: PropertyOrganizationRole | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> "PropertyOrganizationAssociationConnection":
        return _property_organization_associations_for_property(
            info.context.db,
            self,
            role=_property_organization_role_value(role),
            limit=limit,
            offset=offset,
        )


@strawberry.type
class PersonAddressAssociation:
    id: int
    person_id: str
    address_id: int
    source: Source
    role: AddressAssociationRole
    source_rowid: int
    confidence: float | None = None
    observed_date: str | None = None

    @strawberry.field
    def person(self, info: Info) -> Person | None:
        return fetch_person(info.context.db, self.person_id)

    @strawberry.field
    def address(self, info: Info) -> Address | None:
        return fetch_address_by_id(info.context.db, self.address_id)

    @strawberry.field
    def provenance(self, info: Info) -> list[SourceRecord]:
        record = fetch_source_record(info.context.db, self.source.value, self.source_rowid)
        return [record] if record else []

    @strawberry.field
    def source_record(self, info: Info) -> SourceRecord | None:
        return fetch_source_record(info.context.db, self.source.value, self.source_rowid)


@strawberry.type
class PropertyAddressAssociation:
    id: int
    property_id: int
    address_id: int
    source: Source
    role: PropertyAddressRole
    source_rowid: int
    observed_date: str | None = None

    @strawberry.field
    def property(self, info: Info) -> Property | None:
        return fetch_property(info.context.db, self.property_id)

    @strawberry.field
    def address(self, info: Info) -> Address | None:
        return fetch_address_by_id(info.context.db, self.address_id)

    @strawberry.field
    def provenance(self, info: Info) -> list[SourceRecord]:
        record = fetch_source_record(info.context.db, self.source.value, self.source_rowid)
        return [record] if record else []

    @strawberry.field
    def source_record(self, info: Info) -> SourceRecord | None:
        return fetch_source_record(info.context.db, self.source.value, self.source_rowid)


@strawberry.type
class PropertyPersonAssociation:
    id: int
    property_id: int
    source: Source
    role: PropertyPersonRole
    source_rowid: int
    person_id: str | None = None
    display_name: str | None = None
    confidence: float | None = None
    observed_date: str | None = None

    @strawberry.field
    def property(self, info: Info) -> Property | None:
        return fetch_property(info.context.db, self.property_id)

    @strawberry.field
    def person(self, info: Info) -> Person | None:
        if not self.person_id:
            return None
        return fetch_person(info.context.db, self.person_id)

    @strawberry.field
    def provenance(self, info: Info) -> list[SourceRecord]:
        record = fetch_source_record(info.context.db, self.source.value, self.source_rowid)
        return [record] if record else []

    @strawberry.field
    def source_record(self, info: Info) -> SourceRecord | None:
        return fetch_source_record(info.context.db, self.source.value, self.source_rowid)


@strawberry.type
class PropertyOrganizationAssociation:
    id: int
    property_id: int
    organization_id: int
    source: Source
    role: PropertyOrganizationRole
    source_rowid: int
    observed_date: str | None = None

    @strawberry.field
    def property(self, info: Info) -> Property | None:
        return fetch_property(info.context.db, self.property_id)

    @strawberry.field
    def organization(self, info: Info) -> Organization | None:
        return fetch_organization(info.context.db, self.organization_id)

    @strawberry.field
    def provenance(self, info: Info) -> list[SourceRecord]:
        record = fetch_source_record(info.context.db, self.source.value, self.source_rowid)
        return [record] if record else []

    @strawberry.field
    def source_record(self, info: Info) -> SourceRecord | None:
        return fetch_source_record(info.context.db, self.source.value, self.source_rowid)


@strawberry.type
class PersonOrganizationAssociation:
    id: int
    person_id: str
    organization_id: int
    source: Source
    role: OrganizationPersonRole
    source_rowid: int
    observed_date: str | None = None

    @strawberry.field
    def person(self, info: Info) -> Person | None:
        return fetch_person(info.context.db, self.person_id)

    @strawberry.field
    def organization(self, info: Info) -> Organization | None:
        return fetch_organization(info.context.db, self.organization_id)

    @strawberry.field
    def provenance(self, info: Info) -> list[SourceRecord]:
        record = fetch_source_record(info.context.db, self.source.value, self.source_rowid)
        return [record] if record else []

    @strawberry.field
    def source_record(self, info: Info) -> SourceRecord | None:
        return fetch_source_record(info.context.db, self.source.value, self.source_rowid)


@strawberry.type
class PersonContactAssociation:
    id: int
    person_id: str
    contact_id: int
    source: Source
    role: str
    source_rowid: int
    confidence: float | None = None

    @strawberry.field
    def person(self, info: Info) -> Person | None:
        return fetch_person(info.context.db, self.person_id)

    @strawberry.field
    def contact(self, info: Info) -> Contact | None:
        return fetch_contact(info.context.db, self.contact_id)

    @strawberry.field
    def provenance(self, info: Info) -> list[SourceRecord]:
        record = fetch_source_record(info.context.db, self.source.value, self.source_rowid)
        return [record] if record else []

    @strawberry.field
    def source_record(self, info: Info) -> SourceRecord | None:
        return fetch_source_record(info.context.db, self.source.value, self.source_rowid)


@strawberry.type
class PersonVehicleAssociation:
    id: int
    person_id: str
    vehicle_id: int
    source: Source
    role: str
    source_rowid: int
    confidence: float | None = None

    @strawberry.field
    def person(self, info: Info) -> Person | None:
        return fetch_person(info.context.db, self.person_id)

    @strawberry.field
    def vehicle(self, info: Info) -> Vehicle | None:
        return fetch_vehicle(info.context.db, self.vehicle_id)

    @strawberry.field
    def provenance(self, info: Info) -> list[SourceRecord]:
        record = fetch_source_record(info.context.db, self.source.value, self.source_rowid)
        return [record] if record else []

    @strawberry.field
    def source_record(self, info: Info) -> SourceRecord | None:
        return fetch_source_record(info.context.db, self.source.value, self.source_rowid)



@strawberry.type
class EntityEdge:
    id: int
    from_type: str
    from_id: str
    to_type: str
    to_id: str
    relation_type: str
    source: str
    source_rowid: int
    confidence: float
    evidence_role: str


@strawberry.type
class PersonSearchResult:
    match_score: float
    matched_fields: list[str]
    person: Person | None
    name_observation: NameObservation


@strawberry.type
class AddressSearchResult:
    match_score: float
    matched_fields: list[str]
    address: Address
    relation_count: int


def _connection_type(name: str, node_type: type) -> type:
    annotations = {"page_info": PageInfo, "total_count": int, "has_more": bool, "nodes": list[node_type]}

    def total_count(root) -> int:
        return root.page_info.total_count

    def has_more(root) -> bool:
        return root.page_info.has_more

    namespace = {
        "__annotations__": annotations,
        "total_count": strawberry.field(resolver=total_count),
        "has_more": strawberry.field(resolver=has_more),
    }
    return strawberry.type(type(name, (), namespace))


PersonConnection = _connection_type("PersonConnection", Person)
AddressConnection = _connection_type("AddressConnection", Address)
NameObservationConnection = _connection_type("NameObservationConnection", NameObservation)
ContactConnection = _connection_type("ContactConnection", Contact)
VehicleConnection = _connection_type("VehicleConnection", Vehicle)
OrganizationConnection = _connection_type("OrganizationConnection", Organization)
PropertyConnection = _connection_type("PropertyConnection", Property)
EntityEdgeConnection = _connection_type("EntityEdgeConnection", EntityEdge)
SourceRecordConnection = _connection_type("SourceRecordConnection", SourceRecord)
PersonAddressAssociationConnection = _connection_type("PersonAddressAssociationConnection", PersonAddressAssociation)
PropertyAddressAssociationConnection = _connection_type("PropertyAddressAssociationConnection", PropertyAddressAssociation)
PropertyPersonAssociationConnection = _connection_type("PropertyPersonAssociationConnection", PropertyPersonAssociation)
PropertyOrganizationAssociationConnection = _connection_type("PropertyOrganizationAssociationConnection", PropertyOrganizationAssociation)
PersonOrganizationAssociationConnection = _connection_type("PersonOrganizationAssociationConnection", PersonOrganizationAssociation)
PersonContactAssociationConnection = _connection_type("PersonContactAssociationConnection", PersonContactAssociation)
PersonVehicleAssociationConnection = _connection_type("PersonVehicleAssociationConnection", PersonVehicleAssociation)
PersonSearchConnection = _connection_type("PersonSearchConnection", PersonSearchResult)
AddressSearchConnection = _connection_type("AddressSearchConnection", AddressSearchResult)


@dataclasses.dataclass
class RecordNode:
    table: str
    rowid: int
    data: dict[str, str]


def row_to_record_node(table: str, row: Any) -> RecordNode:
    data = {key: row[key] for key in row.keys() if key != "rowid"}
    return RecordNode(table=table, rowid=int(row["rowid"]), data=data)


def fetch_person(db: GraphDatabase, person_id: str) -> Person | None:
    row = db.fetch_one("SELECT * FROM person_entities WHERE id = ?", (person_id,))
    return _row_to_person(row) if row else None


def fetch_persons_by_ids(db: GraphDatabase, ids: list[str]) -> dict[str, Person]:
    """Fetch many persons in ONE query, keyed by id. Avoids N+1 lookups."""
    unique_ids = list({pid for pid in ids if pid})
    if not unique_ids:
        return {}
    placeholders = ",".join("?" for _ in unique_ids)
    rows = db.fetch_all(
        f"SELECT * FROM person_entities WHERE id IN ({placeholders})",
        tuple(unique_ids),
    )
    return {row["id"]: _row_to_person(row) for row in rows}


def fetch_persons(db: GraphDatabase, where: PersonWhereInput | None = None, limit: int | None = None, offset: int | None = None) -> PersonConnection:
    schema = TableSchema(
        name="person_entities",
        graphql_type="Person",
        collection_field="persons",
        singular_field="person",
        columns=("id", "firstname", "lastname", "full_name", "norm_name_key"),
        norm_columns=(),
        filter_columns=("id", "firstname", "lastname", "full_name", "norm_name_key"),
        id_linked=False,
    )
    where_sql, values = build_where_clause(schema, where)
    total = int(db.fetch_value(f"SELECT COUNT(*) FROM person_entities{where_sql}", tuple(values)))
    rows = db.fetch_all(
        f"SELECT * FROM person_entities{where_sql} ORDER BY id LIMIT ? OFFSET ?",
        tuple(values) + (clamp_limit(limit), clamp_offset(offset)),
    )
    return PersonConnection(page_info=_page(total, limit, offset), nodes=[_row_to_person(row) for row in rows])


def fetch_address_by_id(db: GraphDatabase, address_id: int) -> Address | None:
    row = db.fetch_one("SELECT * FROM addresses WHERE id = ?", (address_id,))
    return _row_to_address(row) if row else None


def fetch_address(db: GraphDatabase, norm_address: str, zip_code: str) -> Address | None:
    normalized = normalize_address(norm_address)
    row = db.fetch_one(
        "SELECT * FROM addresses WHERE norm_address = ? AND zip5 = ?",
        (normalized, (zip_code or "").strip()[:5]),
    )
    return _row_to_address(row) if row else None


def fetch_address_by_text(db: GraphDatabase, query: str, zip_code: str | None = None) -> Address | None:
    normalized = normalize_address(query)
    params: list[Any] = [normalized]
    zip_sql = ""
    if zip_code:
        zip_sql = " AND zip5 = ?"
        params.append(zip_code.strip()[:5])
    row = db.fetch_one(f"SELECT * FROM addresses WHERE norm_address = ?{zip_sql} ORDER BY id LIMIT 1", tuple(params))
    return _row_to_address(row) if row else None


def fetch_property(db: GraphDatabase, property_id: int) -> Property | None:
    row = db.fetch_one("SELECT * FROM property_entities WHERE id = ?", (property_id,))
    return _row_to_property(row) if row else None


def fetch_property_by_source_row(db: GraphDatabase, source: str, rowid: int) -> Property | None:
    row = db.fetch_one("SELECT * FROM property_entities WHERE source = ? AND source_rowid = ?", (source, rowid))
    return _row_to_property(row) if row else None


def fetch_organization(db: GraphDatabase, organization_id: int) -> Organization | None:
    row = db.fetch_one("SELECT * FROM organization_entities WHERE id = ?", (organization_id,))
    return _row_to_organization(row) if row else None


def fetch_contact(db: GraphDatabase, contact_id: int) -> Contact | None:
    row = db.fetch_one("SELECT * FROM contact_entities WHERE id = ?", (contact_id,))
    return _row_to_contact(row) if row else None


def fetch_vehicle(db: GraphDatabase, vehicle_id: int) -> Vehicle | None:
    row = db.fetch_one("SELECT * FROM vehicle_entities WHERE id = ?", (vehicle_id,))
    return _row_to_vehicle(row) if row else None


def fetch_source_record(db: GraphDatabase, source: str, rowid: int) -> SourceRecord | None:
    try:
        row = db.fetch_one(f"SELECT rowid, * FROM {quote(source)} WHERE rowid = ?", (rowid,))
    except Exception:
        return None
    return _source_record(row_to_record_node(source, row)) if row else None


def fetch_addresses(
    db: GraphDatabase,
    *,
    where: Any | None = None,
    order_by: list[OrderByInput] | None = None,
    limit: int | None = None,
    offset: int | None = None,
) -> AddressConnection:
    address_schema = address_filter_schema()
    where_sql, where_values = build_where_clause(address_schema, where)
    order_sql = build_order_clause(address_schema, order_by)
    total = int(db.fetch_value(f"SELECT COUNT(*) FROM addresses{where_sql}", tuple(where_values)))
    rows = db.fetch_all(
        f"SELECT * FROM addresses{where_sql}{order_sql} LIMIT ? OFFSET ?",
        tuple(where_values) + (clamp_limit(limit), clamp_offset(offset)),
    )
    return AddressConnection(page_info=_page(total, limit, offset), nodes=[_row_to_address(row) for row in rows])


def search_persons(db: GraphDatabase, query: str, limit: int | None = None, offset: int | None = None) -> PersonSearchConnection:
    normalized = normalize_text(query)
    like = f"%{'%'.join(normalized.split())}%"
    key = "|".join(normalized.split())
    actual_limit = clamp_limit(limit)
    actual_offset = clamp_offset(offset)
    rows = db.fetch_all(
        """
        SELECT *
        FROM name_observations
        WHERE norm_name_key LIKE ? OR lower(full_name) LIKE ?
        ORDER BY CASE WHEN norm_name_key = ? THEN 0 ELSE 1 END, confidence DESC, id
        LIMIT ? OFFSET ?
        """,
        (like, like, key, actual_limit + 1, actual_offset),
    )
    has_more = len(rows) > actual_limit
    rows = rows[:actual_limit]
    total_count = actual_offset + len(rows) + (1 if has_more else 0)
    observations = [_row_to_name_observation(row) for row in rows]
    persons_by_id = fetch_persons_by_ids(db, [obs.person_id for obs in observations if obs.person_id])
    nodes = []
    for observation in observations:
        person = persons_by_id.get(observation.person_id) if observation.person_id else None
        exact = observation.norm_name_key == key
        nodes.append(PersonSearchResult(match_score=1.0 if exact else float(observation.confidence) * 0.75, matched_fields=["name"], person=person, name_observation=observation))
    return PersonSearchConnection(page_info=_page_overfetch(total_count, has_more, limit, offset), nodes=nodes)


def search_addresses(db: GraphDatabase, query: str, zip_code: str | None = None, limit: int | None = None, offset: int | None = None) -> AddressSearchConnection:
    normalized = normalize_address(query)
    params: list[Any] = [f"%{normalized}%"]
    zip_sql = ""
    if zip_code:
        zip_sql = " AND zip5 = ?"
        params.append(zip_code.strip()[:5])
    actual_limit = clamp_limit(limit)
    actual_offset = clamp_offset(offset)
    rows = db.fetch_all(
        f"""
        SELECT addresses.*, COUNT(address_edges.id) AS relation_count
        FROM addresses
        LEFT JOIN address_edges ON address_edges.address_id = addresses.id
        WHERE addresses.norm_address LIKE ?{zip_sql}
        GROUP BY addresses.id
        ORDER BY CASE WHEN addresses.norm_address = ? THEN 0 ELSE 1 END, relation_count DESC, addresses.id
        LIMIT ? OFFSET ?
        """,
        tuple(params) + (normalized, actual_limit + 1, actual_offset),
    )
    has_more = len(rows) > actual_limit
    rows = rows[:actual_limit]
    total_count = actual_offset + len(rows) + (1 if has_more else 0)
    nodes = [
        AddressSearchResult(
            match_score=1.0 if row["norm_address"] == normalized else 0.75,
            matched_fields=["address"],
            address=_row_to_address(row),
            relation_count=int(row["relation_count"]),
        )
        for row in rows
    ]
    return AddressSearchConnection(page_info=_page_overfetch(total_count, has_more, limit, offset), nodes=nodes)


def fetch_records(
    db: GraphDatabase,
    schema: TableSchema,
    *,
    where: Any | None = None,
    order_by: list[OrderByInput] | None = None,
    limit: int | None = None,
    offset: int | None = None,
    extra_where: str = "",
    extra_params: tuple[Any, ...] = (),
) -> tuple[list[RecordNode], int]:
    where_sql, where_values = build_where_clause(schema, where)
    order_sql = build_order_clause(schema, order_by)
    if extra_where:
        where_sql = f"{where_sql} AND ({extra_where})" if where_sql else f" WHERE ({extra_where})"
    total = int(db.fetch_value(f"SELECT COUNT(*) FROM {quote(schema.name)}{where_sql}", tuple(where_values) + extra_params))
    rows = db.fetch_all(
        f"SELECT rowid, * FROM {quote(schema.name)}{where_sql}{order_sql} LIMIT ? OFFSET ?",
        tuple(where_values) + extra_params + (clamp_limit(limit), clamp_offset(offset)),
    )
    return [row_to_record_node(schema.name, row) for row in rows], total


def fetch_record_by_id(db: GraphDatabase, schema: TableSchema, id_value: str) -> RecordNode | None:
    row = db.fetch_one(f"SELECT rowid, * FROM {quote(schema.name)} WHERE id = ?", (id_value,))
    return row_to_record_node(schema.name, row) if row else None


def address_filter_schema() -> TableSchema:
    return TableSchema(
        name="addresses",
        graphql_type="Address",
        collection_field="addresses",
        singular_field="address",
        columns=("norm_address", "zip5", "street_number", "street_name", "unit", "city", "state", "county"),
        norm_columns=(),
        filter_columns=("norm_address", "zip5", "street_number", "street_name", "unit", "city", "state", "county"),
        id_linked=False,
    )


def build_runtime_types(registry: SchemaRegistry) -> dict[str, Any]:
    where_input_by_table = {schema.name: make_where_input(schema) for schema in registry.source_tables()}
    record_type_by_table: dict[str, type] = {}
    for schema in registry.source_tables():
        record_type_by_table[schema.name] = make_record_type(registry, schema, record_type_by_table, with_relations=False)
    for schema in registry.source_tables():
        record_type_by_table[schema.name] = make_record_type(registry, schema, record_type_by_table, with_relations=True)
    return {
        "record_type_by_table": record_type_by_table,
        "connection_type_by_table": {schema.name: make_record_connection_type(schema, record_type_by_table[schema.name]) for schema in registry.source_tables()},
        "where_input_by_table": where_input_by_table,
        "address_where_input": make_where_input(address_filter_schema()),
    }


def make_where_input(schema: TableSchema) -> type:
    fields = [(column, StringFilterInput | None, dataclasses.field(default=None)) for column in schema.filter_columns]
    return strawberry.input(dataclasses.make_dataclass(f"{schema.graphql_type}WhereInput", fields))


def make_record_connection_type(schema: TableSchema, record_type: type) -> type:
    return _connection_type(f"{schema.graphql_type}Connection", record_type)


def make_norm_type(schema: TableSchema) -> tuple[type, Any]:
    norm_names = [column.removeprefix("__norm_") for column in schema.norm_columns if column.startswith("__norm_")]
    fields = [(name, str | None, dataclasses.field(default=None)) for name in norm_names]
    norm_cls = strawberry.type(dataclasses.make_dataclass(f"{schema.graphql_type}Norm", fields))

    def norm_resolver(root: RecordNode) -> Any:
        return norm_cls(**{column.removeprefix("__norm_"): (root.data.get(column, "") or None) for column in schema.norm_columns if column.startswith("__norm_")})

    return norm_cls, norm_resolver


def make_record_type(registry: SchemaRegistry, schema: TableSchema, record_type_by_table: dict[str, type], *, with_relations: bool) -> type:
    norm_cls, norm_resolver = make_norm_type(schema)
    annotations: dict[str, Any] = {"rowid": int, "norm": norm_cls}
    namespace: dict[str, Any] = {"__annotations__": annotations, "norm": strawberry.field(resolver=norm_resolver)}
    for column in schema.columns:
        def make_column_resolver(column_name: str):
            def resolve(root: RecordNode) -> str | None:
                return root.data.get(column_name, "") or None
            return resolve
        annotations[column] = str | None
        namespace[column] = strawberry.field(resolver=make_column_resolver(column))
    if with_relations and schema.name != "base" and schema.id_linked:
        annotations["person"] = Person | None
        namespace["person"] = strawberry.field(resolver=_make_record_person_resolver())
    if with_relations:
        for role, field_name in ADDRESS_ROLES_BY_SOURCE.get(schema.name, {}).items():
            annotations[field_name] = AddressConnection
            namespace[field_name] = strawberry.field(resolver=_make_record_addresses_resolver(role))
    return strawberry.type(type(schema.graphql_type, (), namespace))


def _make_record_addresses_resolver(role: str):
    def resolve(root: RecordNode, info: Info, limit: int | None = None, offset: int | None = None) -> AddressConnection:
        return _addresses_for_record(info.context.db, root.table, root.rowid, role, limit, offset)
    resolve.__annotations__ = {"root": RecordNode, "info": Info, "limit": int | None, "offset": int | None, "return": AddressConnection}
    return resolve


def _make_record_person_resolver():
    def resolve(root: RecordNode, info: Info) -> Person | None:
        person_id = root.data.get("id", "")
        return fetch_person(info.context.db, person_id) if person_id else None

    resolve.__annotations__ = {"root": RecordNode, "info": Info, "return": Person | None}
    return resolve


def _records_for_person_connection(info: Info, source: str, person_id: str, limit: int | None, offset: int | None) -> Any:
    schema = info.context.registry.get(source)
    nodes, total = fetch_records(info.context.db, schema, extra_where="id = ?", extra_params=(person_id,), limit=limit, offset=offset)
    return SourceRecordConnection(page_info=_page(total, limit, offset), nodes=[_source_record(node) for node in nodes])


def _records_for_address_connection(info: Info, source: str, role: str, address_id: int, limit: int | None, offset: int | None) -> Any:
    total = int(info.context.db.fetch_value("SELECT COUNT(*) FROM address_edges WHERE address_id = ? AND source = ? AND role = ?", (address_id, source, role)))
    rows = info.context.db.fetch_all(
        f"""
        SELECT {quote(source)}.rowid, {quote(source)}.*
        FROM {quote(source)}
        JOIN address_edges
          ON address_edges.source = ?
         AND address_edges.source_rowid = {quote(source)}.rowid
         AND address_edges.role = ?
        WHERE address_edges.address_id = ?
        LIMIT ? OFFSET ?
        """,
        (source, role, address_id, clamp_limit(limit), clamp_offset(offset)),
    )
    return SourceRecordConnection(page_info=_page(total, limit, offset), nodes=[_source_record(row_to_record_node(source, row)) for row in rows])


def _source_record(node: RecordNode) -> SourceRecord:
    return SourceRecord(
        source=_source_enum(node.table),
        table=node.table,
        rowid=node.rowid,
        record_id=node.data.get("id") or node.data.get(f"{node.table}_id") or node.data.get("record_id") or None,
        summary=_source_summary(node.table, node.data),
        data=node.data,
    )


def _row_to_person(row: Any) -> Person:
    return Person(id=row["id"], base_rowid=int(row["base_rowid"]), firstname=row["firstname"] or None, middlename=row["middlename"] or None, lastname=row["lastname"] or None, full_name=row["full_name"] or None, norm_name_key=row["norm_name_key"] or None, primary_address_id=int(row["primary_address_id"]) if row["primary_address_id"] else None)


def _row_to_address(row: Any) -> Address:
    return Address(id=int(row["id"]), norm_address=row["norm_address"], zip5=row["zip5"], street_number=row["street_number"] or None, street_name=row["street_name"] or None, unit=row["unit"] or None, city=row["city"] or None, state=row["state"] or None, county=row["county"] or None)


def _row_to_name_observation(row: Any) -> NameObservation:
    return NameObservation(id=int(row["id"]), person_id=row["person_id"] or None, full_name=row["full_name"] or None, firstname=row["firstname"] or None, middlename=row["middlename"] or None, lastname=row["lastname"] or None, norm_name_key=row["norm_name_key"] or None, source=row["source"], source_rowid=int(row["source_rowid"]), confidence=float(row["confidence"]))


def _row_to_contact(row: Any) -> Contact:
    return Contact(id=int(row["id"]), contact_type=row["contact_type"], value=row["value"])


def _row_to_vehicle(row: Any) -> Vehicle:
    return Vehicle(id=int(row["id"]), vin=row["vin"])


def _row_to_property(row: Any) -> Property:
    return Property(
        id=int(row["id"]),
        property_key=row["property_key"],
        source=_source_enum(row["source"]),
        source_rowid=int(row["source_rowid"]),
    )


def _row_to_organization(row: Any) -> Organization:
    return Organization(id=int(row["id"]), name=row["name"], organization_type=row["org_type"])


def _row_to_edge(row: Any) -> EntityEdge:
    return EntityEdge(id=int(row["id"]), from_type=row["from_type"], from_id=row["from_id"], to_type=row["to_type"], to_id=row["to_id"], relation_type=row["relation_type"], source=row["source"], source_rowid=int(row["source_rowid"]), confidence=float(row["confidence"]), evidence_role=row["evidence_role"])


def _source_enum(source: str) -> Source:
    try:
        return Source(source)
    except ValueError:
        return Source.BASE


def _source_value(source: Source | None) -> str | None:
    return source.value if isinstance(source, Source) else None


def _address_relation_value(role: AddressAssociationRole | None) -> str | None:
    return role.value if isinstance(role, AddressAssociationRole) else None


def _address_edge_role_value(role: AddressAssociationRole | None) -> str | None:
    if role is None:
        return None
    return {
        AddressAssociationRole.RESIDENCE: "residence",
        AddressAssociationRole.SERVICE_ADDRESS: "service",
        AddressAssociationRole.LICENSE_ADDRESS: "license",
        AddressAssociationRole.REGISTRATION_ADDRESS: "registration",
        AddressAssociationRole.MAILING_ADDRESS: "owner_mailing",
        AddressAssociationRole.RECORD_ADDRESS: "record",
        AddressAssociationRole.TRACE_ADDRESS: "residence",
    }[role]


def _address_assoc_role_from_relation(relation_type: str, source: str) -> AddressAssociationRole:
    if source == "trace" and relation_type == "RESIDENCE":
        return AddressAssociationRole.TRACE_ADDRESS
    return {
        "RESIDENCE": AddressAssociationRole.RESIDENCE,
        "SERVICE": AddressAssociationRole.SERVICE_ADDRESS,
        "LICENSE": AddressAssociationRole.LICENSE_ADDRESS,
        "REGISTRATION": AddressAssociationRole.REGISTRATION_ADDRESS,
        "OWNER_MAILING": AddressAssociationRole.MAILING_ADDRESS,
        "RECORD": AddressAssociationRole.RECORD_ADDRESS,
        "PROPERTY": AddressAssociationRole.RECORD_ADDRESS,
    }.get(relation_type, AddressAssociationRole.RECORD_ADDRESS)


def _property_address_role_value(role: PropertyAddressRole | None) -> str | None:
    return role.value if isinstance(role, PropertyAddressRole) else None


def _property_person_role_value(role: PropertyPersonRole | None) -> str | None:
    return role.value if isinstance(role, PropertyPersonRole) else None


def _property_organization_role_value(role: PropertyOrganizationRole | None) -> str | None:
    return role.value if isinstance(role, PropertyOrganizationRole) else None


def _organization_person_role_value(role: OrganizationPersonRole | None) -> str | None:
    return role.value if isinstance(role, OrganizationPersonRole) else None


def _source_summary(source: str, data: dict[str, str]) -> str:
    keys_by_source = {
        "tax": ["ownername", "ownercompany", "address", "owneraddressline1", "recordingdate", "lendername", "totalliencount", "ownerrescount"],
        "base": ["firstname", "lastname", "primaryaddress", "zip", "homeownerprobabilitymodel", "lengthofresidence"],
        "utility": ["first_name", "last_name", "address", "zip", "phone"],
        "trace": ["firstname", "lastname", "address", "zip", "dob_year"],
        "drive": ["firstname", "lastname", "address", "zip", "dl_state"],
        "voter": ["firstname", "lastname", "address", "zip", "gender"],
        "auto": ["firstname", "lastname", "address", "zip", "vin", "make", "model"],
        "loan": ["firstname", "lastname", "address", "zip", "own_rent", "loan_amount", "employer"],
        "criminal": ["firstname", "lastname", "address", "zip", "category", "offensedesc1"],
        "linkedin": ["firstname", "lastname", "linkedinurl", "position_companyname", "position_current"],
    }
    parts = [source]
    for key in keys_by_source.get(source, []):
        value = str(data.get(key) or "").strip()
        if value:
            parts.append(f"{key}={value}")
    if len(parts) == 1:
        for key, value in data.items():
            text = str(value or "").strip()
            if text:
                parts.append(f"{key}={text}")
            if len(parts) >= 6:
                break
    return "; ".join(parts)


def _paged_connection(connection_type: type, nodes: list[Any], limit: int | None, offset: int | None) -> Any:
    actual_offset = clamp_offset(offset)
    actual_limit = clamp_limit(limit)
    page_nodes = nodes[actual_offset : actual_offset + actual_limit]
    return connection_type(page_info=_page(len(nodes), limit, offset), nodes=page_nodes)


def _name_observations_for_person(db: GraphDatabase, person_id: str, limit: int | None, offset: int | None) -> tuple[list[NameObservation], int]:
    total = int(db.fetch_value("SELECT COUNT(*) FROM name_observations WHERE person_id = ?", (person_id,)))
    rows = db.fetch_all("SELECT * FROM name_observations WHERE person_id = ? ORDER BY confidence DESC, id LIMIT ? OFFSET ?", (person_id, clamp_limit(limit), clamp_offset(offset)))
    return [_row_to_name_observation(row) for row in rows], total


def _addresses_for_person(db: GraphDatabase, person_id: str, relation_types: list[str] | None, limit: int | None, offset: int | None) -> AddressConnection:
    where, params = _edge_filter("from_type = 'person' AND from_id = ? AND to_type = 'address'", [person_id], relation_types)
    total = int(db.fetch_value(f"SELECT COUNT(DISTINCT to_id) FROM entity_edges WHERE {where}", tuple(params)))
    rows = db.fetch_all(
        f"""
        SELECT DISTINCT addresses.*
        FROM entity_edges
        JOIN addresses ON addresses.id = CAST(entity_edges.to_id AS INTEGER)
        WHERE {where}
        ORDER BY addresses.id
        LIMIT ? OFFSET ?
        """,
        tuple(params) + (clamp_limit(limit), clamp_offset(offset)),
    )
    return AddressConnection(page_info=_page(total, limit, offset), nodes=[_row_to_address(row) for row in rows])


def _people_for_address(db: GraphDatabase, address_id: int, relation_types: list[str] | None, limit: int | None, offset: int | None) -> PersonConnection:
    where, params = _edge_filter("from_type = 'person' AND to_type = 'address' AND to_id = ?", [str(address_id)], relation_types)
    total = int(db.fetch_value(f"SELECT COUNT(DISTINCT from_id) FROM entity_edges WHERE {where}", tuple(params)))
    rows = db.fetch_all(
        f"""
        SELECT DISTINCT person_entities.*
        FROM entity_edges
        JOIN person_entities ON person_entities.id = entity_edges.from_id
        WHERE {where}
        ORDER BY person_entities.id
        LIMIT ? OFFSET ?
        """,
        tuple(params) + (clamp_limit(limit), clamp_offset(offset)),
    )
    return PersonConnection(page_info=_page(total, limit, offset), nodes=[_row_to_person(row) for row in rows])


def _contacts_for_person(db: GraphDatabase, person_id: str, limit: int | None, offset: int | None) -> ContactConnection:
    total = int(db.fetch_value("SELECT COUNT(DISTINCT to_id) FROM entity_edges WHERE from_type = 'person' AND from_id = ? AND to_type = 'contact'", (person_id,)))
    rows = db.fetch_all(
        """
        SELECT DISTINCT contact_entities.*
        FROM entity_edges
        JOIN contact_entities ON contact_entities.id = CAST(entity_edges.to_id AS INTEGER)
        WHERE entity_edges.from_type = 'person' AND entity_edges.from_id = ? AND entity_edges.to_type = 'contact'
        ORDER BY contact_entities.id
        LIMIT ? OFFSET ?
        """,
        (person_id, clamp_limit(limit), clamp_offset(offset)),
    )
    return ContactConnection(page_info=_page(total, limit, offset), nodes=[_row_to_contact(row) for row in rows])


def _vehicles_for_person(db: GraphDatabase, person_id: str, limit: int | None, offset: int | None) -> VehicleConnection:
    total = int(db.fetch_value("SELECT COUNT(DISTINCT to_id) FROM entity_edges WHERE from_type = 'person' AND from_id = ? AND to_type = 'vehicle'", (person_id,)))
    rows = db.fetch_all(
        """
        SELECT DISTINCT vehicle_entities.*
        FROM entity_edges
        JOIN vehicle_entities ON vehicle_entities.id = CAST(entity_edges.to_id AS INTEGER)
        WHERE entity_edges.from_type = 'person' AND entity_edges.from_id = ? AND entity_edges.to_type = 'vehicle'
        ORDER BY vehicle_entities.id
        LIMIT ? OFFSET ?
        """,
        (person_id, clamp_limit(limit), clamp_offset(offset)),
    )
    return VehicleConnection(page_info=_page(total, limit, offset), nodes=[_row_to_vehicle(row) for row in rows])


def _edges_for_person(db: GraphDatabase, person_id: str, relation_types: list[str] | None, limit: int | None, offset: int | None) -> EntityEdgeConnection:
    where, params = _edge_filter("from_type = 'person' AND from_id = ?", [person_id], relation_types)
    total = int(db.fetch_value(f"SELECT COUNT(*) FROM entity_edges WHERE {where}", tuple(params)))
    rows = db.fetch_all(f"SELECT * FROM entity_edges WHERE {where} ORDER BY id LIMIT ? OFFSET ?", tuple(params) + (clamp_limit(limit), clamp_offset(offset)))
    return EntityEdgeConnection(page_info=_page(total, limit, offset), nodes=[_row_to_edge(row) for row in rows])


def _person_address_associations_for_address(
    db: GraphDatabase,
    address_id: int,
    *,
    source: str | None,
    role: str | None,
    limit: int | None,
    offset: int | None,
) -> PersonAddressAssociationConnection:
    clauses = ["from_type = 'person'", "to_type = 'address'", "to_id = ?"]
    params: list[Any] = [str(address_id)]
    if source:
        clauses.append("source = ?")
        params.append(source)
    if role:
        if role == "TRACE":
            clauses.append("source = 'trace'")
            clauses.append("relation_type = 'RESIDENCE'")
        else:
            clauses.append("relation_type = ?")
            params.append(role)
    where = " AND ".join(clauses)
    total = int(db.fetch_value(f"SELECT COUNT(*) FROM entity_edges WHERE {where}", tuple(params)))
    rows = db.fetch_all(
        f"SELECT * FROM entity_edges WHERE {where} ORDER BY id LIMIT ? OFFSET ?",
        tuple(params) + (clamp_limit(limit), clamp_offset(offset)),
    )
    nodes = [
        PersonAddressAssociation(
            id=int(row["id"]),
            person_id=row["from_id"],
            address_id=int(row["to_id"]),
            source=_source_enum(row["source"]),
            role=_address_assoc_role_from_relation(row["relation_type"], row["source"]),
            source_rowid=int(row["source_rowid"]),
            confidence=float(row["confidence"]),
        )
        for row in rows
    ]
    return PersonAddressAssociationConnection(page_info=_page(total, limit, offset), nodes=nodes)


def _person_address_associations_for_person(
    db: GraphDatabase,
    person_id: str,
    *,
    source: str | None,
    role: str | None,
    limit: int | None,
    offset: int | None,
) -> PersonAddressAssociationConnection:
    clauses = ["from_type = 'person'", "from_id = ?", "to_type = 'address'"]
    params: list[Any] = [person_id]
    if source:
        clauses.append("source = ?")
        params.append(source)
    if role:
        if role == "TRACE":
            clauses.append("source = 'trace'")
            clauses.append("relation_type = 'RESIDENCE'")
        else:
            clauses.append("relation_type = ?")
            params.append(role)
    where = " AND ".join(clauses)
    total = int(db.fetch_value(f"SELECT COUNT(*) FROM entity_edges WHERE {where}", tuple(params)))
    rows = db.fetch_all(
        f"SELECT * FROM entity_edges WHERE {where} ORDER BY id LIMIT ? OFFSET ?",
        tuple(params) + (clamp_limit(limit), clamp_offset(offset)),
    )
    nodes = [
        PersonAddressAssociation(
            id=int(row["id"]),
            person_id=row["from_id"],
            address_id=int(row["to_id"]),
            source=_source_enum(row["source"]),
            role=_address_assoc_role_from_relation(row["relation_type"], row["source"]),
            source_rowid=int(row["source_rowid"]),
            confidence=float(row["confidence"]),
        )
        for row in rows
    ]
    return PersonAddressAssociationConnection(page_info=_page(total, limit, offset), nodes=nodes)


def _source_records_for_address(
    db: GraphDatabase,
    address_id: int,
    *,
    source: str | None,
    role: str | None,
    limit: int | None,
    offset: int | None,
) -> SourceRecordConnection:
    clauses = ["address_id = ?"]
    params: list[Any] = [address_id]
    if source:
        clauses.append("source = ?")
        params.append(source)
    if role:
        clauses.append("role = ?")
        params.append(role)
    rows = db.fetch_all(
        f"SELECT source, source_rowid FROM address_edges WHERE {' AND '.join(clauses)} ORDER BY id",
        tuple(params),
    )
    nodes = [
        record
        for row in rows
        if (record := fetch_source_record(db, row["source"], int(row["source_rowid"]))) is not None
    ]
    return _paged_connection(SourceRecordConnection, nodes, limit, offset)


def _all_source_records_for_person(db: GraphDatabase, person_id: str, *, limit: int | None, offset: int | None) -> SourceRecordConnection:
    rows = db.fetch_all(
        """
        SELECT DISTINCT source, source_rowid
        FROM entity_edges
        WHERE from_type = 'person' AND from_id = ?
        ORDER BY source, source_rowid
        """,
        (person_id,),
    )
    nodes = [
        record
        for row in rows
        if (record := fetch_source_record(db, row["source"], int(row["source_rowid"]))) is not None
    ]
    return _paged_connection(SourceRecordConnection, nodes, limit, offset)


def _property_address_associations_for_address(
    db: GraphDatabase,
    address_id: int,
    *,
    source: str | None,
    role: str | None,
    limit: int | None,
    offset: int | None,
) -> PropertyAddressAssociationConnection:
    clauses = ["address_edges.address_id = ?", "property_entities.source = address_edges.source", "property_entities.source_rowid = address_edges.source_rowid"]
    params: list[Any] = [address_id]
    if source:
        clauses.append("address_edges.source = ?")
        params.append(source)
    if role:
        clauses.append("address_edges.role = ?")
        params.append(role)
    else:
        clauses.append("address_edges.role IN ('property', 'owner_mailing')")
    where = " AND ".join(clauses)
    total = int(db.fetch_value(f"SELECT COUNT(*) FROM address_edges JOIN property_entities ON {where}", tuple(params)))
    rows = db.fetch_all(
        f"""
        SELECT address_edges.*, property_entities.id AS property_id
        FROM address_edges
        JOIN property_entities ON {where}
        ORDER BY address_edges.id
        LIMIT ? OFFSET ?
        """,
        tuple(params) + (clamp_limit(limit), clamp_offset(offset)),
    )
    nodes = [
        PropertyAddressAssociation(
            id=int(row["id"]),
            property_id=int(row["property_id"]),
            address_id=int(row["address_id"]),
            source=_source_enum(row["source"]),
            role=PropertyAddressRole.SITUS_ADDRESS if row["role"] == "property" else PropertyAddressRole.MAILING_ADDRESS,
            source_rowid=int(row["source_rowid"]),
        )
        for row in rows
    ]
    return PropertyAddressAssociationConnection(page_info=_page(total, limit, offset), nodes=nodes)


def _property_address_associations_for_property(
    db: GraphDatabase,
    property: Property,
    *,
    role: str | None,
    limit: int | None,
    offset: int | None,
) -> PropertyAddressAssociationConnection:
    clauses = ["source = ?", "source_rowid = ?"]
    params: list[Any] = [property.source.value, property.source_rowid]
    if role:
        clauses.append("role = ?")
        params.append(role)
    else:
        clauses.append("role IN ('property', 'owner_mailing')")
    where = " AND ".join(clauses)
    total = int(db.fetch_value(f"SELECT COUNT(*) FROM address_edges WHERE {where}", tuple(params)))
    rows = db.fetch_all(
        f"SELECT * FROM address_edges WHERE {where} ORDER BY id LIMIT ? OFFSET ?",
        tuple(params) + (clamp_limit(limit), clamp_offset(offset)),
    )
    nodes = [
        PropertyAddressAssociation(
            id=int(row["id"]),
            property_id=property.id,
            address_id=int(row["address_id"]),
            source=_source_enum(row["source"]),
            role=PropertyAddressRole.SITUS_ADDRESS if row["role"] == "property" else PropertyAddressRole.MAILING_ADDRESS,
            source_rowid=int(row["source_rowid"]),
        )
        for row in rows
    ]
    return PropertyAddressAssociationConnection(page_info=_page(total, limit, offset), nodes=nodes)


def _property_person_associations_for_property(
    db: GraphDatabase,
    property: Property,
    *,
    role: str | None,
    limit: int | None,
    offset: int | None,
) -> PropertyPersonAssociationConnection:
    del role
    row = db.fetch_one("SELECT rowid, * FROM tax WHERE rowid = ?", (property.source_rowid,))
    if row is None:
        return PropertyPersonAssociationConnection(page_info=_page(0, limit, offset), nodes=[])
    node = PropertyPersonAssociation(
        id=property.id,
        property_id=property.id,
        source=Source.TAX,
        role=PropertyPersonRole.OWNER,
        source_rowid=property.source_rowid,
        person_id=row["id"] or None,
        display_name=row["ownername"] or " ".join(part for part in (row["firstname"], row["lastname"]) if part) or None,
        confidence=1.0 if row["id"] else 0.65,
        observed_date=row["recordingdate"] or None,
    )
    return _paged_connection(PropertyPersonAssociationConnection, [node], limit, offset)


def _property_person_associations_for_person(
    db: GraphDatabase,
    person_id: str,
    *,
    source: str | None,
    role: str | None,
    limit: int | None,
    offset: int | None,
) -> PropertyPersonAssociationConnection:
    del role
    source_clause = "AND property_entities.source = ?" if source else ""
    params: tuple[Any, ...] = (person_id, source) if source else (person_id,)
    rows = db.fetch_all(
        f"""
        SELECT property_entities.id AS property_id, property_entities.source_rowid, tax.*
        FROM property_entities
        JOIN tax ON tax.rowid = property_entities.source_rowid
        WHERE tax.id = ? {source_clause}
        ORDER BY property_entities.id
        """,
        params,
    )
    nodes = [
        PropertyPersonAssociation(
            id=int(row["property_id"]),
            property_id=int(row["property_id"]),
            source=Source.TAX,
            role=PropertyPersonRole.OWNER,
            source_rowid=int(row["source_rowid"]),
            person_id=row["id"] or None,
            display_name=row["ownername"] or None,
            confidence=1.0,
            observed_date=row["recordingdate"] or None,
        )
        for row in rows
    ]
    return _paged_connection(PropertyPersonAssociationConnection, nodes, limit, offset)


def _property_organization_associations_for_property(
    db: GraphDatabase,
    property: Property,
    *,
    role: str | None,
    limit: int | None,
    offset: int | None,
) -> PropertyOrganizationAssociationConnection:
    row = db.fetch_one("SELECT rowid, * FROM tax WHERE rowid = ?", (property.source_rowid,))
    if row is None:
        return PropertyOrganizationAssociationConnection(page_info=_page(0, limit, offset), nodes=[])
    specs = [
        ("ownercompany", "owner_company", PropertyOrganizationRole.OWNER_ENTITY),
        ("lendername", "lender", PropertyOrganizationRole.LENDER),
    ]
    nodes: list[PropertyOrganizationAssociation] = []
    for column, org_type, assoc_role in specs:
        if role and role != assoc_role.value:
            continue
        value = (row[column] or "").strip()
        if not value:
            continue
        org_row = db.fetch_one("SELECT * FROM organization_entities WHERE norm_name = ? AND org_type = ?", (value.lower(), org_type))
        if org_row:
            nodes.append(
                PropertyOrganizationAssociation(
                    id=len(nodes) + 1,
                    property_id=property.id,
                    organization_id=int(org_row["id"]),
                    source=Source.TAX,
                    role=assoc_role,
                    source_rowid=property.source_rowid,
                    observed_date=row["recordingdate"] or None,
                )
            )
    return _paged_connection(PropertyOrganizationAssociationConnection, nodes, limit, offset)


def _property_organization_associations_for_organization(
    db: GraphDatabase,
    organization_id: int,
    *,
    role: str | None,
    limit: int | None,
    offset: int | None,
) -> PropertyOrganizationAssociationConnection:
    org = fetch_organization(db, organization_id)
    if org is None:
        return PropertyOrganizationAssociationConnection(page_info=_page(0, limit, offset), nodes=[])
    column = "ownercompany" if org.organization_type == "owner_company" else "lendername" if org.organization_type == "lender" else None
    assoc_role = PropertyOrganizationRole.OWNER_ENTITY if org.organization_type == "owner_company" else PropertyOrganizationRole.LENDER
    if column is None or (role and role != assoc_role.value):
        return PropertyOrganizationAssociationConnection(page_info=_page(0, limit, offset), nodes=[])
    rows = db.fetch_all(
        f"""
        SELECT property_entities.id AS property_id, property_entities.source_rowid, tax.*
        FROM property_entities
        JOIN tax ON tax.rowid = property_entities.source_rowid
        WHERE lower(trim(tax.{quote(column)})) = ?
        ORDER BY property_entities.id
        """,
        (org.name.lower().strip(),),
    )
    nodes = [
        PropertyOrganizationAssociation(
            id=i + 1,
            property_id=int(row["property_id"]),
            organization_id=organization_id,
            source=Source.TAX,
            role=assoc_role,
            source_rowid=int(row["source_rowid"]),
            observed_date=row["recordingdate"] or None,
        )
        for i, row in enumerate(rows)
    ]
    return _paged_connection(PropertyOrganizationAssociationConnection, nodes, limit, offset)


def _person_organization_associations(
    db: GraphDatabase,
    person_id: str,
    *,
    source: str | None,
    role: str | None,
    limit: int | None,
    offset: int | None,
) -> PersonOrganizationAssociationConnection:
    specs = [
        ("loan", "employer", "employer", OrganizationPersonRole.EMPLOYER),
        ("linkedin", "position_companyname", "employer", OrganizationPersonRole.EMPLOYER),
    ]
    nodes: list[PersonOrganizationAssociation] = []
    for table, column, org_type, assoc_role in specs:
        if source and source != table:
            continue
        if role and role != assoc_role.value:
            continue
        if column not in db.table_columns(table):
            continue
        rows = db.fetch_all(
            f"""
            SELECT {quote(table)}.rowid AS source_rowid, organization_entities.id AS organization_id
            FROM {quote(table)}
            JOIN organization_entities
              ON organization_entities.norm_name = lower(trim({quote(table)}.{quote(column)}))
             AND organization_entities.org_type = ?
            WHERE {quote(table)}.id = ? AND trim({quote(table)}.{quote(column)}) != ''
            ORDER BY {quote(table)}.rowid
            """,
            (org_type, person_id),
        )
        for row in rows:
            nodes.append(
                PersonOrganizationAssociation(
                    id=len(nodes) + 1,
                    person_id=person_id,
                    organization_id=int(row["organization_id"]),
                    source=_source_enum(table),
                    role=assoc_role,
                    source_rowid=int(row["source_rowid"]),
                )
            )
    return _paged_connection(PersonOrganizationAssociationConnection, nodes, limit, offset)


def _organization_person_associations(
    db: GraphDatabase,
    organization_id: int,
    *,
    role: str | None,
    limit: int | None,
    offset: int | None,
) -> PersonOrganizationAssociationConnection:
    org = fetch_organization(db, organization_id)
    if org is None:
        return PersonOrganizationAssociationConnection(page_info=_page(0, limit, offset), nodes=[])
    nodes: list[PersonOrganizationAssociation] = []
    specs = [
        ("loan", "employer", OrganizationPersonRole.EMPLOYER),
        ("linkedin", "position_companyname", OrganizationPersonRole.EMPLOYER),
    ]
    for table, column, assoc_role in specs:
        if role and role != assoc_role.value:
            continue
        if column not in db.table_columns(table):
            continue
        rows = db.fetch_all(
            f"""
            SELECT rowid AS source_rowid, id AS person_id
            FROM {quote(table)}
            WHERE lower(trim({quote(column)})) = ? AND trim(id) != ''
            ORDER BY rowid
            """,
            (org.name.lower().strip(),),
        )
        for row in rows:
            nodes.append(
                PersonOrganizationAssociation(
                    id=len(nodes) + 1,
                    person_id=row["person_id"],
                    organization_id=organization_id,
                    source=_source_enum(table),
                    role=assoc_role,
                    source_rowid=int(row["source_rowid"]),
                )
            )
    return _paged_connection(PersonOrganizationAssociationConnection, nodes, limit, offset)


def _person_contact_associations(db: GraphDatabase, person_id: str, *, source: str | None, limit: int | None, offset: int | None) -> PersonContactAssociationConnection:
    clauses = ["from_type = 'person'", "from_id = ?", "to_type = 'contact'"]
    params: list[Any] = [person_id]
    if source:
        clauses.append("source = ?")
        params.append(source)
    rows = db.fetch_all(f"SELECT * FROM entity_edges WHERE {' AND '.join(clauses)} ORDER BY id", tuple(params))
    nodes = [
        PersonContactAssociation(
            id=int(row["id"]),
            person_id=row["from_id"],
            contact_id=int(row["to_id"]),
            source=_source_enum(row["source"]),
            role=row["evidence_role"],
            source_rowid=int(row["source_rowid"]),
            confidence=float(row["confidence"]),
        )
        for row in rows
    ]
    return _paged_connection(PersonContactAssociationConnection, nodes, limit, offset)


def _person_vehicle_associations(db: GraphDatabase, person_id: str, *, source: str | None, limit: int | None, offset: int | None) -> PersonVehicleAssociationConnection:
    clauses = ["from_type = 'person'", "from_id = ?", "to_type = 'vehicle'"]
    params: list[Any] = [person_id]
    if source:
        clauses.append("source = ?")
        params.append(source)
    rows = db.fetch_all(f"SELECT * FROM entity_edges WHERE {' AND '.join(clauses)} ORDER BY id", tuple(params))
    nodes = [
        PersonVehicleAssociation(
            id=int(row["id"]),
            person_id=row["from_id"],
            vehicle_id=int(row["to_id"]),
            source=_source_enum(row["source"]),
            role=row["evidence_role"],
            source_rowid=int(row["source_rowid"]),
            confidence=float(row["confidence"]),
        )
        for row in rows
    ]
    return _paged_connection(PersonVehicleAssociationConnection, nodes, limit, offset)


def _vehicle_person_associations(db: GraphDatabase, vehicle_id: int, *, source: str | None, limit: int | None, offset: int | None) -> PersonVehicleAssociationConnection:
    clauses = ["from_type = 'person'", "to_type = 'vehicle'", "to_id = ?"]
    params: list[Any] = [str(vehicle_id)]
    if source:
        clauses.append("source = ?")
        params.append(source)
    rows = db.fetch_all(f"SELECT * FROM entity_edges WHERE {' AND '.join(clauses)} ORDER BY id", tuple(params))
    nodes = [
        PersonVehicleAssociation(
            id=int(row["id"]),
            person_id=row["from_id"],
            vehicle_id=int(row["to_id"]),
            source=_source_enum(row["source"]),
            role=row["evidence_role"],
            source_rowid=int(row["source_rowid"]),
            confidence=float(row["confidence"]),
        )
        for row in rows
    ]
    return _paged_connection(PersonVehicleAssociationConnection, nodes, limit, offset)


def _addresses_for_record(db: GraphDatabase, source: str, rowid: int, role: str, limit: int | None, offset: int | None) -> AddressConnection:
    total = int(db.fetch_value("SELECT COUNT(*) FROM address_edges WHERE source = ? AND source_rowid = ? AND role = ?", (source, rowid, role)))
    rows = db.fetch_all(
        """
        SELECT addresses.*
        FROM address_edges
        JOIN addresses ON addresses.id = address_edges.address_id
        WHERE address_edges.source = ? AND address_edges.source_rowid = ? AND address_edges.role = ?
        ORDER BY addresses.id
        LIMIT ? OFFSET ?
        """,
        (source, rowid, role, clamp_limit(limit), clamp_offset(offset)),
    )
    return AddressConnection(page_info=_page(total, limit, offset), nodes=[_row_to_address(row) for row in rows])


def _edge_filter(base_sql: str, params: list[Any], relation_types: list[str] | None) -> tuple[str, list[Any]]:
    if relation_types:
        placeholders = ", ".join("?" for _ in relation_types)
        base_sql = f"{base_sql} AND relation_type IN ({placeholders})"
        params.extend(relation_types)
    return base_sql, params
