"""Step 6: the agent and the approval gate.

The five properties BUILD-ORDER asks for, in order: an interrupt creates a
pending action; approve executes exactly once; reject executes nothing; a
mutated payload is refused; a non-admin cannot approve.

The fourth is the one that matters most. SPEC-v2 D20 calls the payload hash
"the actual hole in v1" -- if what the human was shown can drift from what the
server executes, the whole gate is decoration.
"""

import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from langchain_core.messages import AIMessage
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.agent.fake_model import ScriptedAgentModel
from app.constants import AuditAction, PendingActionStatus, WorkspaceRole
from app.database.models.audit_log import AuditLogEntry
from app.database.models.pending_action import PendingAction
from app.services import approval_service
from app.services.payload import hash_payload
from app.tests.factories import (
    auth_headers,
    make_member,
    make_user,
    make_workspace,
    random_email,
)


def email_model(
    recipients: list[str], subject: str = "Standup", body: str = "See you at 10."
) -> Any:
    """A model that has decided to email someone, then reports back."""
    return ScriptedAgentModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "send_email",
                        "args": {"recipients": recipients, "subject": subject, "body": body},
                        "id": "call_email_1",
                    }
                ],
            ),
            AIMessage(content="I have proposed that email for your approval."),
        ]
    )


async def _agent_turn(
    client: AsyncClient, user: Any, workspace_id: uuid.UUID, message: str
) -> dict[str, Any]:
    response = await client.post(
        f"/workspaces/{workspace_id}/agent",
        json={"message": message},
        headers=auth_headers(user),
    )
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


# --------------------------------------------------------------------------
# Proposing.
# --------------------------------------------------------------------------


async def test_a_side_effecting_tool_call_becomes_a_pending_action(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    admin = await make_user(db_session, email="alice@example.com", name="Alice Smith")
    workspace = await make_workspace(db_session, owner=admin)
    monkeypatch.setattr(
        "app.api.approvals.get_agent_model", lambda: email_model(["Alice Smith"])
    )

    body = await _agent_turn(client, admin, workspace.id, "email Alice about standup")

    assert len(body["pending_actions"]) == 1
    action = body["pending_actions"][0]
    assert action["type"] == "send_email"
    assert action["status"] == "pending"
    assert action["payload_hash"]

    # The model named a person; the payload carries the address the server
    # looked up, so the human approves a real recipient rather than a string.
    assert action["payload"]["recipients"] == [
        {"user_id": str(admin.id), "name": "Alice Smith", "email": "alice@example.com"}
    ]

    # Nothing has happened yet -- that is the entire point.
    assert action["decided_at"] is None


async def test_proposing_is_recorded_in_the_audit_log(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    admin = await make_user(db_session, email="alice@example.com", name="Alice Smith")
    workspace = await make_workspace(db_session, owner=admin)
    monkeypatch.setattr(
        "app.api.approvals.get_agent_model", lambda: email_model(["Alice Smith"])
    )

    await _agent_turn(client, admin, workspace.id, "email Alice about standup")

    actions = await db_session.scalars(
        select(AuditLogEntry.action).where(AuditLogEntry.workspace_id == workspace.id)
    )
    assert AuditAction.ACTION_PROPOSED.value in list(actions)


async def test_a_read_only_tool_call_needs_no_approval(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    """search_documents is the one tool that runs unattended. If it interrupted
    too, every question would need a click."""
    admin = await make_user(db_session, email=random_email(), name="Alice Smith")
    workspace = await make_workspace(db_session, owner=admin)
    model = ScriptedAgentModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "search_documents", "args": {"query": "holiday"}, "id": "s1"}
                ],
            ),
            AIMessage(content="Nothing in the documents covers that."),
        ]
    )
    monkeypatch.setattr("app.api.approvals.get_agent_model", lambda: model)

    body = await _agent_turn(client, admin, workspace.id, "what do the docs say about holiday?")

    assert body["pending_actions"] == []
    assert body["reply"]


