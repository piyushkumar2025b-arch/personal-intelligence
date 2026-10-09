"""SQLAlchemy models. Postgres is the source of truth for chunk text."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Index, Integer, JSON, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class ChunkRow(Base):
    __tablename__ = "chunks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    doc_id: Mapped[str] = mapped_column(String(64), default="")
    text: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text, default="")
    source_type: Mapped[str] = mapped_column(String(32), default="text")
    chunk_index: Mapped[int] = mapped_column(Integer, default=0)
    # "metadata" is reserved on DeclarativeBase, so the attribute is `meta`.
    # Uses JSONB on PostgreSQL for indexability, standard JSON on SQLite.
    meta: Mapped[dict[str, Any]] = mapped_column(
        "metadata",
        JSON().with_variant(JSONB, "postgresql"),
        default=dict,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("ix_chunks_doc_id", "doc_id"),
        Index("ix_chunks_created_at", "created_at"),
        Index("ix_chunks_source_type", "source_type"),
    )
