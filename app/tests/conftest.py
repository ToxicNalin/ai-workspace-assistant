import shutil
from collections.abc import AsyncGenerator, Iterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from app.config import get_settings
from app.database.session import engine, get_db
from app.main import app


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
