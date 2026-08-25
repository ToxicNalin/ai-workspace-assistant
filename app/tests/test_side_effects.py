"""Step 7: the approval gate stops being a rehearsal.

Step 6 proved that nothing happens without a human decision. These cases prove
the other half -- that once the decision is made, the right thing happens, to
the right people, exactly once, and that a failure is recorded as a failure
rather than reported as a success.

Two of them are about a property rather than a feature. **No side-effecting
tool body is ever executed in this application**: an approved action is carried
out by app/services/action_executor.py from the payload the hash covers, and
the graph is then handed the result. `test_a_side_effecting_tool_body_refuses_
to_run` and `test_an_agent_refuses_to_be_built_with_an_unpoliced_tool` are what
keep that true as the tool list grows.
"""

import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from langchain_core.messages import AIMessage
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.agent.fake_model import KeywordAgentModel, ScriptedAgentModel
from app.ai.agent.graph import INTERRUPT_POLICY, build_agent
from app.ai.tools.base import ApprovalGateBypassed, assert_every_tool_has_a_policy
from app.ai.tools.email import build_email_tool
from app.constants import AuditAction, PendingActionStatus
from app.database.models.audit_log import AuditLogEntry
from app.database.models.calendar_event import CalendarEvent
from app.database.models.pending_action import PendingAction
from app.database.models.task import Task
from app.services.email_service import ConsoleEmailProvider, OutboundEmail, SentEmail
from app.tests.factories import (
    auth_headers,
    make_member,
    make_user,
    make_workspace,
    random_email,
)

START = "2026-09-01T10:00:00+00:00"
END = "2026-09-01T11:00:00+00:00"


def _scripted(name: str, args: dict[str, Any]) -> ScriptedAgentModel:
    """A model that has decided to do one thing, then reports back."""
    return ScriptedAgentModel(
        responses=[
            AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": "call_1"}]),
            AIMessage(content="I have proposed that for your approval."),
        ]
    )


async def _propose(
    client: AsyncClient, user: Any, workspace_id: uuid.UUID, message: str
) -> dict[str, Any]:
    response = await client.post(
        f"/workspaces/{workspace_id}/agent",
        json={"message": message},
        headers=auth_headers(user),
    )
    assert response.status_code == 200, response.text
    return dict(response.json())


async def _approve(
    client: AsyncClient, user: Any, workspace_id: uuid.UUID, action: dict[str, Any]
) -> Any:
    return await client.post(
        f"/workspaces/{workspace_id}/pending-actions/{action['id']}/decide",
        json={"decision": "approve", "payload_hash": action["payload_hash"]},
        headers=auth_headers(user),
    )


# --------------------------------------------------------------------------
# The side effects themselves.
# --------------------------------------------------------------------------


async def test_an_approved_email_is_actually_sent(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    client: AsyncClient,
    outbox: ConsoleEmailProvider,
) -> None:
    admin = await make_user(db_session, email=random_email(), name="Alice Smith")
    colleague = await make_user(db_session, email=random_email(), name="Bob Jones")
    workspace = await make_workspace(db_session, owner=admin)
    await make_member(db_session, workspace=workspace, user=colleague)
    monkeypatch.setattr(
        "app.api.approvals.get_agent_model",
        lambda: _scripted(
            "send_email",
            {"recipients": ["Bob Jones"], "subject": "Standup", "body": "10am."},
        ),
    )

    turn = await _propose(client, admin, workspace.id, "email Bob about standup")
    assert outbox.outbox == [], "proposing must not send"

    decided = await _approve(client, admin, workspace.id, turn["pending_actions"][0])

    assert decided.status_code == 200, decided.text
    assert decided.json()["status"] == "executed"
    assert len(outbox.outbox) == 1
    assert outbox.outbox[0].to == [colleague.email]
    assert outbox.outbox[0].subject == "Standup"
    # The requester, not the approver and not the no-reply sender (D16).
    assert outbox.outbox[0].reply_to == admin.email


async def test_approving_twice_sends_once(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    client: AsyncClient,
    outbox: ConsoleEmailProvider,
) -> None:
    """Step 6 asserted this against the audit log. Now there is a real side
    effect to count, which is the version that would actually embarrass you."""
    admin = await make_user(db_session, email=random_email(), name="Alice Smith")
    workspace = await make_workspace(db_session, owner=admin)
    monkeypatch.setattr(
        "app.api.approvals.get_agent_model",
        lambda: _scripted(
            "send_email",
            {"recipients": ["Alice Smith"], "subject": "Standup", "body": "10am."},
        ),
    )

    turn = await _propose(client, admin, workspace.id, "email Alice about standup")
    action = turn["pending_actions"][0]

    first = await _approve(client, admin, workspace.id, action)
    second = await _approve(client, admin, workspace.id, action)

    assert first.status_code == 200
    assert second.status_code == 409
    assert len(outbox.outbox) == 1


