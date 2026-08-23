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


@lru_cache
def get_settings() -> Settings:
    return Settings()
