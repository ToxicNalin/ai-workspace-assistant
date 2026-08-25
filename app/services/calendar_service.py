"""Calendar events, and the `.ics` invitation that carries them.

SPEC-v2 D17: Google Calendar's `calendar.events` is a *sensitive* scope, and
verification has been reported taking five weeks or more. An `.ics` attachment
demonstrates the same agent flow, needs no OAuth from anybody, and works in
every calendar client that exists. So it is the default rather than the
fallback, and the Google path stays behind the same interface, unshipped.

The `.ics` writer here is hand-rolled against RFC 5545. That is a deliberate
choice over a library: the format is a few dozen lines when you only need to
*emit* VEVENT, and the three things that are easy to get wrong -- escaping,
folding and UID stability -- are exactly the things a dependency would hide.
"""

import logging
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.tools.base import ActionRefused
from app.ai.tools.resolve import ResolvedMember, resolve_member
from app.config import get_settings
from app.constants import ICS_FOLD_OCTETS, ICS_PRODID, MAX_EVENT_GUESTS, OAuthService
from app.database.models.calendar_event import CalendarEvent
from app.database.models.oauth_credential import OAuthCredential
from app.exceptions import Conflict, NotFound

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# RFC 5545.
# --------------------------------------------------------------------------


def _escape(value: str) -> str:
    """RFC 5545 section 3.3.11. Backslash first, or it escapes its own escapes."""
    return (
        value.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
        .replace("\r", "\\n")
    )


def _fold(line: str) -> str:
    """RFC 5545 section 3.1: content lines fold at 75 *octets*, not characters.

    Counting characters would produce lines that look legal and are too long
    the moment a subject contains anything non-ASCII, and folding inside a
    UTF-8 sequence would corrupt it -- so this walks encoded lengths and only
    ever breaks on a character boundary.
    """
    if len(line.encode("utf-8")) <= ICS_FOLD_OCTETS:
        return line

    pieces: list[str] = []
    current = ""
    current_octets = 0
    # The first line gets the full width. Every continuation begins with a
    # space, and that space counts towards the limit.
    limit = ICS_FOLD_OCTETS

    for character in line:
        width = len(character.encode("utf-8"))
        if current_octets + width > limit:
            pieces.append(current)
            current, current_octets = "", 0
            limit = ICS_FOLD_OCTETS - 1
        current += character
        current_octets += width

    if current:
        pieces.append(current)

    return "\r\n ".join(pieces)


