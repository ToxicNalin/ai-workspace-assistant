import uuid
from typing import Annotated

from fastapi import APIRouter, Depends

from app.ai.agent.checkpointer import get_checkpointer
from app.ai.provider import get_agent_model
from app.auth.permissions import require_admin, require_member
from app.dependencies import DbSession, WorkspaceContext, get_workspace_context
from app.schemas.approval import (
    AgentRequest,
    AgentTurnOut,
    ApprovalDecision,
    AuditEntryOut,
    PendingActionOut,
)
from app.services import agent_service, approval_service, audit_service
from app.services.email_service import get_email_provider

router = APIRouter(prefix="/workspaces/{workspace_id}", tags=["approvals"])


@router.post("/agent", response_model=AgentTurnOut)
async def run_agent(
    body: AgentRequest,
    db: DbSession,
    context: Annotated[WorkspaceContext, Depends(require_member)],
) -> AgentTurnOut:
    """Ask the agent to do something.

    Nothing side-effecting happens in this request. Anything the agent wants to
    do comes back as a pending action for a human to decide on.
    """
    turn = await agent_service.run_agent(
        db,
        model=get_agent_model(),
        checkpointer=await get_checkpointer(),
        workspace_id=context.workspace_id,
        user_id=context.user.id,
        message=body.message,
        thread_id=body.thread_id,
    )

    return AgentTurnOut(
        thread_id=turn.thread.id,
        reply=turn.reply,
        pending_actions=[PendingActionOut.model_validate(action) for action in turn.pending],
        refused_actions=[PendingActionOut.model_validate(action) for action in turn.refused],
    )


@router.get("/pending-actions", response_model=list[PendingActionOut])
async def list_pending_actions(
    db: DbSession,
    context: Annotated[WorkspaceContext, Depends(get_workspace_context)],
    include_decided: bool = False,
) -> list[PendingActionOut]:
    actions = await approval_service.list_pending(
        db, context.workspace_id, include_decided=include_decided
    )
    return [PendingActionOut.model_validate(action) for action in actions]


@router.post("/pending-actions/{action_id}/decide", response_model=PendingActionOut)
async def decide(
    action_id: uuid.UUID,
    body: ApprovalDecision,
    db: DbSession,
    # Admin only. Approving is the moment the agent is allowed to touch the
    # world outside this process, so it is not something an ordinary member or
    # a viewer can do on the workspace's behalf.
    context: Annotated[WorkspaceContext, Depends(require_admin)],
) -> PendingActionOut:
    action = await approval_service.decide(
        db,
        model=get_agent_model(),
        checkpointer=await get_checkpointer(),
        mailer=get_email_provider(),
        workspace_id=context.workspace_id,
        user_id=context.user.id,
        action_id=action_id,
        decision=body.decision,
        payload_hash=body.payload_hash,
        edited_payload=body.edited_payload,
    )
    return PendingActionOut.model_validate(action)


@router.get("/audit-log", response_model=list[AuditEntryOut])
async def audit_log(
    db: DbSession,
    context: Annotated[WorkspaceContext, Depends(require_admin)],
) -> list[AuditEntryOut]:
    entries = await audit_service.list_entries(db, context.workspace_id)
    return [AuditEntryOut.model_validate(entry) for entry in entries]
