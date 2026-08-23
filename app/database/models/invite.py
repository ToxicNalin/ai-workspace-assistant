import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.constants import InviteStatus, WorkspaceRole
from app.database.base import Base
from app.database.mixins import UUIDPrimaryKey, WorkspaceScoped
from app.database.types import str_enum


class WorkspaceInvite(Base, UUIDPrimaryKey, WorkspaceScoped):
    __tablename__ = "workspace_invites"
    __table_args__ = (
        Index(
            "uq_workspace_invites_workspace_id_email_pending",
            "workspace_id",
            "email",
            unique=True,
            postgresql_where=text("status = 'pending'"),
        ),
    )

    email: Mapped[str] = mapped_column(String(320))
    # SHA-256 hex digest of the raw invite token — the raw token itself is
    # returned to the caller once, at creation, and never stored.
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    invited_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    role: Mapped[WorkspaceRole] = mapped_column(
        str_enum(WorkspaceRole, name="workspace_role"), default=WorkspaceRole.MEMBER
    )
    status: Mapped[InviteStatus] = mapped_column(
        str_enum(InviteStatus, name="invite_status"), default=InviteStatus.PENDING
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
