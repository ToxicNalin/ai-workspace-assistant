import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.constants import WorkspaceRole
from app.database.base import Base
from app.database.mixins import UUIDPrimaryKey, WorkspaceScoped
from app.database.models.user import User
from app.database.types import str_enum


class WorkspaceMember(Base, UUIDPrimaryKey, WorkspaceScoped):
    __tablename__ = "workspace_members"
    __table_args__ = (UniqueConstraint("workspace_id", "user_id"),)

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    role: Mapped[WorkspaceRole] = mapped_column(
        str_enum(WorkspaceRole, name="workspace_role"), default=WorkspaceRole.MEMBER
    )
    joined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    user: Mapped[User] = relationship(lazy="raise")
