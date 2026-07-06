from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import strawberry
from strawberry.extensions import MaxAliasesLimiter, QueryDepthLimiter
from strawberry.types import Info

from occupancy_graph.graphql.db import GraphDatabase
from occupancy_graph.graphql.filters import OrderByInput
from occupancy_graph.graphql.guardrails import MAX_ALIASES, MAX_QUERY_DEPTH
from occupancy_graph.graphql.loaders import GraphLoaders
from occupancy_graph.graphql.registry import SchemaRegistry
from occupancy_graph.graphql.types import (
    Address,
    AddressConnection,
    AddressSearchConnection,
    Contact,
    Person,
    PersonConnection,
    PersonWhereInput,
    PersonSearchConnection,
    Property,
    RecordNode,
    Source,
    SourceRecord,
    Vehicle,
    Organization,
    address_filter_schema,
    build_runtime_types,
    fetch_address_by_id,
    fetch_address_by_text,
    fetch_contact,
    fetch_organization,
    fetch_person,
    fetch_persons,
    fetch_address,
    fetch_addresses,
    fetch_property,
    fetch_source_record,
    fetch_vehicle,
    fetch_record_by_id,
    fetch_records,
    search_addresses,
    search_persons,
    _page,
    PageInfo,
)


@dataclass
class GraphContext:
    db: GraphDatabase
    registry: SchemaRegistry
    runtime_types: dict[str, Any]
    loaders: GraphLoaders


