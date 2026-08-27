import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from sqlalchemy import Connection
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.ai.agent.checkpointer import close_checkpointer, setup_checkpointer
from app.config import get_settings
from app.database.session import async_session_factory
from app.services import email_service
from app.workers.runner import IngestionRunner

logger = logging.getLogger(__name__)

# An arbitrary but fixed key. Postgres advisory locks share one namespace per
# database, so this only needs to be a value nothing else in this application
# uses.
MIGRATION_ADVISORY_LOCK_KEY = 728_193_641


def _upgrade_to_head(connection: Connection) -> None:
    config = Config("alembic.ini")
    # Handing env.py the live connection keeps the migration inside the same
    # session that holds the advisory lock below.
    config.attributes["connection"] = connection
    command.upgrade(config, "head")


async def run_migrations() -> None:
    """`alembic upgrade head` on boot, guarded by an advisory lock.

    With one Render instance the lock is redundant; with two it is the
    difference between a clean deploy and two processes running the same
    CREATE INDEX concurrently (SPEC-v2 §7, gotcha 5). NullPool so the
    connection is genuinely closed afterwards, which releases the
    session-level lock even if the unlock below never runs.
    """
    settings = get_settings()
    engine = create_async_engine(settings.database_url, poolclass=NullPool)

    try:
        async with engine.connect() as connection:
            await connection.exec_driver_sql(
                f"SELECT pg_advisory_lock({MIGRATION_ADVISORY_LOCK_KEY})"
            )
            try:
                await connection.run_sync(_upgrade_to_head)
            finally:
                await connection.exec_driver_sql(
                    f"SELECT pg_advisory_unlock({MIGRATION_ADVISORY_LOCK_KEY})"
                )
    finally:
        await engine.dispose()

    logger.info("migrations up to date")


def _warn_if_mail_is_undeliverable() -> None:
    """Say at boot what would otherwise be found out by a missing email.

    Not fatal: a deployment that cannot send mail still serves chat, documents
    and retrieval perfectly well, and refusing to start would be a worse
    trade than a line in the log. The line is the point -- an unset
    `EMAIL_PROVIDER` is invisible everywhere else, because falling back to the
    console provider is exactly what the local default is supposed to do.
    """
    reason = email_service.undeliverable_reason()
    if reason is None:
        return

    settings = get_settings()
    if settings.environment == "production":
        logger.warning("this deployment cannot send email: %s", reason)
    else:
        logger.info("email is not being delivered: %s", reason)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()

    _warn_if_mail_is_undeliverable()

    if settings.run_migrations_on_startup:
        await run_migrations()

    # Creates the LangGraph checkpoint tables if they are not there yet. Done
    # at boot rather than lazily so the first person to use the agent does not
    # pay for a DDL round trip, and so a broken database fails the deploy
    # rather than one request.
    await setup_checkpointer()

    runner: IngestionRunner | None = None
    if settings.ingestion_worker_enabled:
        runner = IngestionRunner(async_session_factory)
        runner.start()

    try:
        yield
    finally:
        if runner is not None:
            await runner.stop()
        await close_checkpointer()
