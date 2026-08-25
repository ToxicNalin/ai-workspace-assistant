"""Carrying out an approved action.

This is where Step 6's approval gate finally does something, and *where* it
happens is the whole design.

The obvious implementation is to resume the paused graph and let the tool body
run. This does not do that. The side effect is performed here, from
`action.payload` -- the exact object the human was shown and the object
`payload_hash` covers (SPEC-v2 D20) -- and the graph is afterwards resumed with
the real outcome as the tool's result. Three things follow, all of them worth
more than the indirection costs:

* Nothing the model, the tool layer or the graph does between approval and
  execution can change what executes. There is no second copy of the arguments
  to drift from the first.
* Success and failure are known *here*, so `executed` can mean executed and a
  provider outage is recorded as a failure rather than reported to the user as
  a sent email.
* No side-effecting tool body is ever executed in this application at all --
  see app/ai/tools/base.py, which turns that from a claim into an assertion.

Nothing in this module commits. app/services/approval_service.py owns the
transaction, so the tasks, the event and the audit entry that authorised them
are one atomic fact.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.tools.resolve import ResolvedMember
from app.constants import AuditAction, PendingActionType
from app.database.models.pending_action import PendingAction
from app.database.models.user import User
from app.exceptions import AppError, UpstreamFailure
from app.services import audit_service, calendar_service, task_service
from app.services.email_service import (
    EmailAttachment,
    EmailProvider,
    OutboundEmail,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ExecutionOutcome:
    """What happened, in a sentence the model can report back verbatim."""

    summary: str
    details: dict[str, Any]


def _people(payload: dict[str, Any], field: str) -> list[ResolvedMember]:
    """Read a resolved-member list back out of the approved payload.

    These entries were written by app/services/agent_service.py from
    `workspace_members` and have been covered by the payload hash ever since.
    They are not re-resolved and not re-derived from anything the model said.
    """
    people: list[ResolvedMember] = []
    for entry in payload.get(field) or []:
        if not isinstance(entry, dict):
            continue
        try:
            user_id = uuid.UUID(str(entry.get("user_id")))
        except (TypeError, ValueError):
            continue
        people.append(
            ResolvedMember(
                user_id=user_id,
                name=str(entry.get("name", "")),
                email=str(entry.get("email", "")),
            )
        )
    return people


def _requester_reply_to(requester: User | None) -> str | None:
    """SPEC-v2 D16: `From:` is a no-reply sender the project controls, so a
    reply has to be routed deliberately. It goes to the person who asked for
    the action -- not to the person who approved it, and not into a void."""
    return requester.email if requester is not None else None


async def _execute_send_email(
    db: AsyncSession,
    mailer: EmailProvider,
    *,
    workspace_id: uuid.UUID,
    action: PendingAction,
    requester: User | None,
) -> ExecutionOutcome:
    recipients = _people(action.payload, "recipients")
    if not recipients:
        raise UpstreamFailure("This action has no recipients left to send to")

    subject = str(action.payload.get("subject", ""))
    sent = await mailer.send(
        OutboundEmail(
            to=[person.email for person in recipients],
            subject=subject,
            body=str(action.payload.get("body", "")),
            reply_to=_requester_reply_to(requester),
        )
    )

    audit_service.record(
        db,
        workspace_id=workspace_id,
        action=AuditAction.EMAIL_SENT,
        user_id=action.decided_by,
        details={
            "action_id": str(action.id),
            "provider": sent.provider,
            "message_id": sent.message_id,
            "recipients": sent.recipients,
            "subject": subject,
        },
    )

    names = ", ".join(person.name or person.email for person in recipients)
    return ExecutionOutcome(
        summary=f"The email '{subject}' was sent to {names}.",
        details={"message_id": sent.message_id, "recipients": sent.recipients},
    )


def _parse_moment(value: Any, field: str) -> datetime:
    """Times were already validated at proposal time; this is the second pass.

    A payload can still be edited between proposal and approval, so parsing
    here is not redundant -- it is the check that covers the edited copy.
    """
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise UpstreamFailure(f"This action's {field} is not a valid timestamp") from exc


async def _execute_create_event(
    db: AsyncSession,
    mailer: EmailProvider,
    *,
    workspace_id: uuid.UUID,
    action: PendingAction,
    requester: User | None,
) -> ExecutionOutcome:
    guests = _people(action.payload, "guests")
    title = str(action.payload.get("title", ""))

    event = await calendar_service.create_event(
        db,
        workspace_id=workspace_id,
        created_by=action.initiated_by,
        title=title,
        description=str(action.payload.get("description", "")),
        start_time=_parse_moment(action.payload.get("start_time"), "start time"),
        end_time=_parse_moment(action.payload.get("end_time"), "end time"),
        guests=guests,
    )

    provider = calendar_service.get_calendar_provider()
    external_id = await provider.publish(db, event, organiser_user_id=action.initiated_by)
    if external_id is not None:
        event.external_event_id = external_id

    organiser = (
        ResolvedMember(user_id=requester.id, name=requester.name, email=requester.email)
        if requester is not None
        else None
    )
    invitation = calendar_service.ics_for_event(event, organiser=organiser)

    # The invitation is only useful if it reaches somebody. An event with no
    # guests is a private note, so there is nothing to send.
    if guests:
        await mailer.send(
            OutboundEmail(
                to=[guest.email for guest in guests],
                subject=f"Invitation: {title}",
                body=(
                    f"You have been invited to '{title}'.\n\n"
                    f"Starts: {event.start_time.isoformat()}\n"
                    f"Ends:   {event.end_time.isoformat()}\n\n"
                    f"The attached invitation can be opened in any calendar."
                ),
                reply_to=_requester_reply_to(requester),
                attachments=[
                    EmailAttachment(
                        filename="invitation.ics",
                        content=invitation.encode("utf-8"),
                        content_type="text/calendar",
                    )
                ],
            )
        )

    audit_service.record(
        db,
        workspace_id=workspace_id,
        action=AuditAction.EVENT_CREATED,
        user_id=action.decided_by,
        details={
            "action_id": str(action.id),
            "event_id": str(event.id),
            "ics_uid": event.ics_uid,
            "guests": [guest.email for guest in guests],
        },
    )

    invited = f" and invited {len(guests)} guest(s)" if guests else ""
    return ExecutionOutcome(
        summary=f"Created the event '{title}'{invited}.",
        details={"event_id": str(event.id), "ics_uid": event.ics_uid},
    )


async def _execute_create_tasks(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    action: PendingAction,
) -> ExecutionOutcome:
    specs: list[dict[str, object]] = []
    for raw in action.payload.get("tasks") or []:
        if not isinstance(raw, dict):
            continue
        assignee = raw.get("assignee")
        assigned_to: uuid.UUID | None = None
        if isinstance(assignee, dict):
            try:
                assigned_to = uuid.UUID(str(assignee.get("user_id")))
            except (TypeError, ValueError):
                assigned_to = None

        specs.append(
            {
                "title": str(raw.get("title", "")),
                "description": str(raw.get("description", "")),
                "assigned_to": assigned_to,
            }
        )

    tasks = await task_service.create_many(
        db,
        workspace_id=workspace_id,
        created_by=action.initiated_by,
        specs=specs,
    )

    audit_service.record(
        db,
        workspace_id=workspace_id,
        action=AuditAction.TASKS_CREATED,
        user_id=action.decided_by,
        details={
            "action_id": str(action.id),
            "task_ids": [str(task.id) for task in tasks],
        },
    )

    return ExecutionOutcome(
        summary=f"Created {len(tasks)} task(s): "
        + ", ".join(task.title for task in tasks)
        + ".",
        details={"task_ids": [str(task.id) for task in tasks]},
    )


async def execute(
    db: AsyncSession,
    mailer: EmailProvider,
    *,
    workspace_id: uuid.UUID,
    action: PendingAction,
    requester: User | None,
) -> ExecutionOutcome:
    """Carry out one approved action. Raises UpstreamFailure if it did not work.

    `requester` is whoever asked for the action, loaded from
    `action.initiated_by` -- used for `Reply-To:` and as the calendar
    organiser. Nullable because a user can be deleted between proposing an
    action and somebody approving it, and that should degrade the headers
    rather than fail the send.
    """
    try:
        if action.type is PendingActionType.SEND_EMAIL:
            return await _execute_send_email(
                db, mailer, workspace_id=workspace_id, action=action, requester=requester
            )

        if action.type is PendingActionType.CREATE_EVENT:
            return await _execute_create_event(
                db, mailer, workspace_id=workspace_id, action=action, requester=requester
            )

        return await _execute_create_tasks(db, workspace_id=workspace_id, action=action)

    except AppError:
        raise
    except Exception as exc:  # noqa: BLE001
        # Anything unforeseen becomes a failure of *this action*, not a 500
        # for the whole request. The approval still stands and the caller is
        # told the side effect did not happen, which is the honest answer and
        # the one that makes a retry sensible.
        logger.exception(
            "an approved action failed to execute",
            extra={"action_id": str(action.id), "type": action.type.value},
        )
        raise UpstreamFailure(
            f"This action could not be carried out: {type(exc).__name__}"
        ) from exc
