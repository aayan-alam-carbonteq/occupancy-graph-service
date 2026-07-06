from __future__ import annotations

from pathlib import Path

from strawberry.asgi import GraphQL

from occupancy_graph.graphql.schema import build_context, create_schema


def create_app(db_path: Path) -> GraphQL:
    schema = create_schema(db_path)
    context = build_context(db_path)

    class OccupancyGraphQL(GraphQL):
        async def get_context(self, request, response):
            return context

    return OccupancyGraphQL(schema, graphql_ide="graphiql")
