"""The most important test file in the repo.

For every tenant-scoped route, a caller who isn't a member of the target
workspace must get 404 -- never 403. A 403 would confirm the resource exists,
which leaks tenant structure across a workspace boundary (see app/exceptions.py
and CLAUDE.md's architecture rules). Add a case here whenever a new
tenant-scoped route is added.
"""

import uuid
from collections.abc import Callable
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.tests.factories import auth_headers, make_member, make_user, make_workspace, random_email

# (method, path builder(workspace_id, a_member_user_id), body) for every route
# that depends on get_workspace_context, directly or via require_role.
RouteCase = tuple[str, Callable[[uuid.UUID, uuid.UUID], str], dict[str, Any] | None]
TENANT_SCOPED_ROUTES: list[RouteCase] = [
    ("GET", lambda ws_id, user_id: f"/workspaces/{ws_id}/members", None),
    (
        "POST",
        lambda ws_id, user_id: f"/workspaces/{ws_id}/invite",
        {"email": "target@example.com", "role": "member"},
    ),
    (
        "PATCH",
        lambda ws_id, user_id: f"/workspaces/{ws_id}/members/{user_id}",
        {"role": "admin"},
    ),
    ("DELETE", lambda ws_id, user_id: f"/workspaces/{ws_id}/members/{user_id}", None),
]


@pytest.mark.parametrize(("method", "build_path", "body"), TENANT_SCOPED_ROUTES)
async def test_cross_tenant_access_is_404_never_403(
    db_session: AsyncSession,
    client: AsyncClient,
    method: str,
    build_path: Callable[[uuid.UUID, uuid.UUID], str],
    body: dict[str, Any] | None,
) -> None:
    admin_a = await make_user(db_session, email=random_email())
    workspace_a = await make_workspace(db_session, owner=admin_a)

    # A genuine outsider: a member elsewhere, with no relationship at all to
    # workspace_a -- not even a rejected invite.
    outsider = await make_user(db_session, email=random_email())
    await make_workspace(db_session, owner=outsider)

    path = build_path(workspace_a.id, admin_a.id)
    response = await client.request(method, path, json=body, headers=auth_headers(outsider))

    assert response.status_code == 404, (
        f"{method} {path} leaked workspace existence: expected 404, got "
        f"{response.status_code}"
    )


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
