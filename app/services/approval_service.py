"""Deciding a pending action. This is where SPEC-v2 D20 is enforced.

The rule: before anything executes, re-hash the stored payload and compare it
to the hash the human was actually shown. If they differ, the payload changed
between being displayed and being approved, and the approval refers to
something the approver never saw. Refuse it.

Without that check the gate is theatre -- a dialogue saying "send this email to
Alice" followed by a server that sends whatever the row happens to contain by
the time the click lands.

Since Step 7 an approval also *does* something, and the order of operations
below is deliberate. The payload is validated, the decision is recorded and
committed, and only then is the side effect carried out -- from that same
stored payload, by app/services/action_executor.py, never by resuming the
graph and letting a tool body run. The graph is resumed afterwards, and told
what actually happened.
"""

import logging
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from langchain_core.language_models import BaseChatModel
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.types import Command
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.agent.graph import build_agent
from app.ai.agent.state import reply_text
from app.ai.tools.base import ActionRefused
from app.ai.tools.resolve import UnresolvableRecipient, resolve_members
from app.constants import (
    AuditAction,
    ChatRole,
    PendingActionOrigin,
    PendingActionStatus,
    PendingActionType,
)
from app.database.models.chat import ChatMessage
from app.database.models.pending_action import PendingAction
from app.database.models.user import User
from app.exceptions import AppError, Conflict, NotFound
from app.services import action_executor, audit_service
from app.services.email_service import EmailProvider
from app.services.payload import hash_payload

logger = logging.getLogger(__name__)


class PayloadMismatch(Conflict):
    detail = (
        "This action has changed since it was shown to you. Review it again before "
        "approving."
    )


