"""The LangGraph checkpointer.

A pending approval has to survive the process that created it. On Render's free
tier the service spins down after about fifteen minutes idle, and an approval
waiting on a human will routinely outlive that -- so the graph's state lives in
Postgres, in the same Neon database as everything else (SPEC-v2 §5). No extra
service, and an approval left overnight is still there in the morning.
"""

import asyncio
import logging
import sys
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver

from app.config import get_settings

logger = logging.getLogger(__name__)

_saver: BaseCheckpointSaver | None = None  # type: ignore[type-arg]
_pool: object | None = None


# asyncpg and libpq spell the same connection options differently, and Neon
# hands out a URL written for whichever driver you told it about. Swapping the
# scheme is not enough: psycopg rejects the whole URI on an unknown query
# parameter, so `?ssl=require` fails to connect rather than being ignored.
_QUERY_TRANSLATIONS: dict[str, str] = {"ssl": "sslmode"}
_SSL_VALUE_TRANSLATIONS: dict[str, str] = {"true": "require", "false": "disable"}


def _psycopg_conninfo(database_url: str) -> str:
    """Rewrite the app's asyncpg URL into one libpq will accept.

    AsyncPostgresSaver speaks psycopg3 while the rest of the app speaks
    asyncpg, so the same database is reached through two drivers that disagree
    about parameter names.
    """
    parts = urlsplit(database_url)
    scheme = parts.scheme.split("+", 1)[0]

    translated: list[tuple[str, str]] = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if key == "ssl":
            value = _SSL_VALUE_TRANSLATIONS.get(value.lower(), value)
        translated.append((_QUERY_TRANSLATIONS.get(key, key), value))

    return urlunsplit(
        (scheme, parts.netloc, parts.path, urlencode(translated), parts.fragment)
    )


def _require_compatible_event_loop() -> None:
    """Fail fast and legibly on Windows' default event loop.

    psycopg's async mode cannot run on ProactorEventLoop, which is what Python
    uses by default on Windows. Left alone this surfaces thirty seconds later
    as a PoolTimeout behind a wall of connection warnings, which says nothing
    about the actual cause. Linux -- and therefore Render -- uses
    SelectorEventLoop already, so this only ever fires in local development.
    """
    if sys.platform != "win32":
        return

    loop = asyncio.get_running_loop()
    if isinstance(loop, asyncio.SelectorEventLoop):
        return

    raise RuntimeError(
        "AGENT_CHECKPOINTER=postgres needs a SelectorEventLoop, but this process "
        "is running Windows' default ProactorEventLoop, which psycopg cannot use "
        "asynchronously. Either set AGENT_CHECKPOINTER=memory for local "
        "development, or start the process with "
        "asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy()) "
        "before the server starts. Deployments run on Linux and are unaffected."
    )


async def setup_checkpointer() -> BaseCheckpointSaver:  # type: ignore[type-arg]
    """Build the checkpointer and create its tables if they do not exist.

    The checkpoint tables are the one part of the schema not under Alembic:
    they belong to LangGraph, their layout changes with its version, and
    hand-writing a migration for someone else's internal tables would break on
    the next upgrade. `setup()` is idempotent (SPEC-v2 table 17).
    """
    global _saver, _pool

    if _saver is not None:
        return _saver

    settings = get_settings()

    if settings.agent_checkpointer == "memory":
        _saver = InMemorySaver()
        return _saver

    _require_compatible_event_loop()

    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from psycopg_pool import AsyncConnectionPool

    pool = AsyncConnectionPool(
        conninfo=_psycopg_conninfo(settings.database_url),
        # Tiny, like the SQLAlchemy pool: 512 MB of RAM and Neon's connection
        # budget are shared with the rest of the app.
        min_size=0,
        max_size=2,
        open=False,
        kwargs={
            "autocommit": True,
            # Neon's pooled endpoint is PgBouncer in transaction mode, which
            # cannot carry psycopg's server-side prepared statements across
            # checkouts. Without this the saver fails intermittently and
            # confusingly under load.
            "prepare_threshold": None,
        },
    )
    await pool.open()

    saver = AsyncPostgresSaver(pool)  # type: ignore[arg-type]
    await saver.setup()

    _pool, _saver = pool, saver
    logger.info("agent checkpointer ready", extra={"backend": "postgres"})
    return saver


async def get_checkpointer() -> BaseCheckpointSaver:  # type: ignore[type-arg]
    return await setup_checkpointer()


async def close_checkpointer() -> None:
    global _saver, _pool

    if _pool is not None:
        await _pool.close()  # type: ignore[attr-defined]

    _saver, _pool = None, None
