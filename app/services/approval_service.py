"""Deciding a pending action. This is where SPEC-v2 D20 is enforced.

The rule: before anything executes, re-hash the stored payload and compare it
to the hash the human was actually shown. If they differ, the payload changed
between being displayed and being approved, and the approval refers to
something the approver never saw. Refuse it.

Without that check the gate is theatre -- a dialogue saying "send this email to
Alice" followed by a server that sends whatever the row happens to contain by
the time the click lands.
"""

import logging
import uuid
from collections.abc import Sequence
from typing import Any

from langchain_core.language_models import BaseChatModel
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.types import Command
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.agent.graph import build_agent
from app.ai.tools.resolve import UnresolvableRecipient
from app.constants import AuditAction, ChatRole, PendingActionStatus, PendingActionType
from app.database.models.chat import ChatMessage
from app.database.models.pending_action import PendingAction
from app.exceptions import Conflict, NotFound
from app.services import audit_service
from app.services.payload import hash_payload

logger = logging.getLogger(__name__)


class PayloadMismatch(Conflict):
    detail = (
        "This action has changed since it was shown to you. Review it again before "
        "approving."
    )


async def list_pending(
    db: AsyncSession, workspace_id: uuid.UUID, *, include_decided: bool = False
) -> Sequence[PendingAction]:
    statement = select(PendingAction).where(PendingAction.workspace_id == workspace_id)
    if not include_decided:
        statement = statement.where(PendingAction.status == PendingActionStatus.PENDING)

    result = await db.scalars(statement.order_by(PendingAction.created_at.desc()))
    return result.all()


async def get_action(
    db: AsyncSession, workspace_id: uuid.UUID, action_id: uuid.UUID
) -> PendingAction:
    action = await db.scalar(
        select(PendingAction).where(
            PendingAction.id == action_id, PendingAction.workspace_id == workspace_id
        )
    )
    if action is None:
        raise NotFound
    return action


def _tool_args(payload: dict[str, Any], action_type: PendingActionType) -> dict[str, Any]:
    """Turn the approved payload back into arguments for the tool.

    Addresses, not names. By this point the server has already decided who
    these people are; the model's original strings are not consulted again.
    """
    if action_type is PendingActionType.SEND_EMAIL:
        return {
            "recipients": [person["email"] for person in payload.get("recipients", [])],
            "subject": payload.get("subject", ""),
            "body": payload.get("body", ""),
        }

    if action_type is PendingActionType.CREATE_EVENT:
        return {
            "title": payload.get("title", ""),
            "start_time": payload.get("start_time", ""),
            "end_time": payload.get("end_time", ""),
            "guests": [person["email"] for person in payload.get("guests", [])],
        }

    return {
        "tasks": [
            {
                "title": task.get("title", ""),
                "description": task.get("description", ""),
                "assignee": (task.get("assignee") or {}).get("email"),
            }
            for task in payload.get("tasks", [])
        ]
    }


