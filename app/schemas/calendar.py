import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.common import ORMModel


class CalendarGuestOut(BaseModel):
    user_id: uuid.UUID
    name: str
    email: str


class CalendarEventOut(ORMModel):
    id: uuid.UUID
    title: str
    description: str
    start_time: datetime
    end_time: datetime
    created_by: uuid.UUID | None
    # The RFC 5545 UID. Stable for the life of the event, which is how a
    # calendar client recognises a later invitation as an update rather than a
    # second event in everybody's diary.
    ics_uid: str
    # NULL on the `.ics` path this project ships. The Google Calendar path is
    # a documented non-goal (SPEC-v2 D17) but the column is here so it needs no
    # migration if that ever changes.
    external_event_id: str | None
    # A snapshot of who was invited, resolved from workspace_members when the
    # invitation went out -- not today's membership.
    guests: list[CalendarGuestOut]
    created_at: datetime


class CalendarEventCreate(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    description: str = Field(default="", max_length=10_000)
    start_time: datetime
    end_time: datetime
    # Member references, resolved server-side. Same rule as email recipients:
    # the caller names people, the server decides what their addresses are.
    guests: list[str] = Field(default_factory=list, max_length=50)
