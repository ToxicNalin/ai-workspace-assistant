"""The most important test file in the repo.

For every tenant-scoped route, a caller who isn't a member of the target
workspace must get 404 -- never 403. A 403 would confirm the resource exists,
which leaks tenant structure across a workspace boundary (see app/exceptions.py
and CLAUDE.md's architecture rules). Add a case here whenever a new
tenant-scoped route is added.
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.tests.factories import (
    auth_headers,
    make_calendar_event,
    make_chat_thread,
    make_document,
    make_member,
    make_pending_action,
    make_task,
    make_user,
    make_workspace,
    random_email,
)


@dataclass(frozen=True)
class Victim:
    """Real, existing resources inside the workspace the outsider is probing.

    Every id here belongs to workspace A. That matters: the outsider must get
    404 for things that genuinely exist, which is a stronger claim than
    getting 404 for a random uuid that does not.
    """

    workspace_id: uuid.UUID
    member_id: uuid.UUID
    document_id: uuid.UUID
    thread_id: uuid.UUID
    action_id: uuid.UUID
    task_id: uuid.UUID
    event_id: uuid.UUID


# One case for every route that depends on get_workspace_context, directly or
# via require_role. Multipart upload is exercised separately below -- it can't
# share this json-body shape.
RouteCase = tuple[str, Callable[[Victim], str], dict[str, Any] | None]
TENANT_SCOPED_ROUTES: list[RouteCase] = [
    ("GET", lambda v: f"/workspaces/{v.workspace_id}/members", None),
    (
        "POST",
        lambda v: f"/workspaces/{v.workspace_id}/invite",
        {"email": "target@example.com", "role": "member"},
    ),
    (
        "PATCH",
        lambda v: f"/workspaces/{v.workspace_id}/members/{v.member_id}",
        {"role": "admin"},
    ),
    ("DELETE", lambda v: f"/workspaces/{v.workspace_id}/members/{v.member_id}", None),
    ("GET", lambda v: f"/workspaces/{v.workspace_id}/documents", None),
    ("GET", lambda v: f"/workspaces/{v.workspace_id}/documents/{v.document_id}/status", None),
    ("DELETE", lambda v: f"/workspaces/{v.workspace_id}/documents/{v.document_id}", None),
    # Step 5. Chat reads across the whole corpus at once, so a leak here is a
    # leak of every document in the workspace rather than a single row.
    (
        "POST",
        lambda v: f"/workspaces/{v.workspace_id}/chat/query",
        {"question": "What is in these documents?"},
    ),
    ("GET", lambda v: f"/workspaces/{v.workspace_id}/chat/threads", None),
    (
        "GET",
        lambda v: f"/workspaces/{v.workspace_id}/chat/threads/{v.thread_id}/history",
        None,
    ),
    # Step 6. The agent can propose actions and read the whole corpus, and the
    # approval routes decide whether something touches the world outside this
    # process -- the two most consequential things to get tenant scoping wrong.
    (
        "POST",
        lambda v: f"/workspaces/{v.workspace_id}/agent",
        {"message": "email the team"},
    ),
    ("GET", lambda v: f"/workspaces/{v.workspace_id}/pending-actions", None),
    (
        "POST",
        lambda v: f"/workspaces/{v.workspace_id}/pending-actions/{v.action_id}/decide",
        {"decision": "approve", "payload_hash": "0" * 64},
    ),
    ("GET", lambda v: f"/workspaces/{v.workspace_id}/audit-log", None),
    # Step 7. Tasks and events name real people and carry real content, and
    # the email routes are the one place a message leaves this application --
    # so a workspace boundary that held everywhere else and not here would be
    # the leak that matters most.
    ("GET", lambda v: f"/workspaces/{v.workspace_id}/tasks", None),
    ("POST", lambda v: f"/workspaces/{v.workspace_id}/tasks", {"title": "Injected"}),
    ("GET", lambda v: f"/workspaces/{v.workspace_id}/tasks/{v.task_id}", None),
    (
        "PATCH",
        lambda v: f"/workspaces/{v.workspace_id}/tasks/{v.task_id}",
        {"status": "done"},
    ),
    ("DELETE", lambda v: f"/workspaces/{v.workspace_id}/tasks/{v.task_id}", None),
    ("GET", lambda v: f"/workspaces/{v.workspace_id}/events", None),
    (
        "POST",
        lambda v: f"/workspaces/{v.workspace_id}/events",
        {
            "title": "Injected",
            "start_time": "2026-09-01T10:00:00+00:00",
            "end_time": "2026-09-01T11:00:00+00:00",
            "guests": [],
        },
    ),
    ("GET", lambda v: f"/workspaces/{v.workspace_id}/events/{v.event_id}", None),
    ("GET", lambda v: f"/workspaces/{v.workspace_id}/events/{v.event_id}/ics", None),
    ("GET", lambda v: f"/workspaces/{v.workspace_id}/email/status", None),
    (
        "POST",
        lambda v: f"/workspaces/{v.workspace_id}/email/send",
        {"recipients": ["Someone"], "subject": "Injected", "body": "Body."},
    ),
    # Step 8. The stream reads the whole corpus like /chat/query does, and it
    # has to refuse before the event stream opens -- once a 200 has gone out
    # there is no status code left to say no with.
    (
        "GET",
        lambda v: f"/workspaces/{v.workspace_id}/chat/stream?question=anything",
        None,
    ),
    # The admin views are the only routes here whose purpose is to report on
    # other people. BUILD-ORDER names them /admin/usage and /admin/users
    # unqualified; they are workspace-scoped instead, precisely so these two
    # cases can exist -- see the module docstring in app/api/admin.py.
    ("GET", lambda v: f"/workspaces/{v.workspace_id}/admin/usage", None),
    ("GET", lambda v: f"/workspaces/{v.workspace_id}/admin/users", None),
]


@pytest.mark.parametrize(("method", "build_path", "body"), TENANT_SCOPED_ROUTES)
async def test_cross_tenant_access_is_404_never_403(
    db_session: AsyncSession,
    client: AsyncClient,
    method: str,
    build_path: Callable[[Victim], str],
    body: dict[str, Any] | None,
) -> None:
    admin_a = await make_user(db_session, email=random_email())
    workspace_a = await make_workspace(db_session, owner=admin_a)
    document_a = await make_document(db_session, workspace=workspace_a, uploaded_by=admin_a)
    thread_a = await make_chat_thread(db_session, workspace=workspace_a, user=admin_a)
    action_a = await make_pending_action(
        db_session, workspace=workspace_a, thread=thread_a, user=admin_a
    )
    task_a = await make_task(db_session, workspace=workspace_a, created_by=admin_a)
    event_a = await make_calendar_event(
        db_session, workspace=workspace_a, created_by=admin_a
    )

    # A genuine outsider: a member elsewhere, with no relationship at all to
    # workspace_a -- not even a rejected invite.
    outsider = await make_user(db_session, email=random_email())
    await make_workspace(db_session, owner=outsider)

    victim = Victim(
        workspace_id=workspace_a.id,
        member_id=admin_a.id,
        document_id=document_a.id,
        thread_id=thread_a.id,
        action_id=action_a.id,
        task_id=task_a.id,
        event_id=event_a.id,
    )
    path = build_path(victim)
    response = await client.request(method, path, json=body, headers=auth_headers(outsider))

    assert response.status_code == 404, (
        f"{method} {path} leaked workspace existence: expected 404, got "
        f"{response.status_code}"
    )


async def test_upload_to_a_foreign_workspace_is_404_never_403(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    """Upload needs its own case: multipart form data doesn't fit the
    json-body shape the parametrized cases above share."""
    admin_a = await make_user(db_session, email=random_email())
    workspace_a = await make_workspace(db_session, owner=admin_a)

    outsider = await make_user(db_session, email=random_email())
    await make_workspace(db_session, owner=outsider)

    response = await client.post(
        f"/workspaces/{workspace_a.id}/documents/upload",
        files={"file": ("x.txt", b"content", "text/plain")},
        headers=auth_headers(outsider),
    )

    assert response.status_code == 404


async def test_nonexistent_workspace_is_also_404(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    user = await make_user(db_session, email=random_email())

    response = await client.get(f"/workspaces/{uuid.uuid4()}/members", headers=auth_headers(user))

    assert response.status_code == 404


async def test_insufficient_role_in_own_workspace_is_403_not_404(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    """The contrast case: a real member of the workspace, just underpowered.

    This must render differently from the cross-tenant case above -- if it
    also came back 404, that would mean the app is just returning 404 for
    every rejection, which would make the tests above pass for the wrong
    reason.
    """
    admin = await make_user(db_session, email=random_email())
    member = await make_user(db_session, email=random_email())
    workspace = await make_workspace(db_session, owner=admin)
    await make_member(db_session, workspace=workspace, user=member)

    response = await client.post(
        f"/workspaces/{workspace.id}/invite",
        json={"email": random_email(), "role": "member"},
        headers=auth_headers(member),
    )

    assert response.status_code == 403


async def test_unauthenticated_request_is_401_not_404_or_403(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    admin = await make_user(db_session, email=random_email())
    workspace = await make_workspace(db_session, owner=admin)

    response = await client.get(f"/workspaces/{workspace.id}/members")

    assert response.status_code == 401


async def test_join_leaks_nothing_about_a_workspace_it_does_not_belong_to(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    """accept_invite isn't workspace_id-scoped in the path, but it must still
    never let a token minted for one email/workspace be usable by anyone else."""
    admin = await make_user(db_session, email=random_email())
    workspace = await make_workspace(db_session, owner=admin)
    invitee = await make_user(db_session, email="realinvitee@example.com")
    wrong_person = await make_user(db_session, email=random_email())

    invite = await client.post(
        f"/workspaces/{workspace.id}/invite",
        json={"email": "realinvitee@example.com", "role": "member"},
        headers=auth_headers(admin),
    )
    token = invite.json()["token"]

    response = await client.post(
        "/workspaces/join", json={"token": token}, headers=auth_headers(wrong_person)
    )

    assert response.status_code == 404

    # The real invitee can still use it -- confirms the invite wasn't consumed
    # or corrupted by the failed attempt above.
    real_response = await client.post(
        "/workspaces/join", json={"token": token}, headers=auth_headers(invitee)
    )
    assert real_response.status_code == 200
