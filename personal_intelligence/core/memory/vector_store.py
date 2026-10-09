"""
Qdrant vector store client.
Stores chunk embeddings + metadata.
Supports: upsert, semantic search, filtered search, delete.

Zero-Docker options:
1. Embedded local disk mode (default): uses local files in `./data/qdrant` via QdrantClient(path=...)
2. Qdrant Cloud: set QDRANT_URL and QDRANT_API_KEY in .env (free tier)
3. Remote Qdrant server: host + port
"""

from __future__ import annotations
import asyncio
from pathlib import Path
import uuid
from dataclasses import dataclass, field
from typing import Any

try:
    from qdrant_client import AsyncQdrantClient, QdrantClient
    from qdrant_client.models import (
        Distance, VectorParams, PointStruct,
        Filter, FieldCondition, MatchValue,
        ScoredPoint,
    )
    HAS_QDRANT = True
except ImportError:
    AsyncQdrantClient = None  # type: ignore
    QdrantClient = None  # type: ignore
    Distance = None  # type: ignore
    VectorParams = None  # type: ignore
    PointStruct = None  # type: ignore
    Filter = None  # type: ignore
    FieldCondition = None  # type: ignore
    MatchValue = None  # type: ignore
    ScoredPoint = None  # type: ignore
    HAS_QDRANT = False

from personal_intelligence.config.settings import settings
from personal_intelligence.infrastructure.monitoring.logger import get_logger

logger = get_logger(__name__)


@dataclass
class Chunk:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    text: str = ""
    embedding: list[float] = field(default_factory=list)
    source: str = ""           # file path, URL, telegram message id, etc.
    source_type: str = "text"  # text | pdf | url | telegram | api
    doc_id: str = ""
    chunk_index: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class SearchResult:
    chunk: Chunk
    score: float


class VectorStore:
    """Qdrant wrapper supporting embedded local storage, Qdrant Cloud, and remote instances."""

    def __init__(self) -> None:
        self._async_client: Any | None = None
        self._sync_client: Any | None = None
        self._is_local: bool = False
        self.collection = settings.qdrant_collection_name

    async def connect(self) -> None:
        if not HAS_QDRANT:
            raise RuntimeError("qdrant-client is not installed. Install with: pip install qdrant-client")

        if settings.qdrant_url:
            # Mode 1: Qdrant Cloud or remote URL
            self._is_local = False
            self._async_client = AsyncQdrantClient(
                url=settings.qdrant_url,
                api_key=settings.qdrant_api_key,
            )
            logger.info("qdrant_connected_cloud", url=settings.qdrant_url)
        elif settings.qdrant_path:
            # Mode 2: Zero-Docker embedded local storage
            self._is_local = True
            storage_path = Path(settings.qdrant_path)
            storage_path.mkdir(parents=True, exist_ok=True)
            self._sync_client = QdrantClient(path=str(storage_path))
            logger.info("qdrant_connected_local_embedded", path=str(storage_path))
        else:
            # Mode 3: Host + Port server
            self._is_local = False
            self._async_client = AsyncQdrantClient(
                host=settings.qdrant_host,
                port=settings.qdrant_port,
            )
            logger.info("qdrant_connected_server", host=settings.qdrant_host, port=settings.qdrant_port)

        await self._ensure_collection()

    async def close(self) -> None:
        if self._async_client:
            await self._async_client.close()
        if self._sync_client:
            self._sync_client.close()

    async def _ensure_collection(self) -> None:
        if self._is_local:
            existing = await asyncio.to_thread(self._sync_client.get_collections)
            names = [c.name for c in existing.collections]
            if self.collection not in names:
                await asyncio.to_thread(
                    self._sync_client.create_collection,
                    collection_name=self.collection,
                    vectors_config=VectorParams(
                        size=settings.embedding_dim,
                        distance=Distance.COSINE,
                    ),
                )
                logger.info("qdrant_collection_created", collection=self.collection)
        else:
            existing = await self._async_client.get_collections()
            names = [c.name for c in existing.collections]
            if self.collection not in names:
                await self._async_client.create_collection(
                    collection_name=self.collection,
                    vectors_config=VectorParams(
                        size=settings.embedding_dim,
                        distance=Distance.COSINE,
                    ),
                )
                logger.info("qdrant_collection_created", collection=self.collection)

    # ── Write ─────────────────────────────────────────────────────

    async def upsert(self, chunks: list[Chunk]) -> None:
        points = [
            PointStruct(
                id=c.id,
                vector=c.embedding,
                payload={
                    "text": c.text,
                    "source": c.source,
                    "source_type": c.source_type,
                    "doc_id": c.doc_id,
                    "chunk_index": c.chunk_index,
                    **c.metadata,
                },
            )
            for c in chunks
        ]
        if self._is_local:
            await asyncio.to_thread(self._sync_client.upsert, collection_name=self.collection, points=points)
        else:
            await self._async_client.upsert(collection_name=self.collection, points=points)
        logger.debug("qdrant_upsert", count=len(chunks))

    async def delete_by_doc(self, doc_id: str) -> None:
        points_selector = Filter(
            must=[FieldCondition(key="doc_id", match=MatchValue(value=doc_id))]
        )
        if self._is_local:
            await asyncio.to_thread(self._sync_client.delete, collection_name=self.collection, points_selector=points_selector)
        else:
            await self._async_client.delete(collection_name=self.collection, points_selector=points_selector)

    # ── Search ────────────────────────────────────────────────────

    async def search(
        self,
        query_embedding: list[float],
        top_k: int = 20,
        source_type: str | None = None,
        doc_id: str | None = None,
    ) -> list[SearchResult]:
        filters: list[FieldCondition] = []
        if source_type:
            filters.append(FieldCondition(key="source_type", match=MatchValue(value=source_type)))
        if doc_id:
            filters.append(FieldCondition(key="doc_id", match=MatchValue(value=doc_id)))

        query_filter = Filter(must=filters) if filters else None

        if self._is_local:
            response = await asyncio.to_thread(
                self._sync_client.query_points,
                collection_name=self.collection,
                query=query_embedding,
                limit=top_k,
                query_filter=query_filter,
                with_payload=True,
            )
        else:
            response = await self._async_client.query_points(
                collection_name=self.collection,
                query=query_embedding,
                limit=top_k,
                query_filter=query_filter,
                with_payload=True,
            )

        hits: list[ScoredPoint] = response.points
        return [
            SearchResult(
                chunk=Chunk(
                    id=str(h.id),
                    text=(h.payload or {}).get("text", ""),
                    source=(h.payload or {}).get("source", ""),
                    source_type=(h.payload or {}).get("source_type", ""),
                    doc_id=(h.payload or {}).get("doc_id", ""),
                    chunk_index=(h.payload or {}).get("chunk_index", 0),
                    metadata={k: v for k, v in (h.payload or {}).items()
                              if k not in ("text", "source", "source_type", "doc_id", "chunk_index")},
                ),
                score=h.score,
            )
            for h in hits
        ]

    async def get_collection_info(self) -> dict:
        if self._is_local:
            info = await asyncio.to_thread(self._sync_client.get_collection, self.collection)
        else:
            info = await self._async_client.get_collection(self.collection)
        return {
            "points_count": info.points_count,
            "indexed_vectors_count": info.indexed_vectors_count,
            "status": info.status.value,
        }


# Singleton
vector_store = VectorStore()
