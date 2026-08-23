from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.tests.factories import auth_headers, make_member, make_user, make_workspace


async def test_create_workspace_makes_creator_admin(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    owner = await make_user(db_session, email="owner@example.com")

    create = await client.post(
        "/workspaces", json={"name": "Acme"}, headers=auth_headers(owner)
    )
    assert create.status_code == 201
    workspace_id = create.json()["id"]

    members = await client.get(
        f"/workspaces/{workspace_id}/members", headers=auth_headers(owner)
    )
    assert members.status_code == 200
    assert len(members.json()) == 1
    assert members.json()[0]["role"] == "admin"
    assert members.json()[0]["user"]["email"] == "owner@example.com"


async def test_list_workspaces_only_shows_workspaces_you_belong_to(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    owner_a = await make_user(db_session, email="a@example.com")
    owner_b = await make_user(db_session, email="b@example.com")
    await make_workspace(db_session, owner=owner_a, name="A's workspace")
    await make_workspace(db_session, owner=owner_b, name="B's workspace")

    response = await client.get("/workspaces", headers=auth_headers(owner_a))

    assert response.status_code == 200
    names = [w["name"] for w in response.json()]
    assert names == ["A's workspace"]


async def test_admin_can_invite_and_member_can_accept(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    admin = await make_user(db_session, email="admin@example.com")
    invitee = await make_user(db_session, email="invitee@example.com")
    workspace = await make_workspace(db_session, owner=admin)

    invite = await client.post(
        f"/workspaces/{workspace.id}/invite",
        json={"email": "invitee@example.com", "role": "member"},
        headers=auth_headers(admin),
    )
    assert invite.status_code == 201
    token = invite.json()["token"]
    assert token

    join = await client.post(
        "/workspaces/join", json={"token": token}, headers=auth_headers(invitee)
    )
    assert join.status_code == 200
    assert join.json()["role"] == "member"

    members = await client.get(
        f"/workspaces/{workspace.id}/members", headers=auth_headers(admin)
    )
    emails = {m["user"]["email"] for m in members.json()}
    assert emails == {"admin@example.com", "invitee@example.com"}


async def test_invite_by_non_admin_is_forbidden(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    admin = await make_user(db_session, email="admin2@example.com")
    member = await make_user(db_session, email="member2@example.com")
    workspace = await make_workspace(db_session, owner=admin)
    await make_member(db_session, workspace=workspace, user=member)

    response = await client.post(
        f"/workspaces/{workspace.id}/invite",
        json={"email": "someone@example.com", "role": "member"},
        headers=auth_headers(member),
    )

    assert response.status_code == 403


async def test_join_with_wrong_email_is_refused(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    admin = await make_user(db_session, email="admin3@example.com")
    other_user = await make_user(db_session, email="notinvited@example.com")
    workspace = await make_workspace(db_session, owner=admin)

    invite = await client.post(
        f"/workspaces/{workspace.id}/invite",
        json={"email": "intended@example.com", "role": "member"},
        headers=auth_headers(admin),
    )
    token = invite.json()["token"]

    response = await client.post(
        "/workspaces/join", json={"token": token}, headers=auth_headers(other_user)
    )

    assert response.status_code == 404


async def test_join_with_bogus_token_is_not_found(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    user = await make_user(db_session, email="bogus@example.com")

    response = await client.post(
        "/workspaces/join", json={"token": "not-a-real-token"}, headers=auth_headers(user)
    )

    assert response.status_code == 404


async def test_admin_can_change_member_role(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    admin = await make_user(db_session, email="admin4@example.com")
    member = await make_user(db_session, email="member4@example.com")
    workspace = await make_workspace(db_session, owner=admin)
    await make_member(db_session, workspace=workspace, user=member)

    response = await client.patch(
        f"/workspaces/{workspace.id}/members/{member.id}",
        json={"role": "admin"},
        headers=auth_headers(admin),
    )

    assert response.status_code == 200
    assert response.json()["role"] == "admin"


async def test_non_admin_cannot_change_roles(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    admin = await make_user(db_session, email="admin5@example.com")
    member = await make_user(db_session, email="member5@example.com")
    workspace = await make_workspace(db_session, owner=admin)
    await make_member(db_session, workspace=workspace, user=member)

    response = await client.patch(
        f"/workspaces/{workspace.id}/members/{admin.id}",
        json={"role": "member"},
        headers=auth_headers(member),
    )

    assert response.status_code == 403


async def test_last_admin_cannot_demote_self(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    admin = await make_user(db_session, email="soleadmin@example.com")
    workspace = await make_workspace(db_session, owner=admin)

    response = await client.patch(
        f"/workspaces/{workspace.id}/members/{admin.id}",
        json={"role": "member"},
        headers=auth_headers(admin),
    )

    assert response.status_code == 409


async def test_last_admin_cannot_leave(db_session: AsyncSession, client: AsyncClient) -> None:
    admin = await make_user(db_session, email="soleadmin2@example.com")
    workspace = await make_workspace(db_session, owner=admin)

    response = await client.delete(
        f"/workspaces/{workspace.id}/members/{admin.id}", headers=auth_headers(admin)
    )

    assert response.status_code == 409


async def test_member_can_leave_workspace(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    admin = await make_user(db_session, email="admin6@example.com")
    member = await make_user(db_session, email="member6@example.com")
    workspace = await make_workspace(db_session, owner=admin)
    await make_member(db_session, workspace=workspace, user=member)

    response = await client.delete(
        f"/workspaces/{workspace.id}/members/{member.id}", headers=auth_headers(member)
    )

    assert response.status_code == 204


async def test_admin_can_remove_another_member(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    admin = await make_user(db_session, email="admin7@example.com")
    member = await make_user(db_session, email="member7@example.com")
    workspace = await make_workspace(db_session, owner=admin)
    await make_member(db_session, workspace=workspace, user=member)

    response = await client.delete(
        f"/workspaces/{workspace.id}/members/{member.id}", headers=auth_headers(admin)
    )

    assert response.status_code == 204


async def test_member_cannot_remove_another_member(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    admin = await make_user(db_session, email="admin8@example.com")
    member_a = await make_user(db_session, email="membera8@example.com")
    member_b = await make_user(db_session, email="memberb8@example.com")
    workspace = await make_workspace(db_session, owner=admin)
    await make_member(db_session, workspace=workspace, user=member_a)
    await make_member(db_session, workspace=workspace, user=member_b)

    response = await client.delete(
        f"/workspaces/{workspace.id}/members/{member_b.id}", headers=auth_headers(member_a)
    )

    assert response.status_code == 403
