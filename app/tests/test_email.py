"""Step 7: sending mail, and the gate everything outbound goes through.

There is exactly one place in this application where a message leaves the
process, and these cases pin that down: the provider interface, the payload the
Resend implementation actually builds, and the manual-send endpoint, which
proposes rather than sends.

Nothing here touches the network. `ResendEmailProvider._payload` is tested
directly rather than through a mocked HTTP client, because the thing worth
asserting is the shape of the request, not that httpx can post.
"""

import base64
import uuid

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.constants import AuditAction, PendingActionOrigin, PendingActionStatus
from app.database.models.audit_log import AuditLogEntry
from app.database.models.pending_action import PendingAction
from app.exceptions import UpstreamFailure
from app.services import email_service
from app.services.email_service import (
    ConsoleEmailProvider,
    EmailAttachment,
    GmailEmailProvider,
    OutboundEmail,
    ResendEmailProvider,
    _refusal_reason,
)
from app.tests.factories import (
    auth_headers,
    make_member,
    make_user,
    make_workspace,
    random_email,
)

# --------------------------------------------------------------------------
# The providers.
# --------------------------------------------------------------------------


def test_the_resend_payload_carries_a_reply_to_and_a_base64_attachment() -> None:
    """`From:` is a no-reply sender the project controls, so `Reply-To:` is the
    only thing that gets a reply back to a real person (SPEC-v2 D16)."""
    provider = ResendEmailProvider(
        api_key="key", from_address="no-reply@example.com", from_name="Workspace"
    )

    payload = provider._payload(
        OutboundEmail(
            to=["bob@example.com"],
            subject="Invitation: Review",
            body="Body text.",
            reply_to="alice@example.com",
            attachments=[
                EmailAttachment(
                    filename="invitation.ics",
                    content=b"BEGIN:VCALENDAR\r\n",
                    content_type="text/calendar",
                )
            ],
        )
    )

    assert payload["from"] == "Workspace <no-reply@example.com>"
    assert payload["to"] == ["bob@example.com"]
    assert payload["reply_to"] == "alice@example.com"
    assert payload["text"] == "Body text."
    attachment = payload["attachments"][0]
    assert attachment["filename"] == "invitation.ics"
    assert base64.b64decode(attachment["content"]) == b"BEGIN:VCALENDAR\r\n"


async def test_resend_without_a_key_fails_before_it_tries_the_network() -> None:
    """The provider selects perfectly well without a key, which is why
    /email/status reports `configured` separately."""
    provider = ResendEmailProvider(api_key="", from_address="x@example.com", from_name="")

    with pytest.raises(UpstreamFailure):
        await provider.send(OutboundEmail(to=["bob@example.com"], subject="s", body="b"))


async def test_the_gmail_provider_documents_why_it_is_not_shipped() -> None:
    """SPEC-v2 D16. Kept as a class so the blocker lives in code rather than
    only in a document nobody opens."""
    with pytest.raises(UpstreamFailure) as caught:
        await GmailEmailProvider().send(
            OutboundEmail(to=["bob@example.com"], subject="s", body="b")
        )

    assert "restricted scope" in str(caught.value.detail)


async def test_the_console_provider_records_rather_than_sends() -> None:
    provider = ConsoleEmailProvider()

    sent = await provider.send(
        OutboundEmail(to=["bob@example.com"], subject="Hello", body="Body.")
    )

    assert sent.recipients == ["bob@example.com"]
    assert [message.subject for message in provider.outbox] == ["Hello"]


# --------------------------------------------------------------------------
# The routes.
# --------------------------------------------------------------------------


async def test_status_reports_the_provider_and_whether_it_can_deliver(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    admin = await make_user(db_session, email=random_email())
    workspace = await make_workspace(db_session, owner=admin)

    response = await client.get(
        f"/workspaces/{workspace.id}/email/status", headers=auth_headers(admin)
    )

    assert response.status_code == 200
    assert response.json() == {
        "provider": "console",
        "configured": True,
        "from_address": "onboarding@resend.dev",
    }


async def test_a_manual_send_proposes_rather_than_sends(
    db_session: AsyncSession, client: AsyncClient, outbox: ConsoleEmailProvider
) -> None:
    """202, not 201. What comes back is a pending action -- a person composing
    their own message still goes through the one gate, so there is exactly one
    place mail leaves this application and one audit trail."""
    admin = await make_user(db_session, email=random_email(), name="Alice Smith")
    colleague = await make_user(db_session, email=random_email(), name="Bob Jones")
    workspace = await make_workspace(db_session, owner=admin)
    await make_member(db_session, workspace=workspace, user=colleague)

    response = await client.post(
        f"/workspaces/{workspace.id}/email/send",
        json={"recipients": ["Bob Jones"], "subject": "Standup", "body": "10am."},
        headers=auth_headers(admin),
    )

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] == "pending"
    assert body["origin"] == "manual"
    # No conversation behind it, and so no paused graph run to resume.
    assert body["thread_id"] is None
    assert body["payload"]["recipients"] == [
        {"user_id": str(colleague.id), "name": "Bob Jones", "email": colleague.email}
    ]
    assert outbox.outbox == [], "nothing may be sent before somebody approves it"


