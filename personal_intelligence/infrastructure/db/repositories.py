"""Data access for chunks. Returns the same `Chunk` dataclass the vector store uses."""

from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from personal_intelligence.core.memory.vector_store import Chunk
from personal_intelligence.infrastructure.db.models import ChunkRow
from personal_intelligence.infrastructure.db.session import session_factory


def _to_chunk(row: ChunkRow) -> Chunk:
    return Chunk(
        id=row.id,
        text=row.text,
        source=row.source,
        source_type=row.source_type,
        doc_id=row.doc_id,
        chunk_index=row.chunk_index,
        metadata=row.meta or {},
    )


class ChunkRepository:
    def __init__(self, factory: async_sessionmaker[AsyncSession] | None = None) -> None:
        if factory is not None:
            self._factory = factory
        elif session_factory is not None:
            self._factory = session_factory
        else:
            raise RuntimeError(
                "Database driver not installed. For SQLite: pip install aiosqlite; for Postgres: pip install asyncpg"
            )

    async def add_many(self, chunks: list[Chunk]) -> None:
        async with self._factory() as session, session.begin():
            session.add_all(
                ChunkRow(
                    id=c.id, doc_id=c.doc_id, text=c.text, source=c.source,
                    source_type=c.source_type, chunk_index=c.chunk_index, meta=c.metadata,
                )
                for c in chunks
            )

    async def get_by_ids(self, ids: list[str]) -> dict[str, Chunk]:
        if not ids:
            return {}
        async with self._factory() as session:
            rows = (await session.scalars(select(ChunkRow).where(ChunkRow.id.in_(ids)))).all()
        return {r.id: _to_chunk(r) for r in rows}

    async def get_recent(self, limit: int = 2000, source_type: str | None = None) -> list[Chunk]:
        stmt = select(ChunkRow).order_by(ChunkRow.created_at.desc()).limit(limit)
        if source_type:
            stmt = stmt.where(ChunkRow.source_type == source_type)
        async with self._factory() as session:
            rows = (await session.scalars(stmt)).all()
        return [_to_chunk(r) for r in rows]

    async def delete_by_doc(self, doc_id: str) -> None:
        async with self._factory() as session, session.begin():
            await session.execute(delete(ChunkRow).where(ChunkRow.doc_id == doc_id))
