import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.constants import IngestionJobStatus
from app.database.base import Base
from app.database.mixins import UUIDPrimaryKey, WorkspaceScoped
from app.database.types import str_enum


class IngestionJob(Base, UUIDPrimaryKey, WorkspaceScoped):
    """The Postgres-backed job queue that replaced Celery (SPEC-v2 D11).

    Claimed with FOR UPDATE SKIP LOCKED and held under a `lease_until`
    heartbeat, so a process killed mid-job leaves a row that the next process
    to boot reclaims rather than a job that silently vanishes.
    """

    __tablename__ = "ingestion_jobs"
    __table_args__ = (
        Index("ix_ingestion_jobs_status_lease_until", "status", "lease_until"),
        # One live job per document. Re-uploading or re-enqueueing while a job
        # is already queued or running must not fan out into duplicate
        # embedding calls, which cost real quota. Terminal rows (done/failed)
        # are outside the predicate, so a retry can always be enqueued.
        Index(
            "uq_ingestion_jobs_document_id_active",
            "document_id",
            unique=True,
            postgresql_where=text("status IN ('pending', 'running')"),
        ),
    )

    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"))
    status: Mapped[IngestionJobStatus] = mapped_column(
        str_enum(IngestionJobStatus, name="ingestion_job_status"),
        default=IngestionJobStatus.PENDING,
    )
    attempts: Mapped[int] = mapped_column(default=0, server_default="0")
    # NULL whenever the job is not claimed. Always set from the database
    # clock, never the application's — see app/workers/queue.py.
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    last_error: Mapped[str | None] = mapped_column(String(2000), default=None)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
