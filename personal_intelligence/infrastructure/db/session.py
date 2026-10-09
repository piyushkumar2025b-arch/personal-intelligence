"""Async engine + session factory supporting both local SQLite (no Docker) and PostgreSQL."""

from __future__ import annotations
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from personal_intelligence.config.settings import settings

db_url = settings.effective_database_url

try:
    if db_url.startswith("sqlite"):
        # Ensure data directory exists for the SQLite database file
        db_file = db_url.split("sqlite+aiosqlite:///")[-1].split("?")[0]
        if db_file:
            Path(db_file).parent.mkdir(parents=True, exist_ok=True)
        else:
            Path("./data").mkdir(parents=True, exist_ok=True)
        engine = create_async_engine(
            db_url,
            connect_args={"check_same_thread": False},
        )
    else:
        engine = create_async_engine(db_url, pool_size=10, pool_pre_ping=True)

    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
except Exception:
    engine = None  # type: ignore
    session_factory = None  # type: ignore