async def decide(
    db: AsyncSession,
    *,
    model: BaseChatModel,
    checkpointer: BaseCheckpointSaver,  # type: ignore[type-arg]
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    action_id: uuid.UUID,
    decision: str,
    payload_hash: str,
    edited_payload: dict[str, Any] | None = None,
) -> PendingAction:
    action = await get_action(db, workspace_id, action_id)

    # Already decided. This is what makes approving twice execute once, even if
    # two clicks race: the second finds a row that is no longer pending.
    if action.status is not PendingActionStatus.PENDING:
        raise Conflict(f"This action has already been {action.status.value}.")

    # SPEC-v2 D20. The hash is recomputed from what is stored right now, not
    # read back from the column -- a stored hash would agree with a tampered
    # payload, since whoever changed one could change the other.
    current_hash = hash_payload(action.payload)
    if current_hash != payload_hash:
        audit_service.record(
            db,
            workspace_id=workspace_id,
            action=AuditAction.APPROVAL_HASH_MISMATCH,
            user_id=user_id,
            details={
                "action_id": str(action.id),
                "shown_to_user": payload_hash,
                "current": current_hash,
            },
        )
        await db.commit()
        logger.warning(
            "approval refused: payload changed since it was shown",
            extra={"action_id": str(action.id), "workspace_id": str(workspace_id)},
        )
        raise PayloadMismatch

    if decision == "reject":
        action.status = PendingActionStatus.REJECTED
        resume: dict[str, Any] = {"type": "reject", "message": "Rejected by a reviewer."}
        audit_action = AuditAction.ACTION_REJECTED
    else:
        payload = action.payload
        if decision == "edit":
            if edited_payload is None:
                raise Conflict("An edit decision must carry the edited payload.")
            if action.type is PendingActionType.CREATE_TASKS:
                # Matches the interrupt policy in app/ai/agent/graph.py, which
                # does not offer edit for this tool.
                raise Conflict("This kind of action cannot be edited, only approved or rejected.")
            payload = _validate_edit(action, edited_payload)
            action.payload = payload
            action.payload_hash = hash_payload(payload)

        action.status = PendingActionStatus.APPROVED
        # Resumed as an edit rather than a plain approve, even for a plain
        # approval. The tool then runs on the server's resolved payload -- the
        # exact object the human saw and the hash covers -- instead of on the
        # arguments the model originally produced.
        resume = {
            "type": "edit",
            "edited_action": {
                "name": action.type.value,
                "args": _tool_args(payload, action.type),
            },
        }
        audit_action = AuditAction.ACTION_APPROVED

    action.decided_by = user_id
    action.decided_at = _now()
    audit_service.record(
        db,
        workspace_id=workspace_id,
        action=audit_action,
        user_id=user_id,
        details={"action_id": str(action.id), "payload_hash": action.payload_hash},
    )
    await db.commit()

    agent = build_agent(db, workspace_id, model=model, checkpointer=checkpointer)
    config: Any = {"configurable": {"thread_id": str(action.thread_id)}}
    result = await agent.ainvoke(Command(resume={"decisions": [resume]}), config=config)

    if decision != "reject":
        action.status = PendingActionStatus.EXECUTED
        audit_service.record(
            db,
            workspace_id=workspace_id,
            action=AuditAction.ACTION_EXECUTED,
            user_id=user_id,
            details={"action_id": str(action.id), "type": action.type.value},
        )

    reply = _reply_text(result)
    if reply:
        db.add(
            ChatMessage(
                workspace_id=workspace_id,
                thread_id=action.thread_id,
                user_id=None,
                role=ChatRole.ASSISTANT,
                content=reply,
            )
        )

    await db.commit()
    await db.refresh(action)
    return action


def _validate_edit(action: PendingAction, edited: dict[str, Any]) -> dict[str, Any]:
    """A reviewer may change what is said. They may not change who it goes to.

    Editing is there so someone can fix a subject line or reword a message
    before it goes out. Letting it also rewrite the recipient list would hand
    back the exact capability D21 removed -- and it would arrive with a valid
    approval attached.
    """
    original = action.payload
    people_field = "recipients" if action.type is PendingActionType.SEND_EMAIL else "guests"

    original_people = [person["email"] for person in original.get(people_field, [])]
    edited_people = [
        person["email"] for person in edited.get(people_field, []) if isinstance(person, dict)
    ]

    if sorted(original_people) != sorted(edited_people):
        raise UnresolvableRecipientEdit

    merged = dict(edited)
    # Keep the server's own resolution, not whatever the client echoed back.
    merged[people_field] = original.get(people_field, [])
    merged["type"] = original.get("type")
    return merged


class UnresolvableRecipientEdit(Conflict):
    detail = (
        "An edit cannot change who an action is addressed to. Reject this action and "
        "ask again if the recipients are wrong."
    )


def _now() -> Any:
    from datetime import UTC, datetime

    return datetime.now(UTC)


def _reply_text(result: dict[str, Any]) -> str:
    for message in reversed(result.get("messages") or []):
        if getattr(message, "type", None) == "ai" and message.content:
            return str(message.content)
    return ""


__all__ = [
    "PayloadMismatch",
    "UnresolvableRecipient",
    "UnresolvableRecipientEdit",
    "decide",
    "get_action",
    "list_pending",
]
