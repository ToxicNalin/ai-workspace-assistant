from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    environment: Literal["local", "test", "production"] = "local"
    log_level: str = "INFO"
    cors_origins: list[str] = []

    # Matches docker-compose's local db service. Render's dashboard sets the
    # real Neon pooled connection string in production — never committed.
    database_url: str = "postgresql+asyncpg://app:app@localhost:5432/app"

    # Dev-only defaults below — insecure on purpose so a fresh clone works with
    # zero setup. Render's dashboard sets real values in production.
    jwt_secret: str = "dev-only-insecure-secret-change-in-production"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 15
    refresh_token_expire_days: int = 30

    # A valid Fernet key (Fernet.generate_key()), not a placeholder string —
    # Fernet() rejects anything that isn't 32 url-safe base64 bytes.
    fernet_key: str = "10IAZaiwQMOJNknjWUejIEMlNgV2tEj9Chj22uyWOoE="

    # local | r2 -- local is filesystem-backed, for tests and local dev; r2 is
    # the production Cloudflare R2 implementation, both behind ObjectStore.
    storage_backend: Literal["local", "r2"] = "local"
    local_storage_dir: str = ".data/uploads"

    r2_bucket: str = "ai-workspace-assistant-dev"
    r2_endpoint_url: str = ""
    r2_access_key_id: str = ""
    r2_secret_access_key: str = ""

    # fake | gemini -- fake is a deterministic offline embedder needing no API
    # key and no network, so a fresh clone and CI both ingest documents end to
    # end. Production sets gemini.
    embedding_provider: Literal["fake", "gemini"] = "fake"
    # Pinned deliberately rather than left to float: gemini-embedding-2
    # renormalises automatically at non-default dimensionalities, which
    # gemini-embedding-001 did not — truncating without renormalising is a
    # silent recall bug (SPEC-v2 §5).
    embedding_model: str = "gemini-embedding-2"
    google_api_key: str = ""

    # fake | gemini -- same split as the embedder. fake answers from the
    # retrieved context deterministically, so the whole RAG path is testable
    # without a key, a network call or a bill.
    llm_provider: Literal["fake", "gemini"] = "fake"
    # Passed to langchain's init_chat_model, which is provider-agnostic --
    # swapping to OpenAI is this one string (SPEC-v2 D18).
    llm_model: str = "google_genai:gemini-2.5-flash"

    # memory | postgres -- where LangGraph keeps the agent's paused state.
    # Production uses postgres so an approval left overnight survives the free
    # tier spinning down. Tests use memory: the checkpointer commits on its own
    # connections, outside the per-test rollback, so a Postgres one would
    # accumulate checkpoint rows in the database run after run.
    agent_checkpointer: Literal["memory", "postgres"] = "memory"

    # Migrations and the ingestion worker both run inside the API process
    # (SPEC-v2 §3, §6). On by default so `docker compose up` is the whole of
    # local setup; the lifespan is the only thing that reads these, and the
    # test client never triggers it.
    run_migrations_on_startup: bool = True
    ingestion_worker_enabled: bool = True


@lru_cache
def get_settings() -> Settings:
    return Settings()
