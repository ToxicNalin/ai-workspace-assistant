import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.constants import TaskStatus
from app.schemas.common import ORMModel


class TaskOut(ORMModel):
    id: uuid.UUID
    title: str
    description: str
    # A user id, not a membership id (SPEC-v2 D3) -- a membership row is
    # deleted and recreated when somebody leaves and rejoins.
    assigned_to: uuid.UUID | None
    # Provenance (D4): the agent turn this task came out of, if any.
    source_message_id: uuid.UUID | None
    status: TaskStatus
    due_date: datetime | None
    created_by: uuid.UUID | None
    created_at: datetime
    updated_at: datetime


class TaskCreate(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    description: str = Field(default="", max_length=10_000)
    assigned_to: uuid.UUID | None = None
    due_date: datetime | None = None
    status: TaskStatus = TaskStatus.TODO


class TaskUpdate(BaseModel):
    """Every field optional, and absence means something.

    The service applies this dumped with `exclude_unset`, so `assigned_to:
    null` unassigns the task while leaving the key out entirely keeps whoever
    is on it. A plain optional field cannot tell those two apart, and guessing
    would make it impossible to unassign anybody.
    """

    title: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=10_000)
    assigned_to: uuid.UUID | None = None
    due_date: datetime | None = None
    status: TaskStatus | None = None