async def test_a_manual_send_cannot_reach_outside_the_workspace(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    """This endpoint must not be a way round the resolver the agent is held to.

    If a person could name an arbitrary address here, D21 would only be
    stopping the model from doing directly what it could ask a user to do for
    it through the UI.
    """
    admin = await make_user(db_session, email=random_email(), name="Alice Smith")
    workspace = await make_workspace(db_session, owner=admin)

    response = await client.post(
        f"/workspaces/{workspace.id}/email/send",
        json={
            "recipients": ["attacker@evil.test"],
            "subject": "Quarterly figures",
            "body": "Attached.",
        },
        headers=auth_headers(admin),
    )

    assert response.status_code == 404
    proposed = await db_session.scalar(
        select(func.count())
        .select_from(PendingAction)
        .where(PendingAction.workspace_id == workspace.id)
    )
    assert proposed == 0, "a refused recipient must not leave a proposal behind"


async def test_approving_a_manual_send_actually_sends_it(
    db_session: AsyncSession, client: AsyncClient, outbox: ConsoleEmailProvider
) -> None:
    admin = await make_user(db_session, email=random_email(), name="Alice Smith")
    colleague = await make_user(db_session, email=random_email(), name="Bob Jones")
    workspace = await make_workspace(db_session, owner=admin)
    await make_member(db_session, workspace=workspace, user=colleague)

    proposed = await client.post(
        f"/workspaces/{workspace.id}/email/send",
        json={"recipients": ["Bob Jones"], "subject": "Standup", "body": "10am."},
        headers=auth_headers(admin),
    )
    action = proposed.json()

    decided = await client.post(
        f"/workspaces/{workspace.id}/pending-actions/{action['id']}/decide",
        json={"decision": "approve", "payload_hash": action["payload_hash"]},
        headers=auth_headers(admin),
    )

    assert decided.status_code == 200, decided.text
    assert decided.json()["status"] == "executed"

    assert len(outbox.outbox) == 1
    message = outbox.outbox[0]
    assert message.to == [colleague.email]
    assert message.subject == "Standup"
    # Reply-To is the person who asked for the action, not the approver and not
    # the no-reply sender.
    assert message.reply_to == admin.email

    logged = await db_session.scalars(
        select(AuditLogEntry.action).where(AuditLogEntry.workspace_id == workspace.id)
    )
    assert AuditAction.EMAIL_SENT.value in list(logged)


async def test_rejecting_a_manual_send_sends_nothing(
    db_session: AsyncSession, client: AsyncClient, outbox: ConsoleEmailProvider
) -> None:
    admin = await make_user(db_session, email=random_email(), name="Alice Smith")
    workspace = await make_workspace(db_session, owner=admin)

    proposed = await client.post(
        f"/workspaces/{workspace.id}/email/send",
        json={"recipients": ["Alice Smith"], "subject": "Standup", "body": "10am."},
        headers=auth_headers(admin),
    )
    action = proposed.json()

    decided = await client.post(
        f"/workspaces/{workspace.id}/pending-actions/{action['id']}/decide",
        json={"decision": "reject", "payload_hash": action["payload_hash"]},
        headers=auth_headers(admin),
    )

    assert decided.status_code == 200
    assert decided.json()["status"] == "rejected"
    assert outbox.outbox == []


async def test_a_member_may_propose_but_only_an_admin_may_send(
    db_session: AsyncSession, client: AsyncClient, outbox: ConsoleEmailProvider
) -> None:
    """The two-person property: whoever composes a message, somebody else has
    to be the one who lets it out."""
    admin = await make_user(db_session, email=random_email(), name="Alice Smith")
    member = await make_user(db_session, email=random_email(), name="Bob Jones")
    workspace = await make_workspace(db_session, owner=admin)
    await make_member(db_session, workspace=workspace, user=member)

    proposed = await client.post(
        f"/workspaces/{workspace.id}/email/send",
        json={"recipients": ["Alice Smith"], "subject": "Standup", "body": "10am."},
        headers=auth_headers(member),
    )
    assert proposed.status_code == 202
    action = proposed.json()

    refused = await client.post(
        f"/workspaces/{workspace.id}/pending-actions/{action['id']}/decide",
        json={"decision": "approve", "payload_hash": action["payload_hash"]},
        headers=auth_headers(member),
    )

    assert refused.status_code == 403
    assert outbox.outbox == []


async def test_a_manual_action_is_still_bound_to_its_payload_hash(
    db_session: AsyncSession, client: AsyncClient, outbox: ConsoleEmailProvider
) -> None:
    """SPEC-v2 D20 applies to this route too -- the gate is one gate."""
    admin = await make_user(db_session, email=random_email(), name="Alice Smith")
    workspace = await make_workspace(db_session, owner=admin)

    proposed = await client.post(
        f"/workspaces/{workspace.id}/email/send",
        json={"recipients": ["Alice Smith"], "subject": "Standup", "body": "10am."},
        headers=auth_headers(admin),
    )
    action = proposed.json()

    response = await client.post(
        f"/workspaces/{workspace.id}/pending-actions/{action['id']}/decide",
        json={"decision": "approve", "payload_hash": "0" * 64},
        headers=auth_headers(admin),
    )

    assert response.status_code == 409
    assert outbox.outbox == []

    stored = await db_session.get(PendingAction, uuid.UUID(action["id"]))
    assert stored is not None
    await db_session.refresh(stored)
    assert stored.status is PendingActionStatus.PENDING
    assert stored.origin is PendingActionOrigin.MANUAL


# --------------------------------------------------------------------------
# Undeliverable deployments.
#
# The failure these cover is the quietest one this application can have: with
# EMAIL_PROVIDER unset, a production deployment falls back to the console
# provider, every approved email is recorded as `executed`, the agent reports
# it as sent, and nothing is ever sent.
# --------------------------------------------------------------------------


def test_a_refused_message_carries_the_providers_own_explanation() -> None:
    """Resend's 403 says *why* -- an unverified domain, or a testing sender
    that may only mail its own account holder. A bare status code turns a
    dashboard fix into a hunt, so the message is passed through."""
    response = httpx.Response(
        403,
        json={
            "statusCode": 403,
            "message": "You can only send testing emails to your own email address",
            "name": "validation_error",
        },
    )

    assert (
        _refusal_reason(response)
        == "You can only send testing emails to your own email address"
    )


@pytest.mark.parametrize("body", [b"<html>502 Bad Gateway</html>", b"[]", b""])
def test_an_unparseable_refusal_does_not_raise(body: bytes) -> None:
    """A refusal is exactly when a provider is least likely to return the JSON
    its documentation promises, and a diagnostic that raises while explaining a
    failure is worse than no diagnostic."""
    assert _refusal_reason(httpx.Response(502, content=body)) == ""


def test_console_is_configured_but_not_deliverable(monkeypatch: pytest.MonkeyPatch) -> None:
    """The two questions are different and the distinction is the whole point:
    console is a perfectly configured provider that delivers to a list in
    memory."""
    settings = get_settings()
    monkeypatch.setattr(settings, "email_provider", "console")

    assert email_service.provider_is_configured() is True
    reason = email_service.undeliverable_reason()
    assert reason is not None and "EMAIL_PROVIDER" in reason


def test_resend_without_a_key_is_undeliverable(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "email_provider", "resend")
    monkeypatch.setattr(settings, "resend_api_key", "")

    reason = email_service.undeliverable_reason()
    assert reason is not None and "RESEND_API_KEY" in reason

    monkeypatch.setattr(settings, "resend_api_key", "re_key")
    assert email_service.undeliverable_reason() is None


async def test_approving_a_send_in_an_undeliverable_production_deployment_fails(
    db_session: AsyncSession,
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    outbox: ConsoleEmailProvider,
) -> None:
    """The regression this file exists for. An approval that cannot possibly
    deliver must land on `failed`, not `executed` -- otherwise the status
    column means "somebody clicked approve" rather than "this happened"."""
    admin = await make_user(db_session, email=random_email(), name="Alice Smith")
    colleague = await make_user(db_session, email=random_email(), name="Bob Jones")
    workspace = await make_workspace(db_session, owner=admin)
    await make_member(db_session, workspace=workspace, user=colleague)

    proposed = await client.post(
        f"/workspaces/{workspace.id}/email/send",
        json={"recipients": ["Bob Jones"], "subject": "Hello", "body": "Body."},
        headers=auth_headers(admin),
    )
    assert proposed.status_code == 202
    action = proposed.json()

    # Console provider, production environment: the misconfiguration itself.
    monkeypatch.setattr(get_settings(), "environment", "production")

    decided = await client.post(
        f"/workspaces/{workspace.id}/pending-actions/{action['id']}/decide",
        json={"decision": "approve", "payload_hash": action["payload_hash"]},
        headers=auth_headers(admin),
    )

    assert decided.status_code == 502
    assert "cannot send email" in decided.json()["detail"]

    # Nothing was handed to the provider, and the row says so.
    assert outbox.outbox == []
    row = await db_session.get(PendingAction, uuid.UUID(action["id"]))
    assert row is not None
    await db_session.refresh(row)
    assert row.status is PendingActionStatus.FAILED
