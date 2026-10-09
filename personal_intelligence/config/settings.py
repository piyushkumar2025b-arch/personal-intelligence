"""Central configuration — loaded once at startup, everywhere imported from here."""

from functools import lru_cache
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # ── OpenRouter ───────────────────────────────────────────────
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_default_model: str = "nvidia/nemotron-3-super-120b-a12b:free"
    openrouter_fast_model: str = "liquid/lfm-2.5-2.6b:free"
    openrouter_embedding_model: str = "openai/text-embedding-3-small"
    openrouter_site_url: str = "https://localhost"
    openrouter_site_name: str = "PersonalIntelligence"

    # ── Database (Relational) ────────────────────────────────────
    # Defaults to local SQLite (data/personal_intelligence.db) with zero Docker setup.
    # Set DATABASE_URL to a PostgreSQL URI (e.g., Supabase, Neon) to use Postgres.
    database_url: str = "sqlite+aiosqlite:///./data/personal_intelligence.db"
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_db: str = "personal_intelligence"
    postgres_user: str = "pi_user"
    postgres_password: str = ""

    @property
    def effective_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        if self.postgres_password:
            return (
                f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
                f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
            )
        return "sqlite+aiosqlite:///./data/personal_intelligence.db"

    @property
    def postgres_dsn(self) -> str:
        """Backwards-compatible alias for effective_database_url."""
        return self.effective_database_url

    # ── Neo4j (Graph DB) ─────────────────────────────────────────
    # For zero-Docker setup, use Neo4j AuraDB (cloud free tier: neo4j+s://...)
    # or leave offline (the retriever continues gracefully with Vector + BM25).
    neo4j_enabled: bool = True
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "changeme123"

    # ── Qdrant (Vector Store) ────────────────────────────────────
    # Mode 1: Embedded local storage on disk (zero Docker needed): qdrant_path="./data/qdrant"
    # Mode 2: Qdrant Cloud (cloud free tier): qdrant_url="https://xyz.qdrant.io", qdrant_api_key="..."
    # Mode 3: Self-hosted server: qdrant_host="localhost", qdrant_port=6333
    qdrant_path: str | None = "./data/qdrant"
    qdrant_url: str | None = None
    qdrant_api_key: str | None = None
    qdrant_host: str = "localhost"
    qdrant_port: int = 6333
    qdrant_collection_name: str = "personal_knowledge"
    embedding_dim: int = 1536  # text-embedding-3-small

    # ── Redis (Cache / Rate limiting) ────────────────────────────
    # Optional. For cloud setup, use Upstash Redis (rediss://...)
    redis_enabled: bool = False
    redis_custom_url: str | None = None
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_password: str = "changeme123"

    @property
    def redis_url(self) -> str:
        if self.redis_custom_url:
            return self.redis_custom_url
        return f"redis://:{self.redis_password}@{self.redis_host}:{self.redis_port}/0"

    # ── Telegram ─────────────────────────────────────────────────
    telegram_bot_token: str = ""
    telegram_allowed_user_ids: list[int] | str = Field(default_factory=list)

    @field_validator("telegram_allowed_user_ids", mode="before")
    @classmethod
    def parse_user_ids(cls, v):
        if not v:
            return []
        if isinstance(v, str):
            return [int(x.strip()) for x in v.split(",") if x.strip()]
        return v

    # ── API ──────────────────────────────────────────────────────
    api_secret_key: str = "change-me"
    api_host: str = "0.0.0.0"
    api_port: int = 8000

    # ── Ingestion ────────────────────────────────────────────────
    chunk_size: int = 512
    chunk_overlap: int = 64
    embedding_batch_size: int = 32

    # ── Agent ────────────────────────────────────────────────────
    max_retrieval_iterations: int = 3
    top_k_retrieval: int = 20
    top_k_rerank: int = 5
    agent_temperature: float = 0.1

    # ── Protection ───────────────────────────────────────────────
    max_input_tokens: int = 4000
    max_output_tokens: int = 2000
    rate_limit_requests_per_minute: int = 20
    rate_limit_tokens_per_day: int = 500_000

    # ── Monitoring ───────────────────────────────────────────────
    log_level: str = "INFO"
    sentry_dsn: str = ""

    # ── Reranker ─────────────────────────────────────────────────
    reranker_model: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]


settings = get_settings()
