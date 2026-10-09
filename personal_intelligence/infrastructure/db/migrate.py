"""Create tables. Run: python -m personal_intelligence.infrastructure.db.migrate

This uses create_all for a fresh install. Switch to Alembic once the schema starts changing.
"""

import asyncio

from personal_intelligence.infrastructure.db.models import Base
from personal_intelligence.infrastructure.db.session import engine


async def main() -> None:
    from personal_intelligence.config.settings import settings
    print(f"Initializing database: {settings.effective_database_url}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()
    print("Database schema is up to date.")


if __name__ == "__main__":
    asyncio.run(main())
