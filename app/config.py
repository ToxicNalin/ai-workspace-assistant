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


@lru_cache
def get_settings() -> Settings:
    return Settings()
