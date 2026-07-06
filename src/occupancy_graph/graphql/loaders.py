from __future__ import annotations

from strawberry.dataloader import DataLoader

from occupancy_graph.graphql.db import GraphDatabase, quote
from occupancy_graph.graphql.registry import SchemaRegistry
from occupancy_graph.graphql.types import RecordNode, row_to_record_node


class GraphLoaders:
    def __init__(self, db: GraphDatabase, registry: SchemaRegistry) -> None:
        self.db = db
        self.registry = registry
        self.records_by_person_id: dict[str, DataLoader[str, list[RecordNode]]] = {}
        for source in registry.source_tables():
            if not source.id_linked or source.name == "base":
                continue
            self.records_by_person_id[source.name] = DataLoader(
                load_fn=self._make_person_records_loader(source.name)
            )

    def _make_person_records_loader(self, source: str):
        async def load_fn(person_ids: list[str]) -> list[list[RecordNode]]:
            if not person_ids:
                return []
            placeholders = ", ".join("?" for _ in person_ids)
            rows = self.db.fetch_all(
                f"SELECT rowid, * FROM {quote(source)} WHERE id IN ({placeholders})",
                tuple(person_ids),
            )
            grouped: dict[str, list[RecordNode]] = {person_id: [] for person_id in person_ids}
            for row in rows:
                node = row_to_record_node(source, row)
                grouped.setdefault(node.data.get("id", ""), []).append(node)
            return [grouped.get(person_id, []) for person_id in person_ids]

        return load_fn
