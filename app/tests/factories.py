import hashlib
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.jwt import create_access_token
from app.auth.password import hash_password
from app.constants import DocumentStatus, WorkspaceRole
from app.database.models.document import Document
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


async def make_document(
    db: AsyncSession,
    *,
    workspace: Workspace,
    uploaded_by: User,
    name: str = "doc.txt",
    content: bytes = b"hello world",
) -> Document:
    """Inserts a document row directly, bypassing the storage backend --
    for tests that need an existing document but aren't exercising upload
    itself (status lookup, delete, cross-tenant access)."""
    document = Document(
        workspace_id=workspace.id,
        name=name,
        storage_key=f"{workspace.id}/{uuid.uuid4()}-{name}",
        content_hash=hashlib.sha256(content).hexdigest(),
        mime_type="text/plain",
        size_bytes=len(content),
        uploaded_by=uploaded_by.id,
        status=DocumentStatus.PENDING,
    )
    db.add(document)
    await db.commit()
    await db.refresh(document)
    return document


def auth_headers(user: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(user.id)}"}


def random_email() -> str:
    return f"{uuid.uuid4().hex}@example.com"
