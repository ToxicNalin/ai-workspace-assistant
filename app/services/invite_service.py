import logging
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import quote, urljoin

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.constants import InviteStatus, WorkspaceRole
from app.database.models.invite import WorkspaceInvite
from app.database.models.membership import WorkspaceMember
from app.database.models.user import User
from app.database.models.workspace import Workspace
from app.exceptions import AppError, NotFound
from app.services import email_service
from app.services.email_service import EmailProvider, OutboundEmail
from app.utils.crypto import hash_token

logger = logging.getLogger(__name__)

INVITE_EXPIRY = timedelta(days=7)


def invite_link(raw_token: str) -> str:
    """The URL in the invitation, pointing at the SPA's `/join` route.

    `quote` because the token is URL-safe base64 and base64's alphabet ends in
    `-` and `_` -- safe today, but the encoding is the token's business rather
    than this function's assumption. `urljoin` against a trailing slash so a
    configured base URL with or without one produces the same link.
    """
    base = get_settings().app_base_url.rstrip("/") + "/"
    return urljoin(base, f"join?token={quote(raw_token, safe='')}")


def build_invite_email(
    *,
    to: str,
    workspace_name: str,
    invited_by: User,
    raw_token: str,
    expires_at: datetime,
) -> OutboundEmail:
    """The invitation message.

    Deliberately plain text and deliberately short. The token is a bearer
    credential (SPEC-v2 D6): whoever holds this email holds the invite, so it
    says who sent it and when it stops working, and carries nothing else about
    the workspace that a stranger receiving it by mistake should not see.
    """
    link = invite_link(raw_token)
    inviter = invited_by.name or invited_by.email
    return OutboundEmail(
        to=[to],
        subject=f"{inviter} has invited you to {workspace_name}",
        body=(
            f"{inviter} has invited you to join the '{workspace_name}' workspace "
            f"on AI Workspace Assistant.\n\n"
            f"Accept the invitation:\n{link}\n\n"
            f"You will need an account under this email address -- sign in, or "
            f"create one, and the link will still work.\n\n"
            f"This invitation expires on {expires_at.date().isoformat()}.\n"
            f"If you were not expecting it, you can ignore this message."
        ),
        # Replies reach the colleague who sent the invite, not the no-reply
        # sender -- the same split SPEC-v2 D16 applies to agent mail.
        reply_to=invited_by.email,
    )


async def create_invite(
    db: AsyncSession,
    *,
    mailer: EmailProvider,
    workspace_id: uuid.UUID,
    invited_by: User,
    email: str,
    role: WorkspaceRole,
) -> tuple[WorkspaceInvite, str, bool]:
    """Create an invite and email the link to it.

    Returns the invite, the raw token, and whether the email was delivered.

    The send is deliberately *after* the commit and deliberately not allowed to
    fail the request. An invite that exists but was not emailed is recoverable
    -- the raw token comes back in this response and the UI shows it once, so
    an admin can pass on the link by hand -- whereas rolling the row back over
    a provider outage loses the invite as well as the email. The caller is told
    which of the two happened rather than having to assume.
    """
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

    # Asked before the send rather than inferred from it: the console provider
    # accepts every message and reports success, so in a production deployment
    # that has not been given a real provider, `email_sent: true` would be a
    # lie and the admin would never think to pass the link on by hand.
    blocked = email_service.delivery_blocked()
    if blocked is not None:
        logger.warning(
            "invite created but this deployment cannot send email",
            extra={"invite_id": str(invite.id), "reason": blocked},
        )
        return invite, raw_token, False

    workspace = await db.get(Workspace, workspace_id)
    message = build_invite_email(
        to=email,
        workspace_name=workspace.name if workspace is not None else "your workspace",
        invited_by=invited_by,
        raw_token=raw_token,
        expires_at=invite.expires_at,
    )

    try:
        await mailer.send(message)
    except AppError as exc:
        # Nothing from the message is logged: the body contains the token.
        logger.warning(
            "invite created but the email could not be sent",
            extra={"invite_id": str(invite.id), "reason": str(exc.detail)},
        )
        return invite, raw_token, False

    return invite, raw_token, True


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
