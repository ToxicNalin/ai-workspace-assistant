import uuid

from sqlalchemy import and_, case, delete, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants import (
    INGESTION_LEASE_SECONDS,
    MAX_INGESTION_ATTEMPTS,
    DocumentStatus,
    IngestionJobStatus,
)
from app.database.models.document import Document
from app.database.models.ingestion_job import IngestionJob

# Always the database clock, never datetime.now() in Python: the reclaim check
# below compares lease_until against now() inside Postgres, and comparing an
# application clock to a database clock is how leases expire early or never.
# INGESTION_LEASE_SECONDS is a module constant int, not input.
_LEASE_EXPIRY = text(f"now() + interval '{INGESTION_LEASE_SECONDS} seconds'")


async def enqueue(db: AsyncSession, *, workspace_id: uuid.UUID, document_id: uuid.UUID) -> None:
    """Queue a document for ingestion. Does not commit — the caller commits the
    job and whatever it belongs to in one transaction.

    A document that already has a live job is left alone: the partial unique
    index on `document_id` makes a second insert a no-op rather than a
    duplicate that would embed the same text twice.
    """
    await db.execute(
        pg_insert(IngestionJob)
        .values(
            workspace_id=workspace_id,
            document_id=document_id,
            status=IngestionJobStatus.PENDING.value,
        )
        .on_conflict_do_nothing()
    )


async def claim_next(db: AsyncSession) -> IngestionJob | None:
    """Atomically take the oldest available job, or return None.

    SKIP LOCKED is what lets several processes drain the same table without
    ever colliding, so this scales to multiple replicas unchanged (SPEC-v2 §6).
    The second WHERE arm is the crash recovery: a job whose lease has run out
    is one whose worker died, and it becomes claimable again.
    """
    candidate = (
        select(IngestionJob.id)
        .where(
            or_(
                IngestionJob.status == IngestionJobStatus.PENDING,
                and_(
                    IngestionJob.status == IngestionJobStatus.RUNNING,
                    IngestionJob.lease_until < text("now()"),
                ),
            )
        )
        .order_by(IngestionJob.created_at)
        .limit(1)
        .with_for_update(skip_locked=True)
        .scalar_subquery()
    )

    result = await db.execute(
        update(IngestionJob)
        .where(IngestionJob.id == candidate)
        .values(
            status=IngestionJobStatus.RUNNING.value,
            lease_until=_LEASE_EXPIRY,
            attempts=IngestionJob.attempts + 1,
        )
        .returning(IngestionJob)
        # populate_existing is not optional here: without it, a session that
        # has already loaded this job hands back its stale copy rather than
        # the values RETURNING just produced, so a reclaimed job would report
        # the attempt count it had before it was reclaimed.
        .execution_options(synchronize_session=False, populate_existing=True)
    )
    job = result.scalar_one_or_none()

    # Committed immediately and deliberately: until this lands, no other
    # process can see that the job is taken.
    await db.commit()
    return job


async def heartbeat(db: AsyncSession, job_id: uuid.UUID) -> None:
    """Push the lease out while the job is still being worked on."""
    await db.execute(
        update(IngestionJob).where(IngestionJob.id == job_id).values(lease_until=_LEASE_EXPIRY)
    )
    await db.commit()


async def complete(db: AsyncSession, job_id: uuid.UUID) -> None:
    await db.execute(
        update(IngestionJob)
        .where(IngestionJob.id == job_id)
        .values(status=IngestionJobStatus.DONE.value, lease_until=None, last_error=None)
    )
    await db.commit()


async def release(db: AsyncSession, job_id: uuid.UUID) -> None:
    """Hand a claimed job straight back, without counting it as a failure.

    Used on graceful shutdown so a job interrupted by a deploy is picked up
    immediately instead of waiting out its whole lease. `attempts` stays
    incremented — a job that keeps landing on shutdowns should still
    eventually give up rather than loop forever.
    """
    await db.execute(
        update(IngestionJob)
        .where(IngestionJob.id == job_id)
        .values(status=IngestionJobStatus.PENDING.value, lease_until=None)
    )
    await db.commit()


async def fail(db: AsyncSession, job_id: uuid.UUID, *, error: str) -> bool:
    """Record a failed attempt. Returns True if the job has now given up.

    Whether this was the last attempt is decided in SQL against the row's own
    `attempts`, so two processes racing on the same job cannot both read a
    stale count and disagree. On the final attempt the document is marked
    failed with `error_message` set, which is what the UI shows.
    """
    result = await db.execute(
        update(IngestionJob)
        .where(IngestionJob.id == job_id)
        .values(
            status=case(
                (
                    IngestionJob.attempts >= MAX_INGESTION_ATTEMPTS,
                    IngestionJobStatus.FAILED.value,
                ),
                else_=IngestionJobStatus.PENDING.value,
            ),
            lease_until=None,
            last_error=error[:2000],
        )
        .returning(IngestionJob.status, IngestionJob.document_id)
        .execution_options(synchronize_session=False)
    )
    row = result.one_or_none()
    if row is None:
        await db.commit()
        return False

    status, document_id = row
    given_up = bool(status == IngestionJobStatus.FAILED)
    if given_up:
        await db.execute(
            update(Document)
            .where(Document.id == document_id)
            .values(status=DocumentStatus.FAILED.value, error_message=error[:1000])
        )

    await db.commit()
    return given_up


async def purge_terminal_jobs(db: AsyncSession, document_id: uuid.UUID) -> None:
    """Clear out done/failed rows for a document so a fresh job can be queued.

    The partial unique index only covers live jobs, so this is not required for
    correctness — it just keeps the table from accumulating one dead row per
    re-ingest of the same document.
    """
    await db.execute(
        delete(IngestionJob).where(
            IngestionJob.document_id == document_id,
            IngestionJob.status.in_([IngestionJobStatus.DONE, IngestionJobStatus.FAILED]),
        )
    )
