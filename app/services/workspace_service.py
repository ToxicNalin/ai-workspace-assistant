import uuid
from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.constants import WorkspaceRole
from app.database.models.membership import WorkspaceMember
from app.database.models.user import User
from app.database.models.workspace import Workspace
from app.exceptions import Conflict, NotFound


async def create_workspace(db: AsyncSession, *, owner: User, name: str) -> Workspace:
    workspace = Workspace(name=name, owner_id=owner.id)
    db.add(workspace)
    await db.flush()

    db.add(WorkspaceMember(workspace_id=workspace.id, user_id=owner.id, role=WorkspaceRole.ADMIN))
    await db.commit()
    await db.refresh(workspace)
    return workspace


async def list_workspaces_for_user(db: AsyncSession, user: User) -> Sequence[Workspace]:
    result = await db.scalars(
        select(Workspace)
        .join(WorkspaceMember, WorkspaceMember.workspace_id == Workspace.id)
        .where(WorkspaceMember.user_id == user.id)
        .order_by(Workspace.created_at)
    )
    return result.all()


async def list_members(db: AsyncSession, workspace_id: uuid.UUID) -> Sequence[WorkspaceMember]:
    result = await db.scalars(
        select(WorkspaceMember)
        .where(WorkspaceMember.workspace_id == workspace_id)
        .options(selectinload(WorkspaceMember.user))
        .order_by(WorkspaceMember.joined_at)
    )
    return result.all()


async def _get_membership(
    db: AsyncSession, workspace_id: uuid.UUID, user_id: uuid.UUID
) -> WorkspaceMember:
    membership = await db.scalar(
        select(WorkspaceMember)
        .where(
            WorkspaceMember.workspace_id == workspace_id,
            WorkspaceMember.user_id == user_id,
        )
        .options(selectinload(WorkspaceMember.user))
    )
    if membership is None:
        raise NotFound
    return membership


async def _count_admins(db: AsyncSession, workspace_id: uuid.UUID) -> int:
    count = await db.scalar(
        select(func.count())
        .select_from(WorkspaceMember)
        .where(
            WorkspaceMember.workspace_id == workspace_id,
            WorkspaceMember.role == WorkspaceRole.ADMIN,
        )
    )
    return count or 0


async def change_role(
    db: AsyncSession, workspace_id: uuid.UUID, target_user_id: uuid.UUID, new_role: WorkspaceRole
) -> WorkspaceMember:
    membership = await _get_membership(db, workspace_id, target_user_id)

    if (
        membership.role == WorkspaceRole.ADMIN
        and new_role != WorkspaceRole.ADMIN
        and await _count_admins(db, workspace_id) <= 1
    ):
        raise Conflict("A workspace must keep at least one admin")

    membership.role = new_role
    await db.commit()
    await db.refresh(membership)
    return membership


async def remove_member(
    db: AsyncSession, workspace_id: uuid.UUID, target_user_id: uuid.UUID
) -> None:
    membership = await _get_membership(db, workspace_id, target_user_id)

    if membership.role == WorkspaceRole.ADMIN and await _count_admins(db, workspace_id) <= 1:
        raise Conflict("A workspace must keep at least one admin")

    await db.delete(membership)
    await db.commit()
