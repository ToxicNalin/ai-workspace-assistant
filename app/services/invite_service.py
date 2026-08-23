import secrets
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants import InviteStatus, WorkspaceRole
from app.database.models.invite import WorkspaceInvite
from app.database.models.membership import WorkspaceMember
from app.database.models.user import User
from app.exceptions import NotFound
from app.utils.crypto import hash_token

INVITE_EXPIRY = timedelta(days=7)


async def create_invite(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    invited_by: User,
    email: str,
    role: WorkspaceRole,
) -> tuple[WorkspaceInvite, str]:
    email = email.strip().lower()
    raw_token = secrets.token_urlsafe(32)

    invite = WorkspaceInvite(
        workspace_id=workspace_id,
        email=email,
        token_hash=hash_token(raw_token),
        invited_by=invited_by.id,
        role=role,
        status=InviteStatus.PENDING,
        expires_at=datetime.now(UTC) + INVITE_EXPIRY,
    )
    db.add(invite)
    await db.commit()
    await db.refresh(invite)
    return invite, raw_token


async def accept_invite(db: AsyncSession, *, raw_token: str, user: User) -> WorkspaceMember:
    invite = await db.scalar(
        select(WorkspaceInvite).where(WorkspaceInvite.token_hash == hash_token(raw_token))
    )

    # Every rejection here is NotFound, not a more specific error -- a
    # revoked/expired/wrong-email token must not reveal which case it is.
    if invite is None or invite.status != InviteStatus.PENDING:
        raise NotFound

    if invite.expires_at < datetime.now(UTC):
        invite.status = InviteStatus.EXPIRED
        await db.commit()
        raise NotFound

    if invite.email != user.email:
        raise NotFound

    existing = await db.scalar(
        select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == invite.workspace_id,
            WorkspaceMember.user_id == user.id,
        )
    )
    if existing is not None:
        invite.status = InviteStatus.ACCEPTED
        await db.commit()
        await db.refresh(existing)
        # user.user is lazy="raise" (no implicit lazy loads under asyncio) --
        # we already hold the loaded User in hand, so assign it directly
        # instead of a needless extra query.
        existing.user = user
        return existing

    membership = WorkspaceMember(
        workspace_id=invite.workspace_id, user_id=user.id, role=invite.role
    )
    db.add(membership)
    invite.status = InviteStatus.ACCEPTED
    await db.commit()
    await db.refresh(membership)
    membership.user = user
    return membership


async def revoke_invite(db: AsyncSession, *, workspace_id: uuid.UUID, invite_id: uuid.UUID) -> None:
    invite = await db.scalar(
        select(WorkspaceInvite).where(
            WorkspaceInvite.id == invite_id, WorkspaceInvite.workspace_id == workspace_id
        )
    )
    if invite is None:
        raise NotFound

    invite.status = InviteStatus.REVOKED
    await db.commit()
