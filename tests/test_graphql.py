from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from occupancy_graph.graphdb import build_index
from occupancy_graph.graphql.db import GraphDatabase
from occupancy_graph.graphql.guardrails import MAX_LIMIT
from occupancy_graph.graphql.schema import build_context, create_schema
from graph_fixtures import write_graph_fixture


class GraphQlTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        cleaned = Path(self.tempdir.name) / "cleaned"
        self.db = Path(self.tempdir.name) / "graph.sqlite"
        write_graph_fixture(cleaned)
        build_index(cleaned, self.db)
        self.schema = create_schema(self.db)
        self.context = build_context(self.db)

    def tearDown(self) -> None:
        self.context.db.close()

    def _execute(self, query: str, variables: dict | None = None) -> dict:
        result = self.schema.execute_sync(query, variable_values=variables, context_value=self.context)
        if result.errors:
            raise AssertionError(result.errors)
        return result.data

    def test_person_lookup_and_relation_traversal(self) -> None:
        data = self._execute(
            """
            query {
              person(id: "p1") {
                id
                firstname
                taxRecords { totalCount nodes { table data } }
                addresses { totalCount hasMore pageInfo { limit offset } nodes { normAddress zip5 } }
              }
            }
            """
        )
        person = data["person"]
        self.assertEqual(person["id"], "p1")
        self.assertEqual(person["firstname"], "Jane")
        self.assertEqual(person["taxRecords"]["totalCount"], 1)
        self.assertEqual(person["taxRecords"]["nodes"][0]["data"]["ownername"], "DOE, JANE")
        self.assertGreaterEqual(person["addresses"]["totalCount"], 1)
        self.assertFalse(person["addresses"]["hasMore"])
        self.assertEqual(person["addresses"]["nodes"][0]["normAddress"], "123 MAIN ST")

    def test_address_to_utility_traversal(self) -> None:
        data = self._execute(
            """
            query {
              addressByText(query: "123 MAIN ST", zip: "40505") {
                normAddress
                streetNumber
                streetName
                utilityRecords { totalCount nodes { table data } }
              }
            }
            """
        )
        address = data["addressByText"]
        self.assertEqual(address["normAddress"], "123 MAIN ST")
        self.assertEqual(address["streetNumber"], "123")
        self.assertEqual(address["utilityRecords"]["totalCount"], 1)
        self.assertEqual(address["utilityRecords"]["nodes"][0]["data"]["last_name"], "Tenant")

    def test_person_base_records_expose_demographic_fields(self) -> None:
        data = self._execute(
            """
            query {
              person(id: "p1") {
                baseRecords { totalCount nodes { table rowid data } }
              }
            }
            """
        )
        connection = data["person"]["baseRecords"]
        self.assertEqual(connection["totalCount"], 1)
        node = connection["nodes"][0]
        self.assertEqual(node["table"], "base")
        self.assertEqual(node["data"]["id"], "p1")
        self.assertEqual(node["data"]["homeownerprobabilitymodel"], "H")
        # the demographic/tenure fields that were previously unreachable via the graph
        self.assertIn("lengthofresidence", node["data"])
        self.assertIn("mortgageamountinthousands", node["data"])

    def test_address_base_records_return_resident_rows(self) -> None:
        data = self._execute(
            """
            query {
              addressByText(query: "123 MAIN ST", zip: "40505") {
                baseRecords { totalCount nodes { table data } }
              }
            }
            """
        )
        connection = data["addressByText"]["baseRecords"]
        self.assertEqual(connection["totalCount"], 2)
        self.assertEqual(connection["nodes"][0]["table"], "base")
        self.assertEqual({node["data"]["id"] for node in connection["nodes"]}, {"p1", "p2"})

    def test_collection_filter_and_pagination(self) -> None:
        data = self._execute(
            """
            query {
              persons(where: {lastname: {eq: "Smith"}}, limit: 1, offset: 0) {
                totalCount
                nodes { id lastname }
              }
              baseRecords(where: {lastname: {eq: "Smith"}}, limit: 1, offset: 0) {
                totalCount
                nodes { id lastname }
              }
            }
            """
        )
        connection = data["persons"]
        self.assertEqual(connection["totalCount"], 2)
        self.assertEqual(len(connection["nodes"]), 1)
        self.assertEqual(data["baseRecords"]["totalCount"], 2)
        self.assertEqual(data["baseRecords"]["nodes"][0]["lastname"], "Smith")

    def test_search_entrypoints_return_match_scores_and_counts(self) -> None:
        data = self._execute(
            """
            query {
              searchPersons(query: "Jane Doe", limit: 2) {
                totalCount
                hasMore
                nodes {
                  matchScore
                  matchedFields
                  person { id }
                  nameObservation { fullName source }
                }
              }
              searchAddresses(query: "123 Main Street", zip: "40505") {
                totalCount
                nodes {
                  matchScore
                  relationCount
                  address { id normAddress zip5 }
                }
              }
            }
            """
        )
        self.assertGreaterEqual(data["searchPersons"]["totalCount"], 1)
        self.assertTrue(data["searchPersons"]["hasMore"])
        self.assertEqual(data["searchPersons"]["nodes"][0]["person"]["id"], "p1")
        self.assertIn("name", data["searchPersons"]["nodes"][0]["matchedFields"])
        self.assertEqual(data["searchAddresses"]["nodes"][0]["address"]["normAddress"], "123 MAIN ST")
        self.assertGreater(data["searchAddresses"]["nodes"][0]["relationCount"], 0)

    def test_traversal_entrypoints_are_connection_shaped(self) -> None:
        data = self._execute(
            """
            query {
              peopleAtAddress(addressId: 1, relationTypes: ["RESIDENCE"]) {
                totalCount
                nodes { id firstname }
              }
              addressesForPerson(personId: "p1", relationTypes: ["RESIDENCE"]) {
                totalCount
                nodes { normAddress }
              }
            }
            """
        )
        self.assertEqual(data["peopleAtAddress"]["totalCount"], 2)
        self.assertEqual(data["addressesForPerson"]["nodes"][0]["normAddress"], "123 MAIN ST")

    def test_entity_first_address_associations(self) -> None:
        data = self._execute(
            """
            query {
              resolveAddress(query: "123 MAIN ST", zip: "40505") {
                id
                fullAddress
                normalizedAddress
                zip
                personAssociations(source: DRIVE, role: LICENSE_ADDRESS) {
                  totalCount
                  nodes {
                    role
                    source
                    confidence
                    person { id name firstname lastname }
                    address { fullAddress }
                    sourceRecord { source table rowid recordId summary }
                    provenance { source rowid summary }
                  }
                }
                sourceRecords(source: UTILITY, role: SERVICE_ADDRESS) {
                  totalCount
                  nodes { source table rowid summary data }
                }
              }
            }
            """
        )
        address = data["resolveAddress"]
        self.assertEqual(address["fullAddress"], "123 MAIN ST, 40505")
        self.assertEqual(address["normalizedAddress"], "123 MAIN ST")
        self.assertEqual(address["zip"], "40505")
        association = address["personAssociations"]["nodes"][0]
        self.assertEqual(association["role"], "LICENSE_ADDRESS")
        self.assertEqual(association["source"], "DRIVE")
        self.assertEqual(association["person"]["id"], "p1")
        self.assertIn("drive", association["sourceRecord"]["summary"])
        self.assertEqual(address["sourceRecords"]["totalCount"], 1)
        self.assertEqual(address["sourceRecords"]["nodes"][0]["source"], "UTILITY")
        self.assertEqual(address["sourceRecords"]["nodes"][0]["data"]["last_name"], "Tenant")

    def test_entity_first_property_and_person_traversal(self) -> None:
        data = self._execute(
            """
            query {
              resolveAddress(query: "123 MAIN ST", zip: "40505") {
                propertyAssociations(role: SITUS_ADDRESS) {
                  totalCount
                  nodes {
                    role
                    property {
                      id
                      propertyKey
                      people(role: OWNER) {
                        nodes {
                          role
                          displayName
                          person { id name }
                          sourceRecord { source rowid summary }
                          provenance { source rowid summary }
                        }
                      }
                      organizations(role: LENDER) {
                        nodes {
                          role
                          organization { name organizationType }
                        }
                      }
                    }
                  }
                }
              }
              person(id: "p1") {
                name
                addressAssociations(source: AUTO, role: REGISTRATION_ADDRESS) {
                  nodes {
                    address { fullAddress }
                    sourceRecord { source summary }
                  }
                }
                organizationAssociations(source: LOAN, role: EMPLOYER) {
                  nodes {
                    organization { name organizationType }
                    provenance { source summary }
                  }
                }
              }
            }
            """
        )
        property_node = data["resolveAddress"]["propertyAssociations"]["nodes"][0]["property"]
        owner = property_node["people"]["nodes"][0]
        self.assertEqual(owner["role"], "OWNER")
        self.assertEqual(owner["displayName"], "DOE, JANE")
        self.assertEqual(owner["person"]["id"], "p1")
        self.assertEqual(owner["sourceRecord"]["source"], "TAX")
        self.assertIn("ownername=DOE, JANE", owner["sourceRecord"]["summary"])
        lender = property_node["organizations"]["nodes"][0]
        self.assertEqual(lender["organization"]["name"], "BANK")
        auto_association = data["person"]["addressAssociations"]["nodes"][0]
        self.assertEqual(auto_association["address"]["fullAddress"], "456 PINE ST, 40505")
        employer = data["person"]["organizationAssociations"]["nodes"][0]
        self.assertEqual(employer["organization"]["name"], "ACME")

    def test_auto_filter_by_vin(self) -> None:
        data = self._execute(
            """
            query {
              autos(where: {vin: {eq: "VIN1"}}) {
                totalCount
                nodes { vin make }
              }
            }
            """
        )
        self.assertEqual(data["autos"]["totalCount"], 1)
        self.assertEqual(data["autos"]["nodes"][0]["make"], "FORD")

    def test_limit_cap_enforced(self) -> None:
        data = self._execute(
            f"""
            query {{
              persons(limit: {MAX_LIMIT + 100}) {{
                nodes {{ id }}
              }}
            }}
            """
        )
        self.assertLessEqual(len(data["persons"]["nodes"]), MAX_LIMIT)

    def test_read_only_database_rejects_writes(self) -> None:
        db = GraphDatabase(self.db)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                db.connection.execute('INSERT INTO "base" (id) VALUES (?)', ("blocked",))
        finally:
            db.close()

    def test_schema_has_no_mutations(self) -> None:
        mutation_type = self.schema._schema.mutation_type
        self.assertIsNone(mutation_type)


if __name__ == "__main__":
    unittest.main()
