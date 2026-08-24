import asyncio
import contextlib
import logging
import uuid

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.provider import get_embedder
from app.constants import INGESTION_HEARTBEAT_SECONDS, INGESTION_POLL_SECONDS
from app.storage.base import get_object_store
from app.workers import queue
from app.workers.jobs.ingest_document import ingest_document

logger = logging.getLogger(__name__)


class IngestionRunner:
    """The asyncio task that drains `ingestion_jobs`, started in the lifespan.

    It lives inside the API process — the one architectural compromise SPEC-v2
    §3 makes openly, because no free tier offers a persistent worker. Every
    property that makes that survivable (leases, reclaim, bounded attempts)
    lives in app/workers/queue.py.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        poll_seconds: float = INGESTION_POLL_SECONDS,
    ) -> None:
        self._session_factory = session_factory
        self._poll_seconds = poll_seconds
        self._stopping = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._stopping.clear()
        self._task = asyncio.create_task(self._loop(), name="ingestion-runner")
        logger.info("ingestion runner started")

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is None:
            return

        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None
        logger.info("ingestion runner stopped")

    async def _loop(self) -> None:
        while not self._stopping.is_set():
            try:
                did_work = await self._drain_one()
            except asyncio.CancelledError:
                raise
            except Exception:
                # A crash in one job must never take the loop down with it —
                # on the free tier this task is the only worker there is.
                logger.exception("ingestion runner iteration failed")
                did_work = False

            if not did_work:
                await self._wait(self._poll_seconds)

    async def _wait(self, seconds: float) -> None:
        """Sleep, but wake immediately when asked to stop."""
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stopping.wait(), timeout=seconds)

    async def _drain_one(self) -> bool:
        async with self._session_factory() as db:
            job = await queue.claim_next(db)
            if job is None:
                return False
            job_id, document_id = job.id, job.document_id

        logger.info("ingestion job claimed", extra={"job_id": str(job_id)})
        heartbeat = asyncio.create_task(self._heartbeat(job_id), name=f"heartbeat-{job_id}")

        try:
            async with self._session_factory() as db:
                await ingest_document(
                    db, get_object_store(), get_embedder(), document_id=document_id
                )
            async with self._session_factory() as db:
                await queue.complete(db, job_id)
                await queue.purge_terminal_jobs(db, document_id)
                await db.commit()
        except asyncio.CancelledError:
            # Shutting down mid-job. Hand it back so the next process picks it
            # up at once rather than waiting out the lease.
            async with self._session_factory() as db:
                await queue.release(db, job_id)
            raise
        except Exception as exc:
            async with self._session_factory() as db:
                given_up = await queue.fail(db, job_id, error=f"{type(exc).__name__}: {exc}")
            logger.exception(
                "ingestion job failed", extra={"job_id": str(job_id), "given_up": given_up}
            )
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat

        return True

    async def _heartbeat(self, job_id: uuid.UUID) -> None:
        while True:
            await asyncio.sleep(INGESTION_HEARTBEAT_SECONDS)
            async with self._session_factory() as db:
                await queue.heartbeat(db, job_id)
