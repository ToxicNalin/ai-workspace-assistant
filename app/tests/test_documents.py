from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants import MAX_UPLOAD_SIZE_BYTES, WorkspaceRole
from app.tests.factories import (
    auth_headers,
    make_document,
    make_member,
    make_user,
    make_workspace,
)

_MINIMAL_PDF = b"%PDF-1.4\n%%EOF"


async def test_upload_creates_pending_document(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    user = await make_user(db_session, email="uploader@example.com")
    workspace = await make_workspace(db_session, owner=user)

    response = await client.post(
        f"/workspaces/{workspace.id}/documents/upload",
        files={"file": ("report.pdf", _MINIMAL_PDF, "application/pdf")},
        headers=auth_headers(user),
    )

    assert response.status_code == 201
    body = response.json()
    assert body["deduplicated"] is False
    assert body["document"]["status"] == "pending"
    assert body["document"]["mime_type"] == "application/pdf"
    assert body["document"]["size_bytes"] == len(_MINIMAL_PDF)


async def test_upload_lists_and_shows_status(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    user = await make_user(db_session, email="lister@example.com")
    workspace = await make_workspace(db_session, owner=user)

    upload = await client.post(
        f"/workspaces/{workspace.id}/documents/upload",
        files={"file": ("notes.txt", b"plain text content", "text/plain")},
        headers=auth_headers(user),
    )
    document_id = upload.json()["document"]["id"]

    listing = await client.get(
        f"/workspaces/{workspace.id}/documents", headers=auth_headers(user)
    )
    assert listing.status_code == 200
    assert len(listing.json()) == 1
    assert listing.json()[0]["id"] == document_id

    status_response = await client.get(
        f"/workspaces/{workspace.id}/documents/{document_id}/status",
        headers=auth_headers(user),
    )
    assert status_response.status_code == 200
    assert status_response.json()["status"] == "pending"


async def test_duplicate_upload_is_deduplicated(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    user = await make_user(db_session, email="dedup@example.com")
    workspace = await make_workspace(db_session, owner=user)
    content = b"identical bytes both times"

    first = await client.post(
        f"/workspaces/{workspace.id}/documents/upload",
        files={"file": ("a.txt", content, "text/plain")},
        headers=auth_headers(user),
    )
    second = await client.post(
        f"/workspaces/{workspace.id}/documents/upload",
        files={"file": ("b.txt", content, "text/plain")},
        headers=auth_headers(user),
    )

    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["deduplicated"] is True
    assert second.json()["document"]["id"] == first.json()["document"]["id"]

    listing = await client.get(
        f"/workspaces/{workspace.id}/documents", headers=auth_headers(user)
    )
    assert len(listing.json()) == 1


async def test_oversize_upload_is_rejected(db_session: AsyncSession, client: AsyncClient) -> None:
    user = await make_user(db_session, email="toobig@example.com")
    workspace = await make_workspace(db_session, owner=user)
    oversize_content = b"a" * (MAX_UPLOAD_SIZE_BYTES + 1)

    response = await client.post(
        f"/workspaces/{workspace.id}/documents/upload",
        files={"file": ("huge.txt", oversize_content, "text/plain")},
        headers=auth_headers(user),
    )

    assert response.status_code == 413


async def test_content_that_does_not_match_extension_is_rejected(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    user = await make_user(db_session, email="mismatch@example.com")
    workspace = await make_workspace(db_session, owner=user)

    # Plain text bytes, but claiming to be a PDF by extension.
    response = await client.post(
        f"/workspaces/{workspace.id}/documents/upload",
        files={"file": ("fake.pdf", b"this is not a pdf", "application/pdf")},
        headers=auth_headers(user),
    )

    assert response.status_code == 415


async def test_unrecognisable_binary_content_is_rejected(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    user = await make_user(db_session, email="binary@example.com")
    workspace = await make_workspace(db_session, owner=user)

    response = await client.post(
        f"/workspaces/{workspace.id}/documents/upload",
        files={"file": ("mystery.txt", b"\xff\xd8\xff\xe0binarygarbage\x00\x01", "text/plain")},
        headers=auth_headers(user),
    )

    assert response.status_code == 415


async def test_delete_document_removes_it(db_session: AsyncSession, client: AsyncClient) -> None:
    user = await make_user(db_session, email="deleter@example.com")
    workspace = await make_workspace(db_session, owner=user)
    document = await make_document(db_session, workspace=workspace, uploaded_by=user)

    response = await client.delete(
        f"/workspaces/{workspace.id}/documents/{document.id}", headers=auth_headers(user)
    )
    assert response.status_code == 204

    status_response = await client.get(
        f"/workspaces/{workspace.id}/documents/{document.id}/status",
        headers=auth_headers(user),
    )
    assert status_response.status_code == 404


async def test_viewer_cannot_upload_or_delete(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    admin = await make_user(db_session, email="admin-doc@example.com")
    viewer = await make_user(db_session, email="viewer-doc@example.com")
    workspace = await make_workspace(db_session, owner=admin)
    await make_member(db_session, workspace=workspace, user=viewer, role=WorkspaceRole.VIEWER)

    upload = await client.post(
        f"/workspaces/{workspace.id}/documents/upload",
        files={"file": ("x.txt", b"content", "text/plain")},
        headers=auth_headers(viewer),
    )
    assert upload.status_code == 403

    document = await make_document(db_session, workspace=workspace, uploaded_by=admin)
    delete = await client.delete(
        f"/workspaces/{workspace.id}/documents/{document.id}", headers=auth_headers(viewer)
    )
    assert delete.status_code == 403
