"""Server-side resolution of people to email addresses.

This is SPEC-v2 D21, and per §5 it is "the one that actually stops
exfiltration". The reasoning is worth restating, because the module looks
trivial and is not:

The model sits downstream of untrusted document text. If the model can emit an
arbitrary email address, then a poisoned PDF has a channel out -- and the
approval dialogue would faithfully display a plausible-looking address next to
a plausible-looking subject line, which a human approves. Delimiters and system
prompts raise the bar; the payload hash catches tampering after the fact. Only
this removes the channel.

So the model names a person. The server maps that name to an address, from
`workspace_members` and nowhere else. Anything that does not resolve to a
current member of *this* workspace is refused outright -- never offered for
approval, never shown to a human as a choice.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.membership import WorkspaceMember
from app.database.models.user import User


class UnresolvableRecipient(Exception):
    """A named person is not a member of this workspace.

    Deliberately not a subclass of AppError: this never becomes an HTTP status.
    It aborts the action inside the agent flow, which is recorded as a refusal.
    """

    def __init__(self, reference: str) -> None:
        self.reference = reference
        super().__init__(
            f"'{reference}' is not a member of this workspace, so it cannot be used "
            f"as a recipient. Only current workspace members can be contacted."
        )


@dataclass(frozen=True)
class ResolvedMember:
    user_id: uuid.UUID
    name: str
    email: str


async def resolve_member(
    db: AsyncSession, *, workspace_id: uuid.UUID, reference: str
) -> ResolvedMember:
    """Map one model-supplied reference to a real member of this workspace.

    A reference may be a member's name or their address -- but supplying an
    address buys the caller nothing, because it is still looked up in
    `workspace_members` and rejected if it is not there. There is no branch in
    which a string from the model is used as an address without first being
    found in this workspace.
    """
    needle = reference.strip().lower()
    if not needle:
        raise UnresolvableRecipient(reference)

    result = await db.scalars(
        select(User)
        .join(WorkspaceMember, WorkspaceMember.user_id == User.id)
        .where(
            WorkspaceMember.workspace_id == workspace_id,
            User.is_active.is_(True),
            (func.lower(User.email) == needle) | (func.lower(User.name) == needle),
        )
        .order_by(User.email)
    )
    matches = result.all()

    if len(matches) != 1:
        # Zero matches is the attack case. More than one is ambiguity, and
        # guessing which colleague was meant is not the server's call to make.
        raise UnresolvableRecipient(reference)

    user = matches[0]
    return ResolvedMember(user_id=user.id, name=user.name, email=user.email)


async def resolve_members(
    db: AsyncSession, *, workspace_id: uuid.UUID, references: Sequence[str]
) -> list[ResolvedMember]:
    """All or nothing. One unresolvable name fails the whole action rather than
    quietly sending to the subset that happened to resolve."""
    if not references:
        raise UnresolvableRecipient("(no recipients given)")

    return [
        await resolve_member(db, workspace_id=workspace_id, reference=reference)
        for reference in references
    ]
