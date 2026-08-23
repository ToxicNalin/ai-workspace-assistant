import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Uuid, func, text
from sqlalchemy.orm import Mapped, declared_attr, mapped_column


class UUIDPrimaryKey:
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, server_default=text("uuidv7()"))


class Timestamped:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class WorkspaceScoped:
    """Composed into every tenant-scoped model — never write a workspace_id filter by hand."""

    @declared_attr
    def workspace_id(cls) -> Mapped[uuid.UUID]:
        return mapped_column(
            Uuid, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
        )
