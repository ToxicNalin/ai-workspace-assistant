import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base
from app.database.mixins import UUIDPrimaryKey, WorkspaceScoped


class AuditLogEntry(Base, UUIDPrimaryKey, WorkspaceScoped):
    """Append-only. Nothing in the application updates or deletes a row here.

    `details` is jsonb rather than text so the log is queryable -- "show me
    every action refused in this workspace" is a question worth being able to
    ask of a security log.
    """

    __tablename__ = "audit_log"
    __table_args__ = (
        Index("ix_audit_log_workspace_id_created_at", "workspace_id", "created_at"),
    )

    # Nullable: the server refusing an action on its own behalf is not
    # attributable to a person.
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    action: Mapped[str] = mapped_column(String(100), index=True)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