def _timestamp(moment: datetime) -> str:
    """UTC, in the basic format RFC 5545 calls a UTC date-time."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def build_ics(
    *,
    uid: str,
    title: str,
    description: str,
    start_time: datetime,
    end_time: datetime,
    organiser: ResolvedMember | None,
    guests: Sequence[ResolvedMember],
    created_at: datetime | None = None,
) -> str:
    """Render one VEVENT as a complete iCalendar object.

    METHOD:REQUEST is what makes a client offer "Accept / Decline" rather than
    silently filing the event, which is the whole point of sending it.
    """
    lines: list[str] = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{ICS_PRODID}",
        "CALSCALE:GREGORIAN",
        "METHOD:REQUEST",
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{_timestamp(created_at or datetime.now(UTC))}",
        f"DTSTART:{_timestamp(start_time)}",
        f"DTEND:{_timestamp(end_time)}",
        f"SUMMARY:{_escape(title)}",
    ]

    if description:
        lines.append(f"DESCRIPTION:{_escape(description)}")

    if organiser is not None:
        lines.append(f"ORGANIZER;CN={_escape(organiser.name)}:mailto:{organiser.email}")

    lines += [
        f"ATTENDEE;CN={_escape(guest.name)};ROLE=REQ-PARTICIPANT;"
        f"PARTSTAT=NEEDS-ACTION;RSVP=TRUE:mailto:{guest.email}"
        for guest in guests
    ]

    lines += ["STATUS:CONFIRMED", "END:VEVENT", "END:VCALENDAR"]

    # CRLF throughout, including a trailing one: RFC 5545 section 3.1 requires
    # it, and some clients reject an object whose last line is unterminated.
    return "\r\n".join(_fold(line) for line in lines) + "\r\n"


# --------------------------------------------------------------------------
# Publishing, behind an interface.
# --------------------------------------------------------------------------


class CalendarProvider(Protocol):
    name: str

    async def publish(
        self, db: AsyncSession, event: CalendarEvent, *, organiser_user_id: uuid.UUID | None
    ) -> str | None:
        """Return an external event id, or None if the event lives only here."""
        ...


class IcsCalendarProvider:
    """The shipped path.

    The invitation *is* the `.ics`, which app/services/action_executor.py
    attaches to the notification email -- so there is no external system to
    publish to and no external id to record.
    """

    name = "ics"

    async def publish(
        self, db: AsyncSession, event: CalendarEvent, *, organiser_user_id: uuid.UUID | None
    ) -> str | None:
        return None


class GoogleCalendarProvider:
    """Declared, not shipped -- SPEC-v2 D17 and the non-goals in section 9.

    `calendar.events` is a sensitive scope; verification has been reported
    taking five weeks or more, and testing mode caps you at 100 test users.
    Rather than hardcode a refusal, this looks for the credential a completed
    OAuth flow would have written, so the failure names the piece that is
    actually missing. Adding `app/auth/oauth.py` and filling that table is the
    only work between here and a working Google path.
    """

    name = "google"

    async def publish(
        self, db: AsyncSession, event: CalendarEvent, *, organiser_user_id: uuid.UUID | None
    ) -> str | None:
        credential = None
        if organiser_user_id is not None:
            credential = await db.scalar(
                select(OAuthCredential).where(
                    OAuthCredential.user_id == organiser_user_id,
                    OAuthCredential.service == OAuthService.GOOGLE_CALENDAR,
                )
            )

        if credential is None:
            raise Conflict(
                "Google Calendar is not connected for this user. This deployment ships "
                "the .ics invitation path instead (SPEC-v2 D17)."
            )

        raise Conflict(
            "The Google Calendar path is declared but not implemented in this "
            "deployment. The .ics invitation is the shipped path."
        )


def get_calendar_provider() -> CalendarProvider:
    if get_settings().calendar_provider == "google":
        return GoogleCalendarProvider()
    return IcsCalendarProvider()


# --------------------------------------------------------------------------
# Persistence.
# --------------------------------------------------------------------------


async def list_events(db: AsyncSession, workspace_id: uuid.UUID) -> Sequence[CalendarEvent]:
    result = await db.scalars(
        select(CalendarEvent)
        .where(CalendarEvent.workspace_id == workspace_id)
        .order_by(CalendarEvent.start_time.desc())
    )
    return result.all()


async def get_event(
    db: AsyncSession, workspace_id: uuid.UUID, event_id: uuid.UUID
) -> CalendarEvent:
    event = await db.scalar(
        select(CalendarEvent).where(
            CalendarEvent.id == event_id, CalendarEvent.workspace_id == workspace_id
        )
    )
    if event is None:
        raise NotFound
    return event


async def create_event(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    created_by: uuid.UUID | None,
    title: str,
    description: str = "",
    start_time: datetime,
    end_time: datetime,
    guests: Sequence[ResolvedMember] = (),
) -> CalendarEvent:
    """Insert the event. Does not commit -- the caller owns the transaction, so
    an event and whatever authorised it land together or not at all."""
    if end_time <= start_time:
        raise Conflict("An event must end after it starts")
    if len(guests) > MAX_EVENT_GUESTS:
        raise Conflict(f"An event may not have more than {MAX_EVENT_GUESTS} guests")

    event = CalendarEvent(
        workspace_id=workspace_id,
        title=title,
        description=description,
        start_time=start_time,
        end_time=end_time,
        created_by=created_by,
        # Generated once and stored. A calendar client uses the UID to
        # recognise a later invitation as an update to this event; deriving it
        # from the title or the times would turn every edit into a duplicate
        # in everybody's calendar.
        ics_uid=f"{uuid.uuid4()}@ai-workspace-assistant",
        guests=[
            {"user_id": str(guest.user_id), "name": guest.name, "email": guest.email}
            for guest in guests
        ],
    )
    db.add(event)
    await db.flush()
    return event


def guests_of(event: CalendarEvent) -> list[ResolvedMember]:
    """Read the guest snapshot back as resolved members.

    The stored list is what was resolved from `workspace_members` when the
    invitation went out, and it is deliberately not re-resolved: an `.ics`
    downloaded next month must name the people who were actually invited, not
    whoever happens to be a member now. Same reasoning as D5's `quoted_text`.
    """
    people: list[ResolvedMember] = []
    for guest in event.guests or []:
        try:
            user_id = uuid.UUID(str(guest.get("user_id")))
        except (TypeError, ValueError):
            continue
        people.append(
            ResolvedMember(
                user_id=user_id,
                name=str(guest.get("name", "")),
                email=str(guest.get("email", "")),
            )
        )
    return people


def ics_for_event(event: CalendarEvent, *, organiser: ResolvedMember | None) -> str:
    return build_ics(
        uid=event.ics_uid,
        title=event.title,
        description=event.description,
        start_time=event.start_time,
        end_time=event.end_time,
        organiser=organiser,
        guests=guests_of(event),
        created_at=event.created_at,
    )


async def resolve_guests(
    db: AsyncSession, *, workspace_id: uuid.UUID, references: Sequence[str]
) -> list[ResolvedMember]:
    """Map guest references to members, for the routes a person drives directly.

    Same resolver the agent's proposals go through (SPEC-v2 D21). A human
    naming somebody outside the workspace is a mistake rather than an attack,
    but the answer is identical -- and it is NotFound rather than a 403 for the
    same reason every other cross-tenant answer is: confirming that a
    particular address belongs to a real user elsewhere is itself the leak.
    """
    guests: list[ResolvedMember] = []
    for reference in references:
        try:
            guests.append(
                await resolve_member(db, workspace_id=workspace_id, reference=reference)
            )
        except ActionRefused as exc:
            raise NotFound(str(exc)) from exc
    return guests


async def organiser_of(
    db: AsyncSession, event: CalendarEvent
) -> ResolvedMember | None:
    """The event's creator, as an ORGANIZER line.

    Nullable: `created_by` is ON DELETE SET NULL, so an event can outlive the
    person who made it. An invitation with no organiser is still valid iCalendar.
    """
    if event.created_by is None:
        return None

    from app.database.models.user import User

    user = await db.get(User, event.created_by)
    if user is None:
        return None
    return ResolvedMember(user_id=user.id, name=user.name, email=user.email)


async def ics_for(
    db: AsyncSession, workspace_id: uuid.UUID, event_id: uuid.UUID
) -> tuple[CalendarEvent, str]:
    event = await get_event(db, workspace_id, event_id)
    return event, ics_for_event(event, organiser=await organiser_of(db, event))


async def create_event_for_references(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    created_by: uuid.UUID,
    title: str,
    description: str,
    start_time: datetime,
    end_time: datetime,
    guest_references: Sequence[str],
) -> CalendarEvent:
    """The path behind POST /events: a person creating their own event.

    Deliberately does not email anybody. The approval gate exists because the
    *model* is downstream of untrusted document text, not because sending mail
    is inherently dangerous -- so an approved `create_event` action posts the
    invitation to its guests (see app/services/action_executor.py), while a
    person creating an event here gets a row and an `.ics` to download and
    share as they see fit. Fewer outbound paths, and each one has a person
    behind it who can be named.
    """
    guests = await resolve_guests(db, workspace_id=workspace_id, references=guest_references)
    event = await create_event(
        db,
        workspace_id=workspace_id,
        created_by=created_by,
        title=title,
        description=description,
        start_time=start_time,
        end_time=end_time,
        guests=guests,
    )
    await db.commit()
    await db.refresh(event)
    return event
