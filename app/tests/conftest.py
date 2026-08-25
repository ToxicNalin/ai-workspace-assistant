import os

# Set before anything imports app.config, and deliberately not `setdefault`:
# environment variables outrank the .env file in pydantic-settings, so this
# overrides whatever a developer has configured locally.
#
# Without it the suite's behaviour depends on the .env of whoever is running
# it. The moment a real GOOGLE_API_KEY lands in that file, `pytest` starts
# embedding every fixture against the live API -- slow, quota-burning, and
# flaky -- and a real RESEND_API_KEY would mean tests posting genuine email.
# Tests run against the offline providers, always, whatever the machine says.
_OFFLINE_PROVIDERS = {
    "EMBEDDING_PROVIDER": "fake",
    "LLM_PROVIDER": "fake",
    "EMAIL_PROVIDER": "console",
    "EMAIL_FROM_ADDRESS": "onboarding@resend.dev",
    "EMAIL_FROM_NAME": "AI Workspace Assistant",
    "STORAGE_BACKEND": "local",
    "CALENDAR_PROVIDER": "ics",
    # The checkpointer commits on its own connections, outside the per-test
    # rollback, so a Postgres one would accumulate rows run after run.
    "AGENT_CHECKPOINTER": "memory",
}
os.environ.update(_OFFLINE_PROVIDERS)

import shutil  # noqa: E402
from collections.abc import AsyncGenerator, Iterator  # noqa: E402

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.database.session import engine, get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.services.email_service import ConsoleEmailProvider  # noqa: E402


@pytest.fixture(autouse=True, scope="session")
def _clean_local_storage() -> Iterator[None]:
    storage_dir = get_settings().local_storage_dir
    shutil.rmtree(storage_dir, ignore_errors=True)
    yield
    shutil.rmtree(storage_dir, ignore_errors=True)


@pytest_asyncio.fixture
async def db_connection() -> AsyncGenerator[AsyncConnection, None]:
    async with engine.connect() as connection:
        yield connection


@pytest_asyncio.fixture
async def db_session(db_connection: AsyncConnection) -> AsyncGenerator[AsyncSession, None]:
    """Real Postgres, wrapped in an outer transaction that's always rolled
    back. Application code still calls session.commit() normally -- it just
    releases and reopens a SAVEPOINT rather than the outer transaction, so no
    test can ever leave data behind, whether it passes or fails.
    """
    outer_transaction = await db_connection.begin()
    # expire_on_commit=False must match app/database/session.py's factory --
    # otherwise objects go stale after a service's commit() and a later
    # *synchronous* attribute read (e.g. inside Pydantic's from_attributes)
    # trips SQLAlchemy's "greenlet_spawn has not been called" guard trying to
    # lazy-refresh them outside of an awaited call.
    session = AsyncSession(
        bind=db_connection, join_transaction_mode="create_savepoint", expire_on_commit=False
    )

    try:
        yield session
    finally:
        await session.close()
        await outer_transaction.rollback()


@pytest_asyncio.fixture
async def client(db_session: AsyncSession) -> AsyncGenerator[AsyncClient, None]:
    async def override_get_db() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    transport = ASGITransport(app=app)

    try:
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac
    finally:
        app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def outbox(monkeypatch: pytest.MonkeyPatch) -> ConsoleEmailProvider:
    """A fresh mail recorder for one test, injected where the route reads it.

    Autouse: no test should reach the process-wide provider, whose outbox would
    otherwise accumulate across the whole session. Tests that assert on what
    was sent just name the fixture.

    Patched at the call site rather than reconfiguring the provider globally:
    get_email_provider() is lru_cached, so a settings-level switch would leak
    the same instance -- and its accumulated outbox -- into every later test.
    """
    provider = ConsoleEmailProvider()
    monkeypatch.setattr("app.api.approvals.get_email_provider", lambda: provider)
    return provider
