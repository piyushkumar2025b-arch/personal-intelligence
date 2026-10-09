"""
Neo4j graph client.
Stores: entities (Person, Topic, Document, Event, Concept),
        relationships (RELATED_TO, MENTIONED_IN, CAUSED_BY, etc.)
Every chunk ingested gets linked to its entities here.
"""

from __future__ import annotations
import re
from dataclasses import dataclass
from typing import Any

try:
    from neo4j import AsyncGraphDatabase, AsyncDriver
    HAS_NEO4J = True
except ImportError:
    AsyncGraphDatabase = None  # type: ignore
    AsyncDriver = None  # type: ignore
    HAS_NEO4J = False

from personal_intelligence.infrastructure.monitoring.logger import get_logger

logger = get_logger(__name__)


_LUCENE_SPECIAL = re.compile(r'([+\-&|!(){}\[\]^"~*?:\\/])')


def _lucene_escape(text: str) -> str:
    """Escape Lucene operators so natural-language queries don't raise syntax errors."""
    return _LUCENE_SPECIAL.sub(r"\\\1", text)


@dataclass
class Entity:
    id: str
    type: str          # Person | Topic | Document | Event | Concept | Place
    name: str
    properties: dict[str, Any]


@dataclass
class Relation:
    source_id: str
    target_id: str
    rel_type: str      # e.g. RELATED_TO, MENTIONED_IN, CAUSED_BY, KNOWS
    properties: dict[str, Any]


