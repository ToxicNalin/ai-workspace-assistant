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
    # Only ever populated on the response to the creation call.
    token: str | None = None


class InviteAccept(BaseModel):
    token: str
