import hashlib
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.jwt import create_access_token
from app.auth.password import hash_password
from app.constants import (
    DocumentStatus,
    PendingActionStatus,
    PendingActionType,
    TaskStatus,
    UsageKind,
    WorkspaceRole,
)
from app.database.models.calendar_event import CalendarEvent
from app.database.models.chat import ChatThread
from app.database.models.document import Document
from app.database.models.membership import WorkspaceMember
from app.database.models.pending_action import PendingAction
from app.database.models.task import Task
from app.database.models.usage_event import UsageEvent
from app.database.models.user import User
from app.database.models.workspace import Workspace
from app.services.payload import hash_payload


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


def _pdf_escape(text: str) -> str:
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def make_pdf_bytes(pages: Sequence[str]) -> bytes:
    """Build a small but structurally valid multi-page PDF containing real text.

    pypdf can manipulate PDFs but cannot author text content, and checking a
    binary fixture into the repo would leave a blob nobody can read or adjust.
    This writes the objects out with a correctly computed xref table, so
    page-number preservation is tested against a genuine PDF parse rather
    than a stub.
    """
    font_num = 3
    page_nums = [4 + 2 * index for index in range(len(pages))]
    content_nums = [5 + 2 * index for index in range(len(pages))]

    kids = " ".join(f"{num} 0 R" for num in page_nums)
    bodies: dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode("ascii"),
        font_num: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    }

    for text, page_num, content_num in zip(pages, page_nums, content_nums, strict=True):
        bodies[page_num] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Contents {content_num} 0 R "
            f"/Resources << /Font << /F1 {font_num} 0 R >> >> >>"
        ).encode("ascii")

        instructions = ["BT", "/F1 12 Tf", "14 TL", "72 720 Td"]
        for line in text.split("\n"):
            instructions.append(f"({_pdf_escape(line)}) Tj")
            instructions.append("T*")
        instructions.append("ET")

        stream = "\n".join(instructions).encode("utf-8")
        bodies[content_num] = (
            f"<< /Length {len(stream)} >>\nstream\n".encode("ascii") + stream + b"\nendstream"
        )

    out = bytearray(b"%PDF-1.4\n")
    offsets: dict[int, int] = {}
    for num in sorted(bodies):
        offsets[num] = len(out)
        out += f"{num} 0 obj\n".encode("ascii") + bodies[num] + b"\nendobj\n"

    xref_offset = len(out)
    size = max(bodies) + 1
    out += f"xref\n0 {size}\n".encode("ascii")
    out += b"0000000000 65535 f \n"
    for num in range(1, size):
        out += f"{offsets[num]:010d} 00000 n \n".encode("ascii")
    out += (
        f"trailer\n<< /Size {size} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n"
    ).encode("ascii")

    return bytes(out)


async def make_indexed_document(
    db: AsyncSession,
    *,
    workspace: Workspace,
    uploaded_by: User,
    name: str = "handbook.txt",
    texts: Sequence[str] = ("Some indexed content.",),
) -> Document:
    """A `ready` document with real chunks and real embeddings, inserted
    directly.

    Bypasses upload and the ingestion queue on purpose: retrieval and chat
    tests are about what happens once a corpus exists, and driving the whole
    of Step 3 and Step 4 to arrive there would make them slow and would mean
    a break in ingestion failed these suites too.
    """
    from app.ai.embeddings.embedder import FakeEmbedder
    from app.database.models.chunk import DocumentChunk

    document = Document(
        workspace_id=workspace.id,
        name=name,
        storage_key=f"{workspace.id}/{uuid.uuid4()}-{name}",
        content_hash=uuid.uuid4().hex,
        mime_type="text/plain",
        size_bytes=sum(len(text) for text in texts),
        uploaded_by=uploaded_by.id,
        status=DocumentStatus.READY,
        chunk_count=len(texts),
    )
    db.add(document)
    await db.flush()

    vectors = await FakeEmbedder().embed_documents(list(texts))
    db.add_all(
        [
            DocumentChunk(
                workspace_id=workspace.id,
                document_id=document.id,
                text=text,
                page_no=index + 1,
                chunk_index=index,
                embedding=embedding,
            )
            for index, (text, embedding) in enumerate(zip(texts, vectors, strict=True))
        ]
    )
    await db.commit()
    await db.refresh(document)
    return document


async def make_chat_thread(
    db: AsyncSession,
    *,
    workspace: Workspace,
    user: User,
    title: str = "Existing conversation",
) -> ChatThread:
    thread = ChatThread(workspace_id=workspace.id, user_id=user.id, title=title)
    db.add(thread)
    await db.commit()
    await db.refresh(thread)
    return thread


async def make_pending_action(
    db: AsyncSession,
    *,
    workspace: Workspace,
    thread: ChatThread,
    user: User,
) -> PendingAction:
    payload = {
        "type": "send_email",
        "recipients": [{"user_id": str(user.id), "name": user.name, "email": user.email}],
        "subject": "Existing proposal",
        "body": "Body.",
    }
    action = PendingAction(
        workspace_id=workspace.id,
        thread_id=thread.id,
        type=PendingActionType.SEND_EMAIL,
        payload=payload,
        payload_hash=hash_payload(payload),
        status=PendingActionStatus.PENDING,
        initiated_by=user.id,
    )
    db.add(action)
    await db.commit()
    await db.refresh(action)
    return action


async def make_task(
    db: AsyncSession,
    *,
    workspace: Workspace,
    created_by: User,
    title: str = "Write the handover note",
    assigned_to: User | None = None,
    status: TaskStatus = TaskStatus.TODO,
) -> Task:
    task = Task(
        workspace_id=workspace.id,
        title=title,
        description="",
        assigned_to=assigned_to.id if assigned_to is not None else None,
        status=status,
        created_by=created_by.id,
    )
    db.add(task)
    await db.commit()
    await db.refresh(task)
    return task


async def make_calendar_event(
    db: AsyncSession,
    *,
    workspace: Workspace,
    created_by: User,
    title: str = "Quarterly review",
    guests: Sequence[User] = (),
) -> CalendarEvent:
    event = CalendarEvent(
        workspace_id=workspace.id,
        title=title,
        description="",
        start_time=datetime(2026, 9, 1, 10, 0, tzinfo=UTC),
        end_time=datetime(2026, 9, 1, 11, 0, tzinfo=UTC),
        created_by=created_by.id,
        ics_uid=f"{uuid.uuid4()}@ai-workspace-assistant",
        guests=[
            {"user_id": str(guest.id), "name": guest.name, "email": guest.email}
            for guest in guests
        ],
    )
    db.add(event)
    await db.commit()
    await db.refresh(event)
    return event


async def make_usage_event(
    db: AsyncSession,
    *,
    workspace: Workspace,
    user: User | None = None,
    kind: UsageKind = UsageKind.CHAT,
    tokens_in: int = 0,
    tokens_out: int = 0,
    estimated: bool = False,
) -> UsageEvent:
    """A token-spend row, inserted directly.

    Tests about the budget need a workspace that has already spent something,
    and getting there by asking real questions would make them slow and would
    couple a limit test to the whole retrieval path.
    """
    event = UsageEvent(
        workspace_id=workspace.id,
        user_id=user.id if user is not None else None,
        kind=kind,
        model="test-model",
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        estimated=estimated,
    )
    db.add(event)
    await db.commit()
    await db.refresh(event)
    return event
