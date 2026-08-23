import uuid
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.auth.jwt import decode_access_token
from app.constants import WorkspaceRole
from app.database.models.membership import WorkspaceMember
from app.database.models.user import User
from app.database.session import get_db
from app.exceptions import NotFound, Unauthorized

DbSession = Annotated[AsyncSession, Depends(get_db)]


def _extract_bearer_token(authorization: str | None) -> str:
    if authorization is None:
        raise Unauthorized

    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise Unauthorized

    return token


async def get_current_user(
    db: DbSession,
    authorization: Annotated[str | None, Header()] = None,
) -> User:
    token = _extract_bearer_token(authorization)
    payload = decode_access_token(token)

    try:
        user_id = uuid.UUID(payload["sub"])
    except (KeyError, ValueError) as exc:
        raise Unauthorized from exc

    user = await db.get(User, user_id)
    if user is None or not user.is_active:
        raise Unauthorized

    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


@dataclass
class WorkspaceContext:
    workspace_id: uuid.UUID
    user: User
    role: WorkspaceRole


async def get_workspace_context(
    workspace_id: uuid.UUID,
    db: DbSession,
    user: CurrentUser,
) -> WorkspaceContext:
    """Every tenant-scoped route depends on this. A resource that belongs to a
    workspace the caller isn't a member of must be indistinguishable from a
    resource that doesn't exist — so this always raises NotFound, never
    Forbidden, for anything outside the caller's membership.
    """
    membership = await db.scalar(
        select(WorkspaceMember)
        .where(
            WorkspaceMember.workspace_id == workspace_id,
            WorkspaceMember.user_id == user.id,
        )
        .options(selectinload(WorkspaceMember.user))
    )
    if membership is None:
        raise NotFound

    return WorkspaceContext(workspace_id=workspace_id, user=user, role=membership.role)
