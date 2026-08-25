"""Step 7: task CRUD, and the one rule that is not CRUD.

SPEC-v2 D3 puts `assigned_to` on `users` rather than on `workspace_members`,
because membership rows are deleted and recreated when somebody leaves and
rejoins. The foreign key therefore cannot enforce "this person is in this
workspace", so the service has to -- on every write, for every path. Most of
the cases below are about that check rather than about tasks.
"""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants import TaskStatus, WorkspaceRole
from app.database.models.task import Task
from app.tests.factories import (
    auth_headers,
    make_member,
    make_task,
    make_user,
    make_workspace,
    random_email,
)


async def test_creating_and_listing_a_task(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    admin = await make_user(db_session, email=random_email(), name="Alice Smith")
    workspace = await make_workspace(db_session, owner=admin)

    created = await client.post(
        f"/workspaces/{workspace.id}/tasks",
        json={"title": "Write the handover note", "description": "Before Friday."},
        headers=auth_headers(admin),
    )
    assert created.status_code == 201, created.text
    assert created.json()["status"] == "todo"
    assert created.json()["created_by"] == str(admin.id)

    listed = await client.get(
        f"/workspaces/{workspace.id}/tasks", headers=auth_headers(admin)
    )
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()] == [created.json()["id"]]


async def test_a_task_can_be_assigned_to_a_member(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    admin = await make_user(db_session, email=random_email(), name="Alice Smith")
    colleague = await make_user(db_session, email=random_email(), name="Bob Jones")
    workspace = await make_workspace(db_session, owner=admin)
    await make_member(db_session, workspace=workspace, user=colleague)

    response = await client.post(
        f"/workspaces/{workspace.id}/tasks",
        json={"title": "Review the draft", "assigned_to": str(colleague.id)},
        headers=auth_headers(admin),
    )

    assert response.status_code == 201, response.text
    assert response.json()["assigned_to"] == str(colleague.id)


async def test_a_task_cannot_be_assigned_to_someone_outside_the_workspace(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    """The check a foreign key cannot make (D3).

    404 rather than 403 or 422: the id came from the client, and answering
    "that user exists, but not here" would confirm the existence of a user in
    another tenant. Same leak as the cross-tenant path rule, through a body
    field instead of a URL.
    """
    admin = await make_user(db_session, email=random_email(), name="Alice Smith")
    workspace = await make_workspace(db_session, owner=admin)
    outsider = await make_user(db_session, email=random_email(), name="Mallory")
    await make_workspace(db_session, owner=outsider, name="Somewhere else")

    response = await client.post(
        f"/workspaces/{workspace.id}/tasks",
        json={"title": "Leak something", "assigned_to": str(outsider.id)},
        headers=auth_headers(admin),
    )

    assert response.status_code == 404
    assert await db_session.scalar(select(Task).where(Task.workspace_id == workspace.id)) is None


async def test_reassignment_is_checked_too(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    """A task created legitimately must not become a way to reach a non-member
    by patching it afterwards."""
    admin = await make_user(db_session, email=random_email(), name="Alice Smith")
    workspace = await make_workspace(db_session, owner=admin)
    task = await make_task(db_session, workspace=workspace, created_by=admin)

    outsider = await make_user(db_session, email=random_email(), name="Mallory")
    await make_workspace(db_session, owner=outsider, name="Somewhere else")

    response = await client.patch(
        f"/workspaces/{workspace.id}/tasks/{task.id}",
        json={"assigned_to": str(outsider.id)},
        headers=auth_headers(admin),
    )

    assert response.status_code == 404
    await db_session.refresh(task)
    assert task.assigned_to is None


async def test_a_task_can_be_updated_and_unassigned(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    """`assigned_to: null` unassigns; omitting the key leaves the assignee alone.

    Those are different intentions, which is why the service applies
    exclude_unset rather than exclude_none.
    """
    admin = await make_user(db_session, email=random_email(), name="Alice Smith")
    workspace = await make_workspace(db_session, owner=admin)
    task = await make_task(
        db_session, workspace=workspace, created_by=admin, assigned_to=admin
    )

    progressed = await client.patch(
        f"/workspaces/{workspace.id}/tasks/{task.id}",
        json={"status": "in_progress"},
        headers=auth_headers(admin),
    )
    assert progressed.status_code == 200
    assert progressed.json()["status"] == "in_progress"
    assert progressed.json()["assigned_to"] == str(admin.id), "an absent key changed nothing"

    unassigned = await client.patch(
        f"/workspaces/{workspace.id}/tasks/{task.id}",
        json={"assigned_to": None},
        headers=auth_headers(admin),
    )
    assert unassigned.status_code == 200
    assert unassigned.json()["assigned_to"] is None


async def test_tasks_can_be_filtered(db_session: AsyncSession, client: AsyncClient) -> None:
    admin = await make_user(db_session, email=random_email(), name="Alice Smith")
    workspace = await make_workspace(db_session, owner=admin)
    await make_task(db_session, workspace=workspace, created_by=admin, title="Open one")
    done = await make_task(
        db_session,
        workspace=workspace,
        created_by=admin,
        title="Finished one",
        status=TaskStatus.DONE,
    )

    response = await client.get(
        f"/workspaces/{workspace.id}/tasks?status=done", headers=auth_headers(admin)
    )

    assert response.status_code == 200
    assert [item["id"] for item in response.json()] == [str(done.id)]


async def test_a_task_can_be_deleted(db_session: AsyncSession, client: AsyncClient) -> None:
    admin = await make_user(db_session, email=random_email(), name="Alice Smith")
    workspace = await make_workspace(db_session, owner=admin)
    task = await make_task(db_session, workspace=workspace, created_by=admin)

    response = await client.delete(
        f"/workspaces/{workspace.id}/tasks/{task.id}", headers=auth_headers(admin)
    )

    assert response.status_code == 204
    assert await db_session.get(Task, task.id) is None


async def test_a_viewer_can_read_but_not_write(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    """A real member of the workspace, just underpowered -- so 403, not 404."""
    admin = await make_user(db_session, email=random_email(), name="Alice Smith")
    viewer = await make_user(db_session, email=random_email(), name="Vic Viewer")
    workspace = await make_workspace(db_session, owner=admin)
    await make_member(db_session, workspace=workspace, user=viewer, role=WorkspaceRole.VIEWER)
    await make_task(db_session, workspace=workspace, created_by=admin)

    readable = await client.get(
        f"/workspaces/{workspace.id}/tasks", headers=auth_headers(viewer)
    )
    assert readable.status_code == 200
    assert len(readable.json()) == 1

    writable = await client.post(
        f"/workspaces/{workspace.id}/tasks",
        json={"title": "Not allowed"},
        headers=auth_headers(viewer),
    )
    assert writable.status_code == 403


async def test_a_task_from_another_workspace_is_404(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    admin_a = await make_user(db_session, email=random_email())
    workspace_a = await make_workspace(db_session, owner=admin_a)
    task_a = await make_task(db_session, workspace=workspace_a, created_by=admin_a)

    admin_b = await make_user(db_session, email=random_email())
    workspace_b = await make_workspace(db_session, owner=admin_b, name="Other")

    # Their own workspace, someone else's task id. The path is legitimate for
    # this caller, so only the workspace_id filter on the query stops it.
    response = await client.get(
        f"/workspaces/{workspace_b.id}/tasks/{task_a.id}", headers=auth_headers(admin_b)
    )

    assert response.status_code == 404


@pytest.mark.parametrize("bad_title", ["", "x" * 256])
async def test_a_task_title_is_bounded(
    db_session: AsyncSession, client: AsyncClient, bad_title: str
) -> None:
    """Empty and oversized are both 422 -- a title column is VARCHAR(255), and
    finding that out as a database error would be a 500."""
    admin = await make_user(db_session, email=random_email())
    workspace = await make_workspace(db_session, owner=admin)

    response = await client.post(
        f"/workspaces/{workspace.id}/tasks",
        json={"title": bad_title},
        headers=auth_headers(admin),
    )

    assert response.status_code == 422


async def test_an_unknown_task_is_404(db_session: AsyncSession, client: AsyncClient) -> None:
    admin = await make_user(db_session, email=random_email())
    workspace = await make_workspace(db_session, owner=admin)

    response = await client.get(
        f"/workspaces/{workspace.id}/tasks/{uuid.uuid4()}", headers=auth_headers(admin)
    )

    assert response.status_code == 404
