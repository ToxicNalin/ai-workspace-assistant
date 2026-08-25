import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base
from app.database.mixins import UUIDPrimaryKey, WorkspaceScoped


class CalendarEvent(Base, UUIDPrimaryKey, WorkspaceScoped):
    """An event, and the invitation that was sent for it.

    `ics_uid` is the RFC 5545 UID. It is generated once and stored rather than
    derived, because a UID is how a calendar client recognises a later
    invitation as an update to this event rather than a second event. Deriving
    it from mutable fields would silently turn every edit into a duplicate in
    everyone's calendar.

    `external_event_id` is the Google Calendar id, and stays NULL on the `.ics`
    path this project actually ships (SPEC-v2 D17). The column exists so the
    optional Google path in app/services/calendar_service.py has somewhere to
    put its answer without a migration.

    `guests` is a denormalised snapshot -- the same reasoning as D5's
    `quoted_text` on citations. The guest list was resolved server-side from
    `workspace_members` at the moment the invitation went out, and it has to
    stay readable afterwards: the `.ics` downloaded next month must list the
    people who were actually invited, not who happens to be a member now.
    """

    __tablename__ = "calendar_events"
    __table_args__ = (
        Index("ix_calendar_events_workspace_id_start_time", "workspace_id", "start_time"),
    )

    title: Mapped[str] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(Text, default="", server_default="")
    start_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    ics_uid: Mapped[str] = mapped_column(String(255), unique=True)
    external_event_id: Mapped[str | None] = mapped_column(String(255), default=None)
    guests: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
