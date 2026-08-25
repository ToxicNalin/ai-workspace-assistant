"""The LangGraph checkpointer.

A pending approval has to survive the process that created it. On Render's free
tier the service spins down after about fifteen minutes idle, and an approval
waiting on a human will routinely outlive that -- so the graph's state lives in
Postgres, in the same Neon database as everything else (SPEC-v2 §5). No extra
service, and an approval left overnight is still there in the morning.
"""

import logging

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver

from app.config import get_settings

logger = logging.getLogger(__name__)

_saver: BaseCheckpointSaver | None = None  # type: ignore[type-arg]
_pool: object | None = None


def _psycopg_conninfo(database_url: str) -> str:
    """AsyncPostgresSaver speaks psycopg3; the rest of the app speaks asyncpg.
    Same database, different driver prefix."""
    return database_url.replace("postgresql+asyncpg://", "postgresql://")


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