class UnresolvableRecipientEdit(Conflict):
    detail = (
        "An edit cannot change who an action is addressed to. Reject this action and "
        "ask again if the recipients are wrong."
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


async def propose_email(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    recipients: Sequence[str],
    subject: str,
    body: str,
) -> PendingAction:
    """A person asking to send an email, through the same gate as the agent.

    A human composing their own message is not the threat SPEC-v2 D21 exists to
    stop, so this could reasonably have sent immediately. It does not, for two
    reasons. Recipients still go through the server-side resolver, so this
    endpoint cannot be used as a way to reach an address outside the workspace
    that the agent is forbidden from reaching. And an ordinary member can
    propose while only an admin can approve, which means every outbound message
    -- whoever composed it -- has two people behind it and one audit trail.

    Origin is recorded as `manual`: there is no paused graph run behind this
    one, and deciding it must not try to resume anything.
    """
    try:
        resolved = await resolve_members(
            db, workspace_id=workspace_id, references=recipients
        )
    except ActionRefused as exc:
        # A person naming a non-member is a mistake rather than an attack,
        # but the refusal is the same one the agent gets, and for the same
        # reason: this endpoint must not be a way to reach an address the
        # resolver would not hand out.
        raise NotFound(str(exc)) from exc

    payload: dict[str, Any] = {
        "type": PendingActionType.SEND_EMAIL.value,
        "recipients": [
            {"user_id": str(person.user_id), "name": person.name, "email": person.email}
            for person in resolved
        ],
        "subject": subject,
        "body": body,
    }

    action = PendingAction(
        workspace_id=workspace_id,
        thread_id=None,
        origin=PendingActionOrigin.MANUAL,
        type=PendingActionType.SEND_EMAIL,
        payload=payload,
        payload_hash=hash_payload(payload),
        status=PendingActionStatus.PENDING,
        initiated_by=user_id,
    )
    db.add(action)
    audit_service.record(
        db,
        workspace_id=workspace_id,
        action=AuditAction.ACTION_PROPOSED,
        user_id=user_id,
        details={"tool": PendingActionType.SEND_EMAIL.value, "origin": "manual"},
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


async def _resume_graph(
    db: AsyncSession,
    *,
    model: BaseChatModel,
    checkpointer: BaseCheckpointSaver,  # type: ignore[type-arg]
    workspace_id: uuid.UUID,
    action: PendingAction,
    resume: dict[str, Any],
) -> str:
    """Tell the paused run what the human decided, and what came of it.

    Only agent-proposed actions have a run to resume. A manual proposal has no
    checkpoint behind it, and invoking the graph on a thread that was never
    interrupted would run the model against an empty conversation.
    """
    if action.origin is not PendingActionOrigin.AGENT or action.thread_id is None:
        return ""

    agent = build_agent(db, workspace_id, model=model, checkpointer=checkpointer)
    config: Any = {"configurable": {"thread_id": str(action.thread_id)}}
    result = await agent.ainvoke(Command(resume={"decisions": [resume]}), config=config)
    return reply_text(result)


async def decide(
    db: AsyncSession,
    *,
    model: BaseChatModel,
    checkpointer: BaseCheckpointSaver,  # type: ignore[type-arg]
    mailer: EmailProvider,
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
        return await _reject(
            db,
            model=model,
            checkpointer=checkpointer,
            workspace_id=workspace_id,
            user_id=user_id,
            action=action,
        )

    if decision == "edit":
        if edited_payload is None:
            raise Conflict("An edit decision must carry the edited payload.")
        if action.type is PendingActionType.CREATE_TASKS:
            # Matches the interrupt policy in app/ai/agent/graph.py, which
            # does not offer edit for this tool.
            raise Conflict("This kind of action cannot be edited, only approved or rejected.")
        edited = _validate_edit(action, edited_payload)
        action.payload = edited
        action.payload_hash = hash_payload(edited)

    action.status = PendingActionStatus.APPROVED
    action.decided_by = user_id
    action.decided_at = datetime.now(UTC)
    audit_service.record(
        db,
        workspace_id=workspace_id,
        action=AuditAction.ACTION_APPROVED,
        user_id=user_id,
        details={"action_id": str(action.id), "payload_hash": action.payload_hash},
    )
    # Committed before anything leaves the process. If the send then fails, the
    # decision is still on record and the row is moved to `failed` below --
    # rather than a sent email sitting behind a transaction that rolled back.
    await db.commit()

    requester = await db.get(User, action.initiated_by)

    try:
        outcome = await action_executor.execute(
            db, mailer, workspace_id=workspace_id, action=action, requester=requester
        )
    except AppError as exc:
        # Every failure, not only an upstream one. An assignee who left the
        # workspace between the proposal and the click raises NotFound, and a
        # payload whose times no longer make sense raises Conflict -- both must
        # leave the row saying `failed` rather than `approved`, or the status
        # column starts meaning "somebody clicked approve" instead of "this
        # happened". The status code the caller sees is still the specific one.
        await _mark_failed(
            db,
            model=model,
            checkpointer=checkpointer,
            workspace_id=workspace_id,
            user_id=user_id,
            action_id=action.id,
            reason=str(exc.detail),
        )
        raise

    action.status = PendingActionStatus.EXECUTED
    audit_service.record(
        db,
        workspace_id=workspace_id,
        action=AuditAction.ACTION_EXECUTED,
        user_id=user_id,
        details={
            "action_id": str(action.id),
            "type": action.type.value,
            **outcome.details,
        },
    )
    await db.commit()

    # "respond" rather than "approve" or "edit": the tool has already been
    # carried out, so the graph is handed the real result instead of being
    # allowed to run the tool itself. See app/ai/tools/base.py.
    reply = await _resume_graph(
        db,
        model=model,
        checkpointer=checkpointer,
        workspace_id=workspace_id,
        action=action,
        resume={"type": "respond", "message": outcome.summary},
    )

    await _persist_reply(db, workspace_id=workspace_id, action=action, reply=reply)
    await db.commit()
    await db.refresh(action)
    return action


async def _reject(
    db: AsyncSession,
    *,
    model: BaseChatModel,
    checkpointer: BaseCheckpointSaver,  # type: ignore[type-arg]
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    action: PendingAction,
) -> PendingAction:
    action.status = PendingActionStatus.REJECTED
    action.decided_by = user_id
    action.decided_at = datetime.now(UTC)
    audit_service.record(
        db,
        workspace_id=workspace_id,
        action=AuditAction.ACTION_REJECTED,
        user_id=user_id,
        details={"action_id": str(action.id), "payload_hash": action.payload_hash},
    )
    await db.commit()

    reply = await _resume_graph(
        db,
        model=model,
        checkpointer=checkpointer,
        workspace_id=workspace_id,
        action=action,
        resume={"type": "reject", "message": "Rejected by a reviewer."},
    )

    await _persist_reply(db, workspace_id=workspace_id, action=action, reply=reply)
    await db.commit()
    await db.refresh(action)
    return action


async def _mark_failed(
    db: AsyncSession,
    *,
    model: BaseChatModel,
    checkpointer: BaseCheckpointSaver,  # type: ignore[type-arg]
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    action_id: uuid.UUID,
    reason: str,
) -> None:
    """Record that an approved action did not happen, and tell the agent so.

    The rollback first is the important line: the executor may have inserted an
    event or a batch of tasks before the provider refused the message, and half
    of a side effect is worse than none of it.
    """
    await db.rollback()

    action = await get_action(db, workspace_id, action_id)
    action.status = PendingActionStatus.FAILED
    action.refusal_reason = reason
    audit_service.record(
        db,
        workspace_id=workspace_id,
        action=AuditAction.ACTION_FAILED,
        user_id=user_id,
        details={"action_id": str(action.id), "type": action.type.value, "reason": reason},
    )
    await db.commit()

    try:
        reply = await _resume_graph(
            db,
            model=model,
            checkpointer=checkpointer,
            workspace_id=workspace_id,
            action=action,
            resume={
                "type": "reject",
                "message": f"This action was approved but could not be carried out: {reason}",
            },
        )
        await _persist_reply(db, workspace_id=workspace_id, action=action, reply=reply)
        await db.commit()
    except Exception:  # noqa: BLE001
        # The failure is already recorded and about to be raised to the caller.
        # Leaving the graph paused is recoverable; losing the record is not.
        logger.exception(
            "could not tell the agent its action failed",
            extra={"action_id": str(action_id)},
        )


async def _persist_reply(
    db: AsyncSession, *, workspace_id: uuid.UUID, action: PendingAction, reply: str
) -> None:
    if not reply or action.thread_id is None:
        return

    db.add(
        ChatMessage(
            workspace_id=workspace_id,
            thread_id=action.thread_id,
            user_id=None,
            role=ChatRole.ASSISTANT,
            content=reply,
        )
    )


__all__ = [
    "PayloadMismatch",
    "UnresolvableRecipient",
    "UnresolvableRecipientEdit",
    "decide",
    "get_action",
    "list_pending",
    "propose_email",
]
