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

    # local | s3 -- local is filesystem-backed, for tests and local dev. s3 is
    # any S3-compatible object store, both behind the same ObjectStore
    # protocol.
    #
    # SPEC-v2 D12 chose Cloudflare R2 for its permanently free 10 GB and zero
    # egress. R2 asks for a card on signup, which a student project should not
    # have to give, so the shipped deployment points the same S3 client at
    # Supabase Storage instead. Nothing in the code knows the difference --
    # that is what the endpoint and region being configuration rather than
    # constants buys, and swapping back to R2 is four values in a dashboard.
    storage_backend: Literal["local", "s3"] = "local"
    local_storage_dir: str = ".data/uploads"

    s3_bucket: str = "ai-workspace-assistant-dev"
    s3_endpoint_url: str = ""
    s3_access_key_id: str = ""
    s3_secret_access_key: str = ""
    # R2 ignores the region and wants the literal "auto"; Supabase and AWS
    # want the project's real one, and signing fails if it is wrong.
    s3_region: str = "auto"

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
    #
    # Not the gemini-2.5-flash the spec pinned: Google now returns 404 for it
    # on newly issued keys ("no longer available to new users"), so a fresh
    # clone following the spec would fail on its first real chat call. 3.6 is
    # what that deprecation notice points at, and it is verified working.
    llm_model: str = "google_genai:gemini-3.6-flash"

    # console | resend -- console records the message and logs it instead of
    # sending, so a fresh clone can drive an approved action end to end with
    # no API key, no network and nothing arriving in a stranger's inbox.
    # Production sets resend (SPEC-v2 D16: gmail.send is a restricted scope).
    email_provider: Literal["console", "resend"] = "console"
    resend_api_key: str = ""
    # Resend will only send from a verified domain, with the exception of its
    # own onboarding sender -- which is what makes a £0 demo possible.
    email_from_address: str = "onboarding@resend.dev"
    email_from_name: str = "AI Workspace Assistant"

    # ics | google -- ics builds an RFC 5545 invitation and attaches it to the
    # notification email, which works for every recipient with no OAuth at
    # all. google is declared behind the same interface and deliberately not
    # shipped: calendar.events is a sensitive scope with multi-week
    # verification (SPEC-v2 D17, and the non-goals in §9).
    calendar_provider: Literal["ics", "google"] = "ics"

    # memory | postgres -- where LangGraph keeps the agent's paused state.
    # Production uses postgres so an approval left overnight survives the free
    # tier spinning down. Tests use memory: the checkpointer commits on its own
    # connections, outside the per-test rollback, so a Postgres one would
    # accumulate checkpoint rows in the database run after run.
    agent_checkpointer: Literal["memory", "postgres"] = "memory"

    # --- Step 8: limits and budgets --------------------------------------
    #
    # A public demo holding a live LLM key is an open invitation (SPEC-v2 §7),
    # so both of these are on by default. The test suite turns the request
    # limiter off in conftest: every test shares one client address, so a
    # per-IP cap would make the suite's result depend on how many tests ran
    # before it.
    rate_limit_enabled: bool = True
    # Per authenticated user, or per IP for anyone not logged in.
    rate_limit_requests_per_minute: int = 60
    # Per IP, per hour, on registration alone. Deliberately small: the honest
    # ceiling on how many accounts one person needs is about one.
    rate_limit_registrations_per_hour: int = 5

    # Requests per minute bounds traffic, not spend -- one chat request can
    # fan out into several model calls (SPEC-v2 D22). This is the cap that
    # actually stops a stranger running up a bill, counted per workspace over
    # a rolling 24 hours and checked *before* the model is invoked.
    daily_token_budget: int = 200_000

    # --- Step 9: the browser client (SPEC-v2 D19) ------------------------
    #
    # The SPA is served from Cloudflare Pages and the API from Render, which
    # are different sites, so the refresh cookie has to be `SameSite=None` --
    # and a browser only honours `SameSite=None` together with `Secure`, which
    # needs HTTPS. Neither is usable over plain http://localhost, so the two
    # environments genuinely need different values.
    #
    # None means "decide from `environment`". Set them explicitly only to
    # rehearse the cross-site arrangement locally behind an HTTPS tunnel.
    refresh_cookie_secure: bool | None = None
    refresh_cookie_samesite: Literal["lax", "strict", "none"] | None = None

    # Migrations and the ingestion worker both run inside the API process
    # (SPEC-v2 §3, §6). On by default so `docker compose up` is the whole of
    # local setup; the lifespan is the only thing that reads these, and the
    # test client never triggers it.
    run_migrations_on_startup: bool = True
    ingestion_worker_enabled: bool = True

    @property
    def cookie_secure(self) -> bool:
        return (
            self.refresh_cookie_secure
            if self.refresh_cookie_secure is not None
            else self.environment == "production"
        )

    @property
    def cookie_samesite(self) -> Literal["lax", "strict", "none"]:
        if self.refresh_cookie_samesite is not None:
            return self.refresh_cookie_samesite
        return "none" if self.environment == "production" else "lax"


@lru_cache
def get_settings() -> Settings:
    return Settings()
