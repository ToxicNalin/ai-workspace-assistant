import uuid
from datetime import datetime

from pydantic import BaseModel

from app.constants import WorkspaceRole


class UsageOut(BaseModel):
    """What this workspace has spent, and what it has left.

    `daily_budget` and `tokens_remaining_today` are on the same object as the
    historical totals on purpose: the question an admin actually has is "are we
    about to be cut off", and answering it from two endpoints would mean the
    UI doing arithmetic across a race.
    """

    days: int
    calls: int
    tokens_in: int
    tokens_out: int
    # How many of those tokens are a characters-per-token estimate rather
    # than a figure the provider reported. Embeddings never report one, so
    # this is normally non-zero and saying so is more honest than a total
    # that implies a precision it does not have.
    tokens_estimated: int
    daily_budget: int
    tokens_used_today: int
    tokens_remaining_today: int
    by_kind: dict[str, int]
    by_day: dict[str, int]


class MemberUsageOut(BaseModel):
    user_id: uuid.UUID
    name: str
    email: str
    role: WorkspaceRole
    joined_at: datetime
    calls: int
    tokens_in: int
    tokens_out: int
