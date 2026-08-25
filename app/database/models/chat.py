import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.constants import ChatRole
from app.database.base import Base
from app.database.mixins import UUIDPrimaryKey, WorkspaceScoped
from app.database.types import str_enum


class ChatThread(Base, UUIDPrimaryKey, WorkspaceScoped):
    __tablename__ = "chat_threads"

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    # Derived from the opening question so the sidebar is usable without the
    # user having to name anything.
    title: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ChatMessage(Base, UUIDPrimaryKey, WorkspaceScoped):
    """One turn in a thread.

    SPEC-v2 replaced v1's `is_bot` boolean with a role enum: a tool message is
    neither a user's nor the assistant's, and a boolean cannot say so.
    """

    __tablename__ = "chat_messages"
    __table_args__ = (
        Index("ix_chat_messages_thread_id_created_at", "thread_id", "created_at"),
    )

    thread_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("chat_threads.id", ondelete="CASCADE")
    )
    # NULL for assistant and tool messages -- nobody sent them.
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    role: Mapped[ChatRole] = mapped_column(str_enum(ChatRole, name="chat_role"))
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
