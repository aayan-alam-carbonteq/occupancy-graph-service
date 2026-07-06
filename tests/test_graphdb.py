from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from occupancy_graph.graphdb import build_index

from graph_fixtures import write_graph_fixture


class GraphDbTest(unittest.TestCase):
    def test_build_index_row_parity_and_graph_edges(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cleaned = Path(tmpdir) / "cleaned"
            db = Path(tmpdir) / "graph.sqlite"
            write_graph_fixture(cleaned)

            result = build_index(cleaned, db)

            self.assertEqual(result.source_rows["base"], 3)
            self.assertEqual(result.source_rows["utility"], 1)
            self.assertGreater(result.address_nodes, 0)
            self.assertGreater(result.address_edges, 0)
            self.assertEqual(result.person_entities, 3)
            self.assertGreater(result.name_observations, 0)
            self.assertGreater(result.entity_edges, 0)

            connection = sqlite3.connect(db)
            try:
                self.assertEqual(connection.execute('SELECT COUNT(*) FROM "base"').fetchone()[0], 3)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM addresses").fetchone()[0], result.address_nodes)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM address_edges").fetchone()[0], result.address_edges)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM person_entities").fetchone()[0], result.person_entities)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM name_observations").fetchone()[0], result.name_observations)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM entity_edges").fetchone()[0], result.entity_edges)

                residence_edges = connection.execute(
                    "SELECT COUNT(*) FROM address_edges WHERE source = 'base' AND role = 'residence'"
                ).fetchone()[0]
                service_edges = connection.execute(
                    "SELECT COUNT(*) FROM address_edges WHERE source = 'utility' AND role = 'service'"
                ).fetchone()[0]
                property_edges = connection.execute(
                    "SELECT COUNT(*) FROM address_edges WHERE source = 'tax' AND role = 'property'"
                ).fetchone()[0]
                owner_mailing_edges = connection.execute(
                    "SELECT COUNT(*) FROM address_edges WHERE source = 'tax' AND role = 'owner_mailing'"
                ).fetchone()[0]

                self.assertEqual(residence_edges, 3)
                self.assertEqual(service_edges, 1)
                self.assertEqual(property_edges, 1)
                self.assertEqual(owner_mailing_edges, 1)

                metadata_rows = connection.execute("SELECT source, rows FROM metadata ORDER BY source").fetchall()
                self.assertTrue(any(row[0] == "addresses" for row in metadata_rows))
                self.assertTrue(any(row[0] == "address_edges" for row in metadata_rows))
                self.assertTrue(any(row[0] == "person_entities" for row in metadata_rows))
                self.assertTrue(any(row[0] == "name_observations" for row in metadata_rows))
                self.assertTrue(any(row[0] == "entity_edges" for row in metadata_rows))
            finally:
                connection.close()

if __name__ == "__main__":
    unittest.main()
