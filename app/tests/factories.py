import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.jwt import create_access_token
from app.auth.password import hash_password
from app.constants import WorkspaceRole
from app.database.models.membership import WorkspaceMember
from app.database.models.user import User
from app.database.models.workspace import Workspace


async def make_user(
    db: AsyncSession,
    *,
    email: str = "user@example.com",
    password: str = "password123",
    name: str = "Test User",
) -> User:
    user = User(email=email.lower(), password_hash=hash_password(password), name=name)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def make_workspace(
    db: AsyncSession, *, owner: User, name: str = "Test Workspace"
) -> Workspace:
    workspace = Workspace(name=name, owner_id=owner.id)
    db.add(workspace)
    await db.flush()

    db.add(WorkspaceMember(workspace_id=workspace.id, user_id=owner.id, role=WorkspaceRole.ADMIN))
    await db.commit()
    await db.refresh(workspace)
    return workspace


async def make_member(
    db: AsyncSession,
    *,
    workspace: Workspace,
    user: User,
    role: WorkspaceRole = WorkspaceRole.MEMBER,
) -> WorkspaceMember:
    membership = WorkspaceMember(workspace_id=workspace.id, user_id=user.id, role=role)
    db.add(membership)
    await db.commit()
    await db.refresh(membership)
    return membership


def auth_headers(user: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(user.id)}"}


def random_email() -> str:
    return f"{uuid.uuid4().hex}@example.com"
