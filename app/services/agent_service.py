"""Running the agent, and turning what it proposes into pending actions.

The important work here is not invoking the graph -- it is what happens between
the model deciding to do something and a human being shown it. Every person the
model named is resolved against `workspace_members` first (SPEC-v2 D21), and an
action naming anyone who is not a member never becomes a pending action at all.
It is refused, recorded, and the agent is told so.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from langchain_core.language_models import BaseChatModel
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.types import Command
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.agent.graph import build_agent
from app.ai.agent.state import ProposedAction, parse_interrupts, reply_text
from app.ai.tools.base import ActionRefused, InvalidActionArguments
from app.ai.tools.resolve import ResolvedMember, resolve_members
from app.ai.upstream import THE_AGENT, provider_errors
from app.constants import (
    MAX_TASKS_PER_ACTION,
    AuditAction,
    ChatRole,
    PendingActionStatus,
    PendingActionType,
    UsageKind,
)
from app.database.models.chat import ChatMessage, ChatThread
from app.database.models.pending_action import PendingAction
from app.exceptions import NotFound
from app.services import audit_service, usage_service
from app.services.chat_service import _derive_title
from app.services.payload import hash_payload

logger = logging.getLogger(__name__)


@dataclass
class AgentTurn:
    """What one agent turn produced: a reply, some proposals, some refusals."""

    thread: ChatThread
    reply: str
    pending: list[PendingAction]
    refused: list[PendingAction]


def _parse_moment(value: Any, field: str) -> datetime:
    """Reject an unparseable timestamp at proposal time, not at execution time.

    Discovering it after approval would mean a human had already authorised
    something that was never going to work, and the refusal would arrive as a
    failure rather than as the agent being told to try again.
    """
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise InvalidActionArguments(
            field, f"'{value}' is not a valid ISO 8601 {field}."
        ) from exc


def _members_payload(members: list[ResolvedMember]) -> list[dict[str, str]]:
    return [
        {"user_id": str(member.user_id), "name": member.name, "email": member.email}
        for member in members
    ]


async def _resolve_action(
    db: AsyncSession, *, workspace_id: uuid.UUID, action: ProposedAction
) -> dict[str, Any]:
    """Turn a proposed tool call into the exact payload a human will approve.

    Everything the model said about *who* is replaced by what the server found
    in this workspace's membership. Raises UnresolvableRecipient if any named
    person is not a member, which aborts the action entirely.
    """
    args = action.args

    if action.action_type is PendingActionType.SEND_EMAIL:
        recipients = await resolve_members(
            db, workspace_id=workspace_id, references=list(args.get("recipients") or [])
        )
        return {
            "type": PendingActionType.SEND_EMAIL.value,
            "recipients": _members_payload(recipients),
            "subject": str(args.get("subject") or ""),
            "body": str(args.get("body") or ""),
        }

    if action.action_type is PendingActionType.CREATE_EVENT:
        guests = await resolve_members(
            db, workspace_id=workspace_id, references=list(args.get("guests") or [])
        )
        start_time = _parse_moment(args.get("start_time"), "start time")
        end_time = _parse_moment(args.get("end_time"), "end time")
        if end_time <= start_time:
            raise InvalidActionArguments(
                "end time", "An event must end after it starts."
            )
        return {
            "type": PendingActionType.CREATE_EVENT.value,
            "title": str(args.get("title") or ""),
            # Stored normalised rather than as the model wrote them. What the
            # human is shown, what the hash covers and what is eventually
            # written to the calendar are then one string, not three
            # renderings of the same instant.
            "start_time": start_time.isoformat(),
            "end_time": end_time.isoformat(),
            "guests": _members_payload(guests),
        }

    raw_tasks = list(args.get("tasks") or [])
    if not raw_tasks:
        raise InvalidActionArguments("tasks", "There are no tasks to create.")
    if len(raw_tasks) > MAX_TASKS_PER_ACTION:
        raise InvalidActionArguments(
            "tasks",
            f"No more than {MAX_TASKS_PER_ACTION} tasks can be created in one action.",
        )

    tasks: list[dict[str, Any]] = []
    for raw in raw_tasks:
        assignee = raw.get("assignee")
        resolved = (
            await resolve_members(db, workspace_id=workspace_id, references=[assignee])
            if assignee
            else []
        )
        tasks.append(
            {
                "title": str(raw.get("title") or ""),
                "description": str(raw.get("description") or ""),
                "assignee": _members_payload(resolved)[0] if resolved else None,
            }
        )

    return {"type": PendingActionType.CREATE_TASKS.value, "tasks": tasks}


async def run_agent(
    db: AsyncSession,
    *,
    model: BaseChatModel,
    checkpointer: BaseCheckpointSaver,  # type: ignore[type-arg]
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    message: str,
    thread_id: uuid.UUID | None = None,
) -> AgentTurn:
    # Before the thread is opened, before the user's message is written, and
    # well before the graph runs. An agent turn is the most expensive thing
    # this application does -- it can fan out into a tool call and several
    # completions -- so it is the one that most needs stopping at the door
    # rather than halfway through (SPEC-v2 D22).
    await usage_service.enforce_budget(db, workspace_id)

    if thread_id is None:
        thread = ChatThread(
            workspace_id=workspace_id, user_id=user_id, title=_derive_title(message)
        )
        db.add(thread)
        await db.flush()
    else:
        existing = await db.scalar(
            select(ChatThread).where(
                ChatThread.id == thread_id, ChatThread.workspace_id == workspace_id
            )
        )
        if existing is None:
            raise NotFound
        thread = existing

    db.add(
        ChatMessage(
            workspace_id=workspace_id,
            thread_id=thread.id,
            user_id=user_id,
            role=ChatRole.USER,
            content=message,
        )
    )
    await db.flush()

    agent = build_agent(db, workspace_id, model=model, checkpointer=checkpointer)
    # The graph's thread_id is the chat thread's, so a paused approval is
    # attached to the conversation it came from and survives a restart.
    config: Any = {"configurable": {"thread_id": str(thread.id)}}
    # An agent turn is several provider calls behind one await, and on the free
    # tier the likeliest way it fails is the quota rather than a bug here
    # (SPEC-v2 §7).
    with provider_errors(THE_AGENT):
        result = await agent.ainvoke({"messages": [("user", message)]}, config=config)

    # Summed across every message the run produced, not read off the last one:
    # a turn that searched the corpus and then drafted an email was billed
    # twice, and charging it once would make the budget an under-count of
    # exactly the runs that cost the most.
    tokens_in, tokens_out = usage_service.tokens_from_messages(result.get("messages", []))
    usage_service.record(
        db,
        workspace_id=workspace_id,
        user_id=user_id,
        kind=UsageKind.AGENT,
        model=getattr(model, "model_name", "") or model.__class__.__name__,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
    )

    pending: list[PendingAction] = []
    refused: list[PendingAction] = []
    decisions: list[dict[str, Any]] = []

    for action in parse_interrupts(result):
        try:
            payload = await _resolve_action(db, workspace_id=workspace_id, action=action)
        except ActionRefused as exc:
            # For a named non-member this is the channel D21 exists to close.
            # For unparseable arguments it is simply the server declining to
            # show a human something it already knows is broken. Either way
            # the attempt is recorded, the agent is told why, and nothing is
            # ever offered for approval.
            record = PendingAction(
                workspace_id=workspace_id,
                thread_id=thread.id,
                type=action.action_type,
                payload={"type": action.tool_name, "rejected_arguments": action.args},
                payload_hash="",
                status=PendingActionStatus.REFUSED,
                initiated_by=user_id,
                refusal_reason=str(exc),
            )
            db.add(record)
            refused.append(record)
            audit_service.record(
                db,
                workspace_id=workspace_id,
                action=AuditAction.ACTION_REFUSED,
                user_id=user_id,
                details={
                    "tool": action.tool_name,
                    "reference": exc.reference,
                    "reason": str(exc),
                },
            )
            decisions.append({"type": "reject", "message": str(exc)})
            logger.warning(
                "refused an action naming a non-member",
                extra={"workspace_id": str(workspace_id), "tool": action.tool_name},
            )
            continue

        record = PendingAction(
            workspace_id=workspace_id,
            thread_id=thread.id,
            type=action.action_type,
            payload=payload,
            payload_hash=hash_payload(payload),
            status=PendingActionStatus.PENDING,
            initiated_by=user_id,
        )
        db.add(record)
        pending.append(record)
        audit_service.record(
            db,
            workspace_id=workspace_id,
            action=AuditAction.ACTION_PROPOSED,
            user_id=user_id,
            details={"tool": action.tool_name, "payload_hash": record.payload_hash},
        )
        # No decision is appended: the graph stays paused on this one until a
        # human decides. Refusals above resolve themselves immediately.
        decisions.append({})

    reply = reply_text(result)

    if refused and not pending:
        # Every proposal was refused, so nothing is waiting on a human and the
        # graph can be told now rather than being left paused for ever.
        resume = [
            decision or {"type": "reject", "message": "Refused."} for decision in decisions
        ]
        with provider_errors(THE_AGENT):
            await agent.ainvoke(Command(resume={"decisions": resume}), config=config)
        reply = reply or "That action was refused: it named someone outside this workspace."

    if reply:
        db.add(
            ChatMessage(
                workspace_id=workspace_id,
                thread_id=thread.id,
                user_id=None,
                role=ChatRole.ASSISTANT,
                content=reply,
            )
        )

    await db.commit()
    await db.refresh(thread)
    for record in [*pending, *refused]:
        await db.refresh(record)

    return AgentTurn(thread=thread, reply=reply, pending=pending, refused=refused)