class GraphClient:
    """Async Neo4j wrapper with support for Neo4j AuraDB (cloud) and offline fallback."""

    def __init__(self) -> None:
        self._driver: Any | None = None
        self._is_connected: bool = False

    @property
    def is_connected(self) -> bool:
        return self._is_connected and self._driver is not None

    async def connect(self) -> None:
        if not HAS_NEO4J:
            logger.warning("neo4j_driver_not_installed - graph features disabled (Vector + BM25 will operate normally)")
            self._is_connected = False
            return

        if not settings.neo4j_enabled or not settings.neo4j_uri:
            logger.info("neo4j_disabled - skipping graph connection")
            self._is_connected = False
            return

        try:
            self._driver = AsyncGraphDatabase.driver(
                settings.neo4j_uri,
                auth=(settings.neo4j_user, settings.neo4j_password),
            )
            await self._driver.verify_connectivity()
            await self._ensure_indexes()
            self._is_connected = True
            logger.info("neo4j_connected", uri=settings.neo4j_uri)
        except Exception as exc:
            logger.warning(
                "neo4j_connection_failed - graph features disabled (Vector + BM25 will operate normally)",
                uri=settings.neo4j_uri,
                error=str(exc),
            )
            self._is_connected = False

    async def close(self) -> None:
        if self._driver:
            await self._driver.close()
            self._is_connected = False

    @property
    def driver(self) -> Any:
        if not self.is_connected or not self._driver:
            raise RuntimeError("GraphClient not connected. Call .connect() first.")
        return self._driver

    # ── Schema setup ─────────────────────────────────────────────

    async def _ensure_indexes(self) -> None:
        queries = [
            "CREATE CONSTRAINT entity_id IF NOT EXISTS FOR (e:Entity) REQUIRE e.id IS UNIQUE",
            "CREATE CONSTRAINT doc_id IF NOT EXISTS FOR (d:Document) REQUIRE d.id IS UNIQUE",
            "CREATE INDEX entity_name IF NOT EXISTS FOR (e:Entity) ON (e.name)",
            "CREATE INDEX entity_type IF NOT EXISTS FOR (e:Entity) ON (e.type)",
            "CREATE FULLTEXT INDEX entity_search IF NOT EXISTS FOR (e:Entity) ON EACH [e.name, e.description]",
        ]
        async with self.driver.session() as session:
            for q in queries:
                try:
                    await session.run(q)
                except Exception as exc:
                    logger.warning("neo4j_index_failed", query=q[:60], error=str(exc))

    # ── Entity CRUD ──────────────────────────────────────────────

    async def upsert_entity(self, entity: Entity) -> None:
        if not self.is_connected:
            return
        query = """
        MERGE (e:Entity {id: $id})
        SET e.type = $type,
            e.name = $name,
            e += $properties,
            e.updated_at = timestamp()
        RETURN e
        """
        async with self.driver.session() as session:
            await session.run(query, id=entity.id, type=entity.type,
                              name=entity.name, properties=entity.properties)

    async def upsert_entities_batch(self, entities: list[Entity]) -> None:
        if not self.is_connected:
            return
        query = """
        UNWIND $entities AS ent
        MERGE (e:Entity {id: ent.id})
        SET e.type = ent.type, e.name = ent.name, e += ent.properties, e.updated_at = timestamp()
        """
        async with self.driver.session() as session:
            await session.run(query, entities=[
                {"id": e.id, "type": e.type, "name": e.name, "properties": e.properties}
                for e in entities
            ])

    async def upsert_relation(self, rel: Relation) -> None:
        if not self.is_connected:
            return
        query = """
        MATCH (a:Entity {id: $src}), (b:Entity {id: $tgt})
        MERGE (a)-[r:RELATED {type: $rel_type}]->(b)
        SET r += $properties, r.updated_at = timestamp()
        """
        async with self.driver.session() as session:
            await session.run(query, src=rel.source_id, tgt=rel.target_id,
                              rel_type=rel.rel_type, properties=rel.properties)

    # ── Linking chunks to entities ───────────────────────────────

    async def link_chunk_to_entities(self, chunk_id: str, entity_ids: list[str]) -> None:
        if not self.is_connected:
            return
        query = """
        MERGE (c:Chunk {id: $chunk_id})
        WITH c
        UNWIND $entity_ids AS eid
        MATCH (e:Entity {id: eid})
        MERGE (c)-[:MENTIONS]->(e)
        """
        async with self.driver.session() as session:
            await session.run(query, chunk_id=chunk_id, entity_ids=entity_ids)

    # ── Retrieval ────────────────────────────────────────────────

    async def find_entities_by_name(self, name: str, limit: int = 10) -> list[dict]:
        if not self.is_connected:
            return []
        query = """
        CALL db.index.fulltext.queryNodes('entity_search', $name)
        YIELD node, score
        RETURN node.id AS id, node.name AS name, node.type AS type,
               node.description AS description, score
        ORDER BY score DESC
        LIMIT $limit
        """
        escaped = _lucene_escape(name)
        if not escaped.strip():
            return []
        async with self.driver.session() as session:
            result = await session.run(query, name=escaped, limit=limit)
            return [dict(r) async for r in result]

    async def get_entity_neighborhood(
        self, entity_id: str, depth: int = 2, limit: int = 50
    ) -> list[dict]:
        """Return all nodes within `depth` hops of an entity."""
        if not self.is_connected:
            return []
        depth = max(1, min(int(depth), 4))  # bounds can't be parameters; inline a validated int
        query = f"""
        MATCH path = (e:Entity {{id: $entity_id}})-[*1..{depth}]-(neighbor)
        WITH neighbor, relationships(path) AS rels
        UNWIND rels AS r
        RETURN DISTINCT
            startNode(r).name AS from_name,
            type(r) AS rel_type,
            endNode(r).name AS to_name,
            r.type AS rel_subtype
        LIMIT $limit
        """
        async with self.driver.session() as session:
            result = await session.run(query, entity_id=entity_id, limit=limit)
            return [dict(r) async for r in result]

    async def get_chunks_for_entities(self, entity_ids: list[str], limit: int = 20) -> list[str]:
        """Get chunk IDs that mention any of the given entities."""
        if not self.is_connected:
            return []
        query = """
        UNWIND $entity_ids AS eid
        MATCH (c:Chunk)-[:MENTIONS]->(e:Entity {id: eid})
        RETURN DISTINCT c.id AS chunk_id
        LIMIT $limit
        """
        async with self.driver.session() as session:
            result = await session.run(query, entity_ids=entity_ids, limit=limit)
            return [r["chunk_id"] async for r in result]

    async def get_related_entities(self, entity_id: str, limit: int = 20) -> list[dict]:
        if not self.is_connected:
            return []
        query = """
        MATCH (e:Entity {id: $entity_id})-[r]-(related:Entity)
        RETURN related.id AS id, related.name AS name, related.type AS type, type(r) AS rel_type
        ORDER BY related.name
        LIMIT $limit
        """
        async with self.driver.session() as session:
            result = await session.run(query, entity_id=entity_id, limit=limit)
            return [dict(r) async for r in result]

    async def get_graph_stats(self) -> dict:
        if not self.is_connected:
            return {"entities": 0, "relations": 0, "chunks": 0}
        async with self.driver.session() as session:
            r = await session.run("""
            RETURN
              count{(e:Entity)} AS entities,
              count{()-[:RELATED]-()} AS relations,
              count{(c:Chunk)} AS chunks
            """)
            return dict(await r.single() or {})


# Singleton
graph_client = GraphClient()
