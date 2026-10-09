"""Minimal ingestion: split text -> embed -> write to Relational DB + Qdrant (+ optional graph links)."""

from __future__ import annotations

import uuid

from personal_intelligence.config.settings import settings
from personal_intelligence.core.llm.client import llm_client
from personal_intelligence.core.memory.vector_store import Chunk, vector_store
from personal_intelligence.infrastructure.db.repositories import ChunkRepository

try:
    import tiktoken
    _enc = tiktoken.get_encoding("cl100k_base")
except ImportError:
    class _FallbackEncoding:  # type: ignore
        """Word-based fallback when tiktoken is not installed."""
        def encode(self, text: str) -> list[str]:
            return text.split()
        def decode(self, tokens: list[str]) -> str:
            return " ".join(tokens)
    _enc = _FallbackEncoding()


def split_text(text: str, size: int | None = None, overlap: int | None = None) -> list[str]:
    """Token-window chunking with overlap."""
    size = size or settings.chunk_size
    overlap = settings.chunk_overlap if overlap is None else overlap
    if overlap >= size:
        raise ValueError("chunk_overlap must be smaller than chunk_size")
    tokens = _enc.encode(text)
    step = size - overlap
    return [
        _enc.decode(tokens[i : i + size])
        for i in range(0, max(len(tokens), 1), step)
        if tokens[i : i + size]
    ]


async def ingest_text(
    text: str,
    source: str,
    source_type: str = "text",
    metadata: dict | None = None,
    repo: ChunkRepository | None = None,
) -> str:
    """Ingest one document. Returns its doc_id."""
    repo = repo or ChunkRepository()
    doc_id = str(uuid.uuid4())
    pieces = split_text(text)
    if not pieces:
        return doc_id

    embeddings = await llm_client.embed(pieces)
    chunks = [
        Chunk(
            text=p, embedding=e, source=source, source_type=source_type,
            doc_id=doc_id, chunk_index=i, metadata=metadata or {},
        )
        for i, (p, e) in enumerate(zip(pieces, embeddings))
    ]
    # Relational DB first: if Qdrant fails, retrieval simply won't surface these rows.
    await repo.add_many(chunks)
    await vector_store.upsert(chunks)
    return doc_id
