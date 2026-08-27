import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status

from app.auth.permissions import require_admin
from app.constants import WorkspaceRole
from app.dependencies import CurrentUser, DbSession, WorkspaceContext, get_workspace_context
from app.exceptions import Forbidden
from app.schemas.workspace import (
    InviteAccept,
    InviteCreate,
    InviteOut,
    MemberOut,
    MemberRoleUpdate,
    WorkspaceCreate,
    WorkspaceOut,
)
from app.services import invite_service, workspace_service
from app.services.email_service import get_email_provider

router = APIRouter(prefix="/workspaces", tags=["workspaces"])


@router.post("", response_model=WorkspaceOut, status_code=status.HTTP_201_CREATED)
async def create_workspace(body: WorkspaceCreate, db: DbSession, user: CurrentUser) -> WorkspaceOut:
    workspace = await workspace_service.create_workspace(db, owner=user, name=body.name)
    return WorkspaceOut.model_validate(workspace)


@router.get("", response_model=list[WorkspaceOut])
async def list_workspaces(db: DbSession, user: CurrentUser) -> list[WorkspaceOut]:
    workspaces = await workspace_service.list_workspaces_for_user(db, user)
    return [WorkspaceOut.model_validate(workspace) for workspace in workspaces]


@router.post("/join", response_model=MemberOut)
async def join_workspace(body: InviteAccept, db: DbSession, user: CurrentUser) -> MemberOut:
    membership = await invite_service.accept_invite(db, raw_token=body.token, user=user)
    return MemberOut.model_validate(membership)


@router.get("/{workspace_id}/members", response_model=list[MemberOut])
async def list_members(
    db: DbSession,
    context: Annotated[WorkspaceContext, Depends(get_workspace_context)],
) -> list[MemberOut]:
    members = await workspace_service.list_members(db, context.workspace_id)
    return [MemberOut.model_validate(member) for member in members]


@router.patch("/{workspace_id}/members/{user_id}", response_model=MemberOut)
async def update_member_role(
    user_id: uuid.UUID,
    body: MemberRoleUpdate,
    db: DbSession,
    context: Annotated[WorkspaceContext, Depends(require_admin)],
) -> MemberOut:
    membership = await workspace_service.change_role(db, context.workspace_id, user_id, body.role)
    return MemberOut.model_validate(membership)


@router.delete("/{workspace_id}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_member(
    user_id: uuid.UUID,
    db: DbSession,
    context: Annotated[WorkspaceContext, Depends(get_workspace_context)],
) -> None:
    is_self = user_id == context.user.id
    if not is_self and context.role != WorkspaceRole.ADMIN:
        raise Forbidden("Only an admin can remove another member")

    await workspace_service.remove_member(db, context.workspace_id, user_id)


@router.post(
    "/{workspace_id}/invite", response_model=InviteOut, status_code=status.HTTP_201_CREATED
)
async def create_invite(
    body: InviteCreate,
    db: DbSession,
    context: Annotated[WorkspaceContext, Depends(require_admin)],
) -> InviteOut:
    invite, raw_token, email_sent = await invite_service.create_invite(
        db,
        mailer=get_email_provider(),
        workspace_id=context.workspace_id,
        invited_by=context.user,
        email=body.email,
        role=body.role,
    )
    out = InviteOut.model_validate(invite)
    out.token = raw_token
    out.email_sent = email_sent
    return out
