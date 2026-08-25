import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.constants import TaskStatus
from app.database.base import Base
from app.database.mixins import UUIDPrimaryKey, WorkspaceScoped
from app.database.types import str_enum


class Task(Base, UUIDPrimaryKey, WorkspaceScoped):
    """A unit of work in a workspace.

    Two of these columns come from SPEC-v2's decision log rather than from the
    obvious shape of a to-do:

    D3 -- `assigned_to` points at `users`, not at `workspace_members`.
    Membership rows are deleted and recreated when somebody leaves and rejoins,
    so a task keyed to a membership would either break or, worse, silently
    reattach to whoever inherited that row. The membership check is done in
    app/services/task_service.py at write time instead.

    D4 -- `source_message_id` is provenance: "this task came from that agent
    turn". Nullable and ON DELETE SET NULL, because a task outlives the
    conversation that suggested it.
    """

    __tablename__ = "tasks"
    __table_args__ = (
        Index("ix_tasks_workspace_id_status", "workspace_id", "status"),
        Index("ix_tasks_workspace_id_assigned_to", "workspace_id", "assigned_to"),
    )

    title: Mapped[str] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(Text, default="", server_default="")
    assigned_to: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    source_message_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("chat_messages.id", ondelete="SET NULL"), default=None
    )
    status: Mapped[TaskStatus] = mapped_column(
        str_enum(TaskStatus, name="task_status"), default=TaskStatus.TODO
    )
    due_date: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