def create_schema(db_path: Path) -> strawberry.Schema:
    registry = SchemaRegistry(db_path)
    runtime_types = build_runtime_types(registry)

    query_fields: dict[str, Any] = {}
    query_annotations: dict[str, Any] = {}

    for schema in registry.source_tables():
        record_type = runtime_types["record_type_by_table"][schema.name]
        connection_type = runtime_types["connection_type_by_table"][schema.name]
        where_input = runtime_types["where_input_by_table"][schema.name]

        if schema.id_linked:

            def make_singular_resolver(table_schema=schema):
                def resolve(id: str, info: Info) -> RecordNode | None:
                    return fetch_record_by_id(info.context.db, table_schema, id)

                return resolve

            query_annotations[schema.singular_field] = record_type | None
            query_fields[schema.singular_field] = strawberry.field(resolver=make_singular_resolver())

        def make_collection_resolver(table_schema=schema, connection=connection_type, where_cls=where_input):
            def resolve(info, where=None, order_by=None, limit=None, offset=None):
                nodes, total_count = fetch_records(
                    info.context.db,
                    table_schema,
                    where=where,
                    order_by=order_by,
                    limit=limit,
                    offset=offset,
                )
                return connection(page_info=_page(total_count, limit, offset), nodes=nodes)

            resolve.__annotations__ = {
                "info": Info,
                "where": where_cls | None,
                "order_by": list[OrderByInput] | None,
                "limit": int | None,
                "offset": int | None,
                "return": connection,
            }
            return resolve

        query_annotations[schema.collection_field] = connection_type
        query_fields[schema.collection_field] = strawberry.field(resolver=make_collection_resolver())

    address_where_input = runtime_types["address_where_input"]

    def resolve_person(id: str, info: Info) -> Person | None:
        return fetch_person(info.context.db, id)

    def resolve_persons(info: Info, where: PersonWhereInput | None = None, limit: int | None = None, offset: int | None = None) -> PersonConnection:
        return fetch_persons(info.context.db, where=where, limit=limit, offset=offset)

    def resolve_address(id: int, info: Info) -> Address | None:
        return fetch_address_by_id(info.context.db, id)

    def resolve_address_by_text(query: str, info: Info, zip: str | None = None) -> Address | None:
        return fetch_address_by_text(info.context.db, query, zip)

    def resolve_resolve_address(query: str, info: Info, zip: str | None = None) -> Address | None:
        return fetch_address_by_text(info.context.db, query, zip)

    def resolve_legacy_address(norm_address: str, zip: str, info: Info) -> Address | None:
        return fetch_address(info.context.db, norm_address, zip)

    def resolve_property(id: int, info: Info) -> Property | None:
        return fetch_property(info.context.db, id)

    def resolve_organization(id: int, info: Info) -> Organization | None:
        return fetch_organization(info.context.db, id)

    def resolve_vehicle(id: int, info: Info) -> Vehicle | None:
        return fetch_vehicle(info.context.db, id)

    def resolve_contact_point(id: int, info: Info) -> Contact | None:
        return fetch_contact(info.context.db, id)

    def resolve_source_record(source: Source, rowid: int, info: Info) -> SourceRecord | None:
        return fetch_source_record(info.context.db, source.value, rowid)

    def resolve_addresses(info, where=None, order_by=None, limit=None, offset=None):
        return fetch_addresses(
            info.context.db,
            where=where,
            order_by=order_by,
            limit=limit,
            offset=offset,
        )

    resolve_addresses.__annotations__ = {
        "info": Info,
        "where": address_where_input | None,
        "order_by": list[OrderByInput] | None,
        "limit": int | None,
        "offset": int | None,
        "return": AddressConnection,
    }

    def resolve_search_persons(query: str, info: Info, limit: int | None = None, offset: int | None = None) -> PersonSearchConnection:
        return search_persons(info.context.db, query, limit=limit, offset=offset)

    def resolve_search_people(query: str, info: Info, limit: int | None = None, offset: int | None = None) -> PersonSearchConnection:
        return search_persons(info.context.db, query, limit=limit, offset=offset)

    def resolve_search_addresses(query: str, info: Info, zip: str | None = None, limit: int | None = None, offset: int | None = None) -> AddressSearchConnection:
        return search_addresses(info.context.db, query, zip_code=zip, limit=limit, offset=offset)

    def resolve_people_at_address(address_id: int, info: Info, relation_types: list[str] | None = None, limit: int | None = None, offset: int | None = None) -> PersonConnection:
        address = fetch_address_by_id(info.context.db, address_id)
        if address is None:
            return PersonConnection(page_info=PageInfo(total_count=0, has_more=False, limit=limit or 50, offset=offset or 0), nodes=[])
        return address.people(info, relation_types=relation_types, limit=limit, offset=offset)

    def resolve_addresses_for_person(person_id: str, info: Info, relation_types: list[str] | None = None, limit: int | None = None, offset: int | None = None) -> AddressConnection:
        person = fetch_person(info.context.db, person_id)
        if person is None:
            return AddressConnection(page_info=PageInfo(total_count=0, has_more=False, limit=limit or 50, offset=offset or 0), nodes=[])
        return person.addresses(info, relation_types=relation_types, limit=limit, offset=offset)

    resolve_search_persons.__annotations__ = {"query": str, "info": Info, "limit": int | None, "offset": int | None, "return": PersonSearchConnection}
    resolve_search_people.__annotations__ = {"query": str, "info": Info, "limit": int | None, "offset": int | None, "return": PersonSearchConnection}
    resolve_search_addresses.__annotations__ = {"query": str, "info": Info, "zip": str | None, "limit": int | None, "offset": int | None, "return": AddressSearchConnection}
    resolve_people_at_address.__annotations__ = {"address_id": int, "info": Info, "relation_types": list[str] | None, "limit": int | None, "offset": int | None, "return": PersonConnection}
    resolve_addresses_for_person.__annotations__ = {"person_id": str, "info": Info, "relation_types": list[str] | None, "limit": int | None, "offset": int | None, "return": AddressConnection}
    resolve_address_by_text.__annotations__ = {"query": str, "info": Info, "zip": str | None, "return": Address | None}
    resolve_resolve_address.__annotations__ = {"query": str, "info": Info, "zip": str | None, "return": Address | None}
    resolve_legacy_address.__annotations__ = {"norm_address": str, "zip": str, "info": Info, "return": Address | None}
    resolve_property.__annotations__ = {"id": int, "info": Info, "return": Property | None}
    resolve_organization.__annotations__ = {"id": int, "info": Info, "return": Organization | None}
    resolve_vehicle.__annotations__ = {"id": int, "info": Info, "return": Vehicle | None}
    resolve_contact_point.__annotations__ = {"id": int, "info": Info, "return": Contact | None}
    resolve_source_record.__annotations__ = {"source": Source, "rowid": int, "info": Info, "return": SourceRecord | None}

    query_annotations["person"] = Person | None
    query_fields["person"] = strawberry.field(resolver=resolve_person)
    query_annotations["persons"] = PersonConnection
    query_fields["persons"] = strawberry.field(resolver=resolve_persons)
    query_annotations["address"] = Address | None
    query_fields["address"] = strawberry.field(resolver=resolve_address)
    query_annotations["resolveAddress"] = Address | None
    query_fields["resolveAddress"] = strawberry.field(resolver=resolve_resolve_address)
    query_annotations["addressByText"] = Address | None
    query_fields["addressByText"] = strawberry.field(resolver=resolve_address_by_text)
    query_annotations["addressByNormalized"] = Address | None
    query_fields["addressByNormalized"] = strawberry.field(resolver=resolve_legacy_address)
    query_annotations["property"] = Property | None
    query_fields["property"] = strawberry.field(resolver=resolve_property)
    query_annotations["organization"] = Organization | None
    query_fields["organization"] = strawberry.field(resolver=resolve_organization)
    query_annotations["vehicle"] = Vehicle | None
    query_fields["vehicle"] = strawberry.field(resolver=resolve_vehicle)
    query_annotations["contactPoint"] = Contact | None
    query_fields["contactPoint"] = strawberry.field(resolver=resolve_contact_point)
    query_annotations["sourceRecord"] = SourceRecord | None
    query_fields["sourceRecord"] = strawberry.field(resolver=resolve_source_record)
    query_annotations["addresses"] = AddressConnection
    query_fields["addresses"] = strawberry.field(resolver=resolve_addresses)
    query_annotations["searchPersons"] = PersonSearchConnection
    query_fields["searchPersons"] = strawberry.field(resolver=resolve_search_persons)
    query_annotations["searchPeople"] = PersonSearchConnection
    query_fields["searchPeople"] = strawberry.field(resolver=resolve_search_people)
    query_annotations["searchAddresses"] = AddressSearchConnection
    query_fields["searchAddresses"] = strawberry.field(resolver=resolve_search_addresses)
    query_annotations["peopleAtAddress"] = PersonConnection
    query_fields["peopleAtAddress"] = strawberry.field(resolver=resolve_people_at_address)
    query_annotations["addressesForPerson"] = AddressConnection
    query_fields["addressesForPerson"] = strawberry.field(resolver=resolve_addresses_for_person)

    query_fields["__annotations__"] = query_annotations
    query_type = strawberry.type(type("Query", (), query_fields))

    return strawberry.Schema(
        query=query_type,
        extensions=[
            lambda: QueryDepthLimiter(max_depth=MAX_QUERY_DEPTH),
            lambda: MaxAliasesLimiter(max_alias_count=MAX_ALIASES),
        ],
    )


def build_context(db_path: Path) -> GraphContext:
    registry = SchemaRegistry(db_path)
    runtime_types = build_runtime_types(registry)
    db = GraphDatabase(db_path)
    loaders = GraphLoaders(db, registry)
    return GraphContext(db=db, registry=registry, runtime_types=runtime_types, loaders=loaders)
