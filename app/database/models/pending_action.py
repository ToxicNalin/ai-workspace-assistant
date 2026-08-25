import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.constants import PendingActionStatus, PendingActionType
from app.database.base import Base
from app.database.mixins import UUIDPrimaryKey, WorkspaceScoped
from app.database.types import str_enum


class PendingAction(Base, UUIDPrimaryKey, WorkspaceScoped):
    """A side-effecting action the agent proposed, waiting on a human.

    SPEC-v2 D20 is the reason `payload_hash` exists. Without it the approval
    gate is theatre: if the payload can change between "here is the email we
    will send" and "approved", then what the human agreed to and what the
    server executes are two different things. The hash binds them.
    """

    __tablename__ = "pending_actions"
    __table_args__ = (
        Index("ix_pending_actions_workspace_id_status", "workspace_id", "status"),
    )

    thread_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("chat_threads.id", ondelete="CASCADE")
    )
    type: Mapped[PendingActionType] = mapped_column(
        str_enum(PendingActionType, name="pending_action_type")
    )
    # The action exactly as the human will be shown it, and exactly as it will
    # be executed: recipients already resolved server-side to real addresses,
    # nothing left to interpret. Seen, hashed and executed are the same object.
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    payload_hash: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[PendingActionStatus] = mapped_column(
        str_enum(PendingActionStatus, name="pending_action_status"),
        default=PendingActionStatus.PENDING,
    )
    initiated_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    decided_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    # Why the server refused to offer it, when it did. Shown to the user.
    refusal_reason: Mapped[str | None] = mapped_column(String(1000), default=None)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
