import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.constants import UsageKind
from app.database.base import Base
from app.database.mixins import UUIDPrimaryKey, WorkspaceScoped
from app.database.types import str_enum


class UsageEvent(Base, UUIDPrimaryKey, WorkspaceScoped):
    """One model call's token spend (SPEC-v2 D8).

    Written in the same transaction as whatever it paid for, so a recorded
    answer and the tokens it cost cannot disagree -- the same reasoning as the
    audit log next door. Append-only for the same reason.

    The index is on (workspace_id, created_at) because both readers ask a
    range question: the budget check wants the last 24 hours for one
    workspace, and /admin/usage wants the last week for one workspace.
    """

    __tablename__ = "usage_events"
    __table_args__ = (
        Index("ix_usage_events_workspace_id_created_at", "workspace_id", "created_at"),
    )

    # Nullable: ingestion embeds a document long after the upload request has
    # returned, and attributing that spend to whoever happened to be holding
    # the connection would be a fiction.
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    kind: Mapped[UsageKind] = mapped_column(str_enum(UsageKind, name="usage_kind"))
    # The model that was actually billed. Recorded per row rather than read
    # from settings at report time, because settings change and a historical
    # row that silently re-attributes itself to the current model is worse
    # than no attribution at all.
    model: Mapped[str] = mapped_column(String(100), default="")
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    # Whether the counts above came from the provider or from a
    # characters-per-token estimate. Embeddings are always estimated -- the
    # API returns vectors, not usage -- and a streamed completion is whenever
    # the provider declined to report. Recorded rather than discarded so
    # /admin/usage can say how much of its own total is a guess, instead of
    # presenting four significant figures of arithmetic done on a heuristic.
    estimated: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
