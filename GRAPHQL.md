# GraphQL Agent Graph

Read-only GraphQL API over the cleaned Lexington CSVs. The graph index is an additive SQLite artifact; source CSVs are never modified. This layer is optimized for LLM agents: search first, anchor on entity IDs, traverse relationships through paginated connections, and drill into raw source records only when needed.

## Build the graph index

```bash
uv run oe-graphdb-build-index \
  --cleaned-dir data/cleaned/lexington \
  --db data/indexes/graph.sqlite
```

The index imports all cleaned source tables as raw `TEXT` tables, adds `__norm_*` helper columns, and materializes:

- `addresses` / `address_edges` for normalized address matching.
- `person_entities` from canonical `base.id` rows.
- `name_observations` from every source that carries first/last names, including source-only utility names.
- `contact_entities`, `vehicle_entities`, `organization_entities`, `property_entities`.
- `entity_edges` with source-backed provenance and confidence.

## Serve GraphQL

```bash
uv run oe-graphql-serve \
  --db data/indexes/graph.sqlite \
  --host 127.0.0.1 \
  --port 8000
```

GraphiQL is available at `http://127.0.0.1:8000/graphql`. SQLite is opened read-only and `PRAGMA query_only=ON` is set. No mutations are defined.

## Agent query pattern

Agents should use this flow:

1. Search by user input.
2. Select a stable entity ID from candidates.
3. Traverse by entity ID.
4. Page through full result sets only when counts indicate it is necessary.

### Person search to traversal

```graphql
query {
  searchPersons(query: "Jane Doe", limit: 10) {
    totalCount
    hasMore
    nodes {
      matchScore
      matchedFields
      person { id firstname lastname }
      nameObservation { fullName source confidence }
    }
  }
}
```

```graphql
query {
  person(id: "cd126210") {
    id
    firstname
    lastname
    names { totalCount nodes { fullName source confidence } }
    addresses(relationTypes: ["RESIDENCE"]) {
      totalCount
      nodes { id normAddress zip5 city state }
    }
    contacts { totalCount nodes { contactType value } }
    taxRecords { totalCount nodes { table rowid data } }
  }
}
```

### Address search to traversal

```graphql
query {
  searchAddresses(query: "608 Tundra Ct", zip: "40517") {
    totalCount
    nodes {
      matchScore
      relationCount
      address { id normAddress zip5 streetNumber streetName }
    }
  }
}
```

```graphql
query {
  address(id: 123) {
    normAddress
    zip5
    residents { totalCount nodes { id firstname lastname } }
    utilityRecords { totalCount nodes { table rowid data } }
    taxProperties { totalCount nodes { table rowid data } }
    listings { totalCount nodes { table rowid data } }
  }
}
```

### Direct traversal entry points

```graphql
query {
  peopleAtAddress(addressId: 123, relationTypes: ["RESIDENCE"], limit: 50) {
    totalCount
    hasMore
    nodes { id firstname lastname }
  }

  addressesForPerson(personId: "cd126210", relationTypes: ["RESIDENCE"], limit: 50) {
    totalCount
    nodes { id normAddress zip5 }
  }
}
```

## Connections and completeness

All agent-facing list fields return connections:

```graphql
{
  totalCount
  hasMore
  pageInfo { totalCount hasMore limit offset }
  nodes { ... }
}
```

This gives agents complete counts without forcing every row into context. Default limit is 50; hard cap is 500.

## Raw source access

Raw source tables remain queryable as paginated collections. The canonical base table is exposed as `baseRecords` to avoid colliding with entity `Person`.

```graphql
query {
  baseRecords(where: { lastname: { eq: "Smith" } }, limit: 25) {
    totalCount
    nodes { id firstname lastname primaryaddress }
  }

  autos(where: { vin: { eq: "VIN1" } }, limit: 10) {
    totalCount
    nodes { id vin make model address }
  }
}
```

Nested raw drilldowns return generic `SourceRecord` nodes:

```graphql
query {
  person(id: "cd126210") {
    taxRecords {
      totalCount
      nodes { table rowid data }
    }
  }
}
```

## Guardrails

- Read-only SQLite URI plus `PRAGMA query_only=ON`.
- No mutations.
- Full introspection enabled for agents.
- Default `limit`: 50.
- Hard `limit` cap: 500.
- Query depth limit: 12.
- Alias limit: 50.

## Tests

```bash
uv run pytest tests/test_graphdb.py tests/test_graphql.py -v
```