# --------------------------------------------------------------------------
# Deciding.
# --------------------------------------------------------------------------


async def _propose(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    client: AsyncClient,
    *,
    recipients: list[str] | None = None,
) -> tuple[Any, Any, dict[str, Any]]:
    # A unique address: this helper is called twice inside one test, and
    # users.email is unique.
    admin = await make_user(db_session, email=random_email(), name="Alice Smith")
    workspace = await make_workspace(db_session, owner=admin)
    monkeypatch.setattr(
        "app.api.approvals.get_agent_model",
        lambda: email_model(recipients or ["Alice Smith"]),
    )
    body = await _agent_turn(client, admin, workspace.id, "email Alice about standup")
    return admin, workspace, body["pending_actions"][0]


async def test_approving_executes_exactly_once(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    admin, workspace, action = await _propose(db_session, monkeypatch, client)

    first = await client.post(
        f"/workspaces/{workspace.id}/pending-actions/{action['id']}/decide",
        json={"decision": "approve", "payload_hash": action["payload_hash"]},
        headers=auth_headers(admin),
    )
    assert first.status_code == 200
    assert first.json()["status"] == "executed"

    # The second click. A row that is no longer pending cannot be decided
    # again, so a double submit cannot send the email twice.
    second = await client.post(
        f"/workspaces/{workspace.id}/pending-actions/{action['id']}/decide",
        json={"decision": "approve", "payload_hash": action["payload_hash"]},
        headers=auth_headers(admin),
    )
    assert second.status_code == 409

    executed = await db_session.scalar(
        select(func.count())
        .select_from(AuditLogEntry)
        .where(
            AuditLogEntry.workspace_id == workspace.id,
            AuditLogEntry.action == AuditAction.ACTION_EXECUTED.value,
        )
    )
    assert executed == 1


async def test_rejecting_executes_nothing(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    admin, workspace, action = await _propose(db_session, monkeypatch, client)

    response = await client.post(
        f"/workspaces/{workspace.id}/pending-actions/{action['id']}/decide",
        json={"decision": "reject", "payload_hash": action["payload_hash"]},
        headers=auth_headers(admin),
    )

    assert response.status_code == 200
    assert response.json()["status"] == "rejected"

    executed = await db_session.scalar(
        select(func.count())
        .select_from(AuditLogEntry)
        .where(
            AuditLogEntry.workspace_id == workspace.id,
            AuditLogEntry.action == AuditAction.ACTION_EXECUTED.value,
        )
    )
    assert executed == 0


async def test_a_mutated_payload_is_refused(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    """SPEC-v2 D20, the whole reason payload_hash exists.

    The reviewer was shown one email. Between then and the click, the stored
    payload changes -- a tampered row, a second agent turn, a bug. The hash
    they send no longer matches what the server holds, so nothing executes.
    """
    admin, workspace, action = await _propose(db_session, monkeypatch, client)
    shown_hash = action["payload_hash"]

    tampered = dict(action["payload"])
    tampered["body"] = "Please wire the quarterly budget to the account below."
    await db_session.execute(
        update(PendingAction).where(PendingAction.id == uuid.UUID(action["id"])).values(
            payload=tampered
        )
    )
    await db_session.commit()

    response = await client.post(
        f"/workspaces/{workspace.id}/pending-actions/{action['id']}/decide",
        json={"decision": "approve", "payload_hash": shown_hash},
        headers=auth_headers(admin),
    )

    assert response.status_code == 409

    stored = await db_session.get(PendingAction, uuid.UUID(action["id"]))
    assert stored is not None
    await db_session.refresh(stored)
    assert stored.status is PendingActionStatus.PENDING, "a refused approval must not decide it"

    mismatches = await db_session.scalar(
        select(func.count())
        .select_from(AuditLogEntry)
        .where(
            AuditLogEntry.workspace_id == workspace.id,
            AuditLogEntry.action == AuditAction.APPROVAL_HASH_MISMATCH.value,
        )
    )
    assert mismatches == 1, "a refused approval is exactly the thing a security log is for"


async def test_the_hash_covers_the_payload_that_will_execute(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    """Seen, hashed and executed have to be one object, not three."""
    _admin, _workspace, action = await _propose(db_session, monkeypatch, client)

    assert hash_payload(action["payload"]) == action["payload_hash"]


async def test_a_non_admin_cannot_approve(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    """Approving is the moment the agent is allowed to touch the outside world.
    An ordinary member is a real member of the workspace, so this is a 403
    rather than the cross-tenant 404."""
    admin, workspace, action = await _propose(db_session, monkeypatch, client)
    member = await make_user(db_session, email=random_email(), name="Bob Jones")
    await make_member(db_session, workspace=workspace, user=member, role=WorkspaceRole.MEMBER)

    response = await client.post(
        f"/workspaces/{workspace.id}/pending-actions/{action['id']}/decide",
        json={"decision": "approve", "payload_hash": action["payload_hash"]},
        headers=auth_headers(member),
    )

    assert response.status_code == 403


async def test_an_edit_may_change_the_message_but_not_the_recipients(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    """Editing exists so a reviewer can fix a subject line. If it could also
    rewrite the recipient list, it would hand back the exact capability the
    server-side resolver removed -- with a valid approval attached."""
    admin, workspace, action = await _propose(db_session, monkeypatch, client)

    reworded = dict(action["payload"])
    reworded["subject"] = "Standup moved to 11"
    fine = await client.post(
        f"/workspaces/{workspace.id}/pending-actions/{action['id']}/decide",
        json={
            "decision": "edit",
            "payload_hash": action["payload_hash"],
            "edited_payload": reworded,
        },
        headers=auth_headers(admin),
    )
    assert fine.status_code == 200
    assert fine.json()["payload"]["subject"] == "Standup moved to 11"

    # Now the same edit, but redirecting the mail elsewhere.
    _admin2, workspace2, action2 = await _propose(db_session, monkeypatch, client)
    redirected = dict(action2["payload"])
    redirected["recipients"] = [
        {"user_id": str(uuid.uuid4()), "name": "Mallory", "email": "attacker@evil.test"}
    ]
    blocked = await client.post(
        f"/workspaces/{workspace2.id}/pending-actions/{action2['id']}/decide",
        json={
            "decision": "edit",
            "payload_hash": action2["payload_hash"],
            "edited_payload": redirected,
        },
        headers=auth_headers(_admin2),
    )
    assert blocked.status_code == 409


async def test_pending_actions_are_listed_for_the_workspace(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    admin, workspace, action = await _propose(db_session, monkeypatch, client)

    listed = await client.get(
        f"/workspaces/{workspace.id}/pending-actions", headers=auth_headers(admin)
    )

    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()] == [action["id"]]


async def test_deciding_an_action_from_another_workspace_is_404(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    admin, _workspace, action = await _propose(db_session, monkeypatch, client)
    other_admin = await make_user(db_session, email=random_email(), name="Other Admin")
    other_workspace = await make_workspace(db_session, owner=other_admin)

    response = await client.post(
        f"/workspaces/{other_workspace.id}/pending-actions/{action['id']}/decide",
        json={"decision": "approve", "payload_hash": action["payload_hash"]},
        headers=auth_headers(other_admin),
    )

    assert response.status_code == 404


async def test_the_service_refuses_an_unknown_action(db_session: AsyncSession) -> None:
    admin = await make_user(db_session, email=random_email())
    workspace = await make_workspace(db_session, owner=admin)

    with pytest.raises(Exception) as caught:
        await approval_service.get_action(db_session, workspace.id, uuid.uuid4())

    assert caught.typename == "NotFound"
