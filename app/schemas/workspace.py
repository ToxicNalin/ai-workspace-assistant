import uuid
from datetime import datetime

from pydantic import BaseModel, EmailStr, Field

from app.constants import WorkspaceRole
from app.schemas.auth import UserOut
from app.schemas.common import ORMModel


class WorkspaceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)


class WorkspaceOut(ORMModel):
    id: uuid.UUID
    name: str
    owner_id: uuid.UUID
    created_at: datetime


class MemberOut(ORMModel):
    id: uuid.UUID
    user: UserOut
    role: WorkspaceRole
    joined_at: datetime


class MemberRoleUpdate(BaseModel):
    role: WorkspaceRole


class InviteCreate(BaseModel):
    email: EmailStr
    role: WorkspaceRole = WorkspaceRole.MEMBER


class InviteOut(ORMModel):
    id: uuid.UUID
    email: str
    role: WorkspaceRole
    status: str
    expires_at: datetime
    # Both only ever populated on the response to the creation call.
    token: str | None = None
    # False means the invite exists but the invitation email did not go out,
    # so the token above is the only copy of the link there is and the admin
    # has to pass it on themselves.
    email_sent: bool | None = None
    # Why it did not. "Not sent" has two causes needing different responses --
    # a deployment with no mail provider is fixed in a dashboard, a provider
    # refusing one recipient is not -- and only the server knows which. Passing
    # the reason on is the same rule the provider layer follows: the component
    # that learns why a thing failed is the one that must say so.
    email_error: str | None = None


class InviteAccept(BaseModel):
    token: str