async def test_approved_tasks_are_created_and_assigned(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    admin = await make_user(db_session, email=random_email(), name="Alice Smith")
    colleague = await make_user(db_session, email=random_email(), name="Bob Jones")
    workspace = await make_workspace(db_session, owner=admin)
    await make_member(db_session, workspace=workspace, user=colleague)
    monkeypatch.setattr(
        "app.api.approvals.get_agent_model",
        lambda: _scripted(
            "create_tasks",
            {
                "tasks": [
                    {"title": "Draft the report", "assignee": "Bob Jones"},
                    {"title": "Book the room"},
                ]
            },
        ),
    )

    turn = await _propose(client, admin, workspace.id, "create tasks for the review")
    decided = await _approve(client, admin, workspace.id, turn["pending_actions"][0])

    assert decided.status_code == 200, decided.text
    assert decided.json()["status"] == "executed"

    tasks = (
        await db_session.scalars(
            select(Task).where(Task.workspace_id == workspace.id).order_by(Task.title)
        )
    ).all()
    assert [task.title for task in tasks] == ["Book the room", "Draft the report"]
    # The model named a person; the assignee is the id the server resolved.
    assert tasks[1].assigned_to == colleague.id
    assert tasks[0].assigned_to is None


async def test_an_approved_event_is_created_and_the_invitation_emailed(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    client: AsyncClient,
    outbox: ConsoleEmailProvider,
) -> None:
    """BUILD-ORDER's 'done when' for this step, on the agent path: an approved
    event downloads as a valid `.ics` -- and here, arrives as one."""
    admin = await make_user(db_session, email=random_email(), name="Alice Smith")
    colleague = await make_user(db_session, email=random_email(), name="Bob Jones")
    workspace = await make_workspace(db_session, owner=admin)
    await make_member(db_session, workspace=workspace, user=colleague)
    monkeypatch.setattr(
        "app.api.approvals.get_agent_model",
        lambda: _scripted(
            "create_event",
            {
                "title": "Quarterly review",
                "start_time": START,
                "end_time": END,
                "guests": ["Bob Jones"],
            },
        ),
    )

    turn = await _propose(client, admin, workspace.id, "set up the quarterly review")
    decided = await _approve(client, admin, workspace.id, turn["pending_actions"][0])
    assert decided.status_code == 200, decided.text

    event = await db_session.scalar(
        select(CalendarEvent).where(CalendarEvent.workspace_id == workspace.id)
    )
    assert event is not None
    assert event.title == "Quarterly review"
    assert [guest["email"] for guest in event.guests] == [colleague.email]

    assert len(outbox.outbox) == 1
    message = outbox.outbox[0]
    assert message.to == [colleague.email]
    invitation = message.attachments[0]
    assert invitation.filename == "invitation.ics"
    assert invitation.content_type == "text/calendar"

    body = invitation.content.decode("utf-8")
    assert body.startswith("BEGIN:VCALENDAR")
    assert f"UID:{event.ics_uid}" in body
    assert "DTSTART:20260901T100000Z" in body
    assert f"mailto:{colleague.email}" in body.replace("\r\n ", "")

    # The same invitation is downloadable afterwards, from the stored snapshot.
    downloaded = await client.get(
        f"/workspaces/{workspace.id}/events/{event.id}/ics", headers=auth_headers(admin)
    )
    assert downloaded.status_code == 200
    assert f"UID:{event.ics_uid}" in downloaded.text


async def test_the_agent_is_told_what_actually_happened(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    """The graph is resumed with the executor's outcome, not with the tool's
    canned string -- so the assistant reports the send that happened rather
    than the one it hoped for.

    KeywordAgentModel echoes the last tool result, which makes the channel
    visible: whatever reaches the model as the tool's answer is what comes back.
    """
    admin = await make_user(db_session, email=random_email(), name="Alice Smith")
    workspace = await make_workspace(db_session, owner=admin)
    monkeypatch.setattr("app.api.approvals.get_agent_model", KeywordAgentModel)

    turn = await _propose(client, admin, workspace.id, "email Alice Smith about standup")
    decided = await _approve(client, admin, workspace.id, turn["pending_actions"][0])
    assert decided.status_code == 200, decided.text

    history = await client.get(
        f"/workspaces/{workspace.id}/chat/threads/{turn['thread_id']}/history",
        headers=auth_headers(admin),
    )
    replies = [item["content"] for item in history.json() if item["role"] == "assistant"]

    assert any("was sent to Alice Smith" in reply for reply in replies), replies


# --------------------------------------------------------------------------
# When it goes wrong.
# --------------------------------------------------------------------------


class _BrokenMailer:
    """A provider that is reachable and refuses -- the realistic outage."""

    name = "broken"

    async def send(self, message: OutboundEmail) -> SentEmail:
        from app.exceptions import UpstreamFailure

        raise UpstreamFailure("The email provider refused the message (503)")


async def test_a_failed_send_is_recorded_as_failed_and_leaves_nothing_behind(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    """The reason `executed` and `failed` are different statuses.

    An event action inserts its row and *then* emails the invitation. If the
    provider fails at that point, half the side effect has happened -- so the
    executor's work is rolled back, the action is moved to `failed`, and the
    audit log says so. An approval that silently reported success would be the
    worst of the available outcomes.
    """
    admin = await make_user(db_session, email=random_email(), name="Alice Smith")
    colleague = await make_user(db_session, email=random_email(), name="Bob Jones")
    workspace = await make_workspace(db_session, owner=admin)
    await make_member(db_session, workspace=workspace, user=colleague)
    monkeypatch.setattr(
        "app.api.approvals.get_agent_model",
        lambda: _scripted(
            "create_event",
            {
                "title": "Quarterly review",
                "start_time": START,
                "end_time": END,
                "guests": ["Bob Jones"],
            },
        ),
    )
    monkeypatch.setattr("app.api.approvals.get_email_provider", _BrokenMailer)

    # Read the ids out before the request that fails. The failure path calls
    # rollback() on the session, which expires every object attached to it --
    # and this test shares one session with the app, so a later `workspace.id`
    # would be a lazy load in a sync attribute read. In the running app each
    # request has its own session and the question does not arise.
    workspace_id = workspace.id
    turn = await _propose(client, admin, workspace_id, "set up the quarterly review")
    action = turn["pending_actions"][0]
    decided = await _approve(client, admin, workspace_id, action)

    assert decided.status_code == 502, decided.text

    stored = await db_session.get(PendingAction, uuid.UUID(action["id"]))
    assert stored is not None
    await db_session.refresh(stored)
    assert stored.status is PendingActionStatus.FAILED
    assert stored.refusal_reason

    events = await db_session.scalar(
        select(func.count())
        .select_from(CalendarEvent)
        .where(CalendarEvent.workspace_id == workspace_id)
    )
    assert events == 0, "a half-created event is worse than no event"

    logged = list(
        await db_session.scalars(
            select(AuditLogEntry.action).where(AuditLogEntry.workspace_id == workspace_id)
        )
    )
    assert AuditAction.ACTION_FAILED.value in logged
    assert AuditAction.ACTION_EXECUTED.value not in logged


async def test_an_unusable_timestamp_is_refused_before_a_human_sees_it(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    """Validated at proposal time, not at execution time.

    Discovering it after approval would mean somebody had already authorised
    something that was never going to work -- and the refusal would surface as
    a failure rather than as the agent being told to try again.
    """
    admin = await make_user(db_session, email=random_email(), name="Alice Smith")
    workspace = await make_workspace(db_session, owner=admin)
    monkeypatch.setattr(
        "app.api.approvals.get_agent_model",
        lambda: _scripted(
            "create_event",
            {
                "title": "Whenever",
                "start_time": "next Tuesday-ish",
                "end_time": END,
                "guests": ["Alice Smith"],
            },
        ),
    )

    turn = await _propose(client, admin, workspace.id, "set up a meeting")

    assert turn["pending_actions"] == []
    assert len(turn["refused_actions"]) == 1
    assert turn["refused_actions"][0]["status"] == "refused"
    assert "ISO 8601" in turn["refused_actions"][0]["refusal_reason"]


async def test_an_event_that_ends_before_it_starts_is_refused(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    admin = await make_user(db_session, email=random_email(), name="Alice Smith")
    workspace = await make_workspace(db_session, owner=admin)
    monkeypatch.setattr(
        "app.api.approvals.get_agent_model",
        lambda: _scripted(
            "create_event",
            {
                "title": "Backwards",
                "start_time": END,
                "end_time": START,
                "guests": ["Alice Smith"],
            },
        ),
    )

    turn = await _propose(client, admin, workspace.id, "set up a meeting")

    assert turn["pending_actions"] == []
    assert len(turn["refused_actions"]) == 1


# --------------------------------------------------------------------------
# The invariant: no side-effecting tool body ever runs.
# --------------------------------------------------------------------------


async def test_a_side_effecting_tool_body_refuses_to_run() -> None:
    """Reaching a tool body means the gate was bypassed, so it fails loudly.

    Every side-effecting tool interrupts, and an approved action is carried out
    from the stored payload rather than by letting the tool run -- so this line
    is unreachable. An assertion is worth more than a comment saying so.
    """
    tool = build_email_tool()

    with pytest.raises(ApprovalGateBypassed):
        await tool.ainvoke(
            {"recipients": ["alice@example.com"], "subject": "s", "body": "b"}
        )


def test_an_agent_refuses_to_be_built_with_an_unpoliced_tool() -> None:
    """HumanInTheLoopMiddleware does not fail safe.

    A tool absent from `interrupt_on` is not interrupted -- it simply runs, with
    no approval and no audit trail. So omission has to be a startup failure
    rather than a silent default, which is what this guard supplies.
    """
    rogue = build_email_tool()
    rogue.name = "exfiltrate_everything"

    with pytest.raises(ApprovalGateBypassed) as caught:
        assert_every_tool_has_a_policy([rogue], INTERRUPT_POLICY)

    assert "exfiltrate_everything" in str(caught.value)


def test_every_shipped_tool_is_accounted_for() -> None:
    """The positive case: the agent this application actually builds passes."""
    assert build_agent is not None
    assert set(INTERRUPT_POLICY) == {
        "send_email",
        "create_event",
        "create_tasks",
        "search_documents",
    }
