import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.constants import PendingActionOrigin, PendingActionStatus, PendingActionType
from app.schemas.common import ORMModel


class PendingActionOut(ORMModel):
    id: uuid.UUID
    # NULL for an action proposed directly through /email/send: there is no
    # conversation behind it, and no paused agent run waiting on the decision.
    thread_id: uuid.UUID | None
    origin: PendingActionOrigin
    type: PendingActionType
    # The action exactly as it will be executed, recipients already resolved to
    # real members of this workspace.
    payload: dict[str, Any]
    # Echo this back with the decision. The server re-hashes what it holds and
    # refuses if the two disagree, which is what stops an action being altered
    # between being shown and being approved (SPEC-v2 D20).
    payload_hash: str
    status: PendingActionStatus
    initiated_by: uuid.UUID
    decided_by: uuid.UUID | None
    decided_at: datetime | None
    refusal_reason: str | None
    created_at: datetime


class ApprovalDecision(BaseModel):
    # Three, not four. The middleware also understands "respond", but that is
    # the server's own channel for handing back the result of an action it has
    # already carried out (see app/ai/agent/graph.py) -- never something a
    # client may ask for.
    decision: Literal["approve", "edit", "reject"]
    # Required on every decision, including a rejection: it proves the reviewer
    # was looking at the action the server actually holds.
    payload_hash: str = Field(min_length=64, max_length=64)
    # Only for an edit. It may change what is said; it may not change who the
    # action is addressed to.
    edited_payload: dict[str, Any] | None = None


class AgentRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    thread_id: uuid.UUID | None = None


class AgentTurnOut(BaseModel):
    thread_id: uuid.UUID
    reply: str
    # Waiting on a human before anything happens.
    pending_actions: list[PendingActionOut] = []
    # Never offered for approval: the model named someone who is not a member
    # of this workspace, so the server declined to propose it at all.
    refused_actions: list[PendingActionOut] = []


class AuditEntryOut(ORMModel):
    id: uuid.UUID
    user_id: uuid.UUID | None
    action: str
    details: dict[str, Any]
    created_at: datetime
