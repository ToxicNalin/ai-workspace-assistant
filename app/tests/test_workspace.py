import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.exceptions import UpstreamFailure
from app.services import invite_service
from app.services.email_service import ConsoleEmailProvider, OutboundEmail, SentEmail
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


# --------------------------------------------------------------------------
# The invitation email.
#
# An invite whose link never reaches anybody is not an invite. These pin down
# that one goes out, that it carries a link the SPA's /join route can actually
# redeem, and -- the case that matters most -- that a mail provider failing
# does not lose the invite along with the email.
# --------------------------------------------------------------------------


async def test_creating_an_invite_emails_the_link(
    db_session: AsyncSession, client: AsyncClient, outbox: ConsoleEmailProvider
) -> None:
    admin = await make_user(db_session, email="inviter@example.com", name="Alice Smith")
    workspace = await make_workspace(db_session, owner=admin, name="Acme")

    response = await client.post(
        f"/workspaces/{workspace.id}/invite",
        json={"email": "Newcomer@Example.com", "role": "member"},
        headers=auth_headers(admin),
    )

    assert response.status_code == 201
    assert response.json()["email_sent"] is True
    assert response.json()["email_error"] is None

    assert len(outbox.outbox) == 1
    message = outbox.outbox[0]
    # Normalised to lower case by the service, so the address the invite is
    # checked against at redemption time is the address it was sent to.
    assert message.to == ["newcomer@example.com"]
    assert "Alice Smith" in message.subject
    assert "Acme" in message.subject
    # A reply reaches the colleague who invited them, not the no-reply sender.
    assert message.reply_to == "inviter@example.com"

    token = response.json()["token"]
    assert f"/join?token={token}" in message.body


async def test_the_invite_link_uses_the_configured_app_base_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The link is read outside the application, so it has to be absolute --
    and a trailing slash on the setting must not double up in the URL."""
    settings = get_settings()
    monkeypatch.setattr(settings, "app_base_url", "https://app.example.com/")

    assert invite_service.invite_link("abc-123") == "https://app.example.com/join?token=abc-123"


async def test_an_invite_survives_the_email_failing(
    db_session: AsyncSession, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The invite is committed before the send, so a provider outage costs the
    email and not the invitation. The raw token still comes back, which is the
    admin's way of passing the link on by hand."""

    class BrokenProvider(ConsoleEmailProvider):
        async def send(self, message: OutboundEmail) -> SentEmail:
            raise UpstreamFailure("The email provider could not be reached")

    monkeypatch.setattr("app.api.workspace.get_email_provider", BrokenProvider)

    admin = await make_user(db_session, email="inviter2@example.com")
    invitee = await make_user(db_session, email="invitee2@example.com")
    workspace = await make_workspace(db_session, owner=admin)

    response = await client.post(
        f"/workspaces/{workspace.id}/invite",
        json={"email": "invitee2@example.com", "role": "member"},
        headers=auth_headers(admin),
    )

    assert response.status_code == 201
    assert response.json()["email_sent"] is False
    # The provider's own words reach the admin. Without them the UI has to
    # guess between "no mail provider" and "this recipient was refused", which
    # need different responses and cannot be told apart from a boolean.
    assert response.json()["email_error"] == "The email provider could not be reached"
    token = response.json()["token"]
    assert token

    # And the invite genuinely works -- it was created, not rolled back.
    join = await client.post(
        "/workspaces/join", json={"token": token}, headers=auth_headers(invitee)
    )
    assert join.status_code == 200


async def test_a_production_deployment_that_cannot_send_says_so(
    db_session: AsyncSession,
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    outbox: ConsoleEmailProvider,
) -> None:
    """The console provider accepts every message and reports success, so
    `email_sent` cannot be inferred from the send returning. In production it
    is asked before the send, or an admin would never think to pass the link
    on by hand."""
    settings = get_settings()
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "email_provider", "console")

    admin = await make_user(db_session, email="inviter3@example.com")
    workspace = await make_workspace(db_session, owner=admin)

    response = await client.post(
        f"/workspaces/{workspace.id}/invite",
        json={"email": "invitee3@example.com", "role": "member"},
        headers=auth_headers(admin),
    )

    assert response.status_code == 201
    assert response.json()["email_sent"] is False
    assert "EMAIL_PROVIDER" in response.json()["email_error"]
    assert response.json()["token"]
    assert outbox.outbox == []
