import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.constants import DocumentStatus
from app.database.base import Base
from app.database.mixins import UUIDPrimaryKey, WorkspaceScoped
from app.database.types import str_enum


class Document(Base, UUIDPrimaryKey, WorkspaceScoped):
    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint("workspace_id", "content_hash"),
        Index("ix_documents_workspace_id_uploaded_at", "workspace_id", "uploaded_at"),
    )

    name: Mapped[str] = mapped_column(String(255))
    storage_key: Mapped[str] = mapped_column(String(512))
    # SHA-256 hex digest of the file content -- the basis for dedup within a
    # workspace, not the filename.
    content_hash: Mapped[str] = mapped_column(String(64))
    mime_type: Mapped[str] = mapped_column(String(255))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    uploaded_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    status: Mapped[DocumentStatus] = mapped_column(
        str_enum(DocumentStatus, name="document_status"), default=DocumentStatus.PENDING
    )
    chunk_count: Mapped[int] = mapped_column(default=0, server_default="0")
    error_message: Mapped[str | None] = mapped_column(String(1000), default=None)
    # Recorded on the row so a future re-index knows what produced the
    # existing chunks -- set once ingestion (Step 4) actually embeds it.
    embedding_model: Mapped[str | None] = mapped_column(String(255), default=None)
