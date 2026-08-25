"""Step 7: calendar events and the `.ics` invitation.

Half of this file tests a string format, which is unusual and deliberate. The
`.ics` writer is hand-rolled (see app/services/calendar_service.py for why), so
the three things a library would have handled -- escaping, octet-based folding,
and a UID that stays put -- are the three things worth asserting. Getting any
of them wrong produces a file that opens fine in one calendar client and is
silently rejected by another, which is the worst possible failure mode.
"""

import uuid
from datetime import UTC, datetime

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.tools.resolve import ResolvedMember
from app.constants import ICS_FOLD_OCTETS
from app.services import calendar_service
from app.tests.factories import (
    auth_headers,
    make_calendar_event,
    make_member,
    make_user,
    make_workspace,
    random_email,
)

START = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)
END = datetime(2026, 9, 1, 11, 0, tzinfo=UTC)


def _person(name: str, email: str) -> ResolvedMember:
    return ResolvedMember(user_id=uuid.uuid4(), name=name, email=email)


def _unfold(ics: str) -> str:
    """Undo RFC 5545 line folding, so a test can look for a whole value."""
    return ics.replace("\r\n ", "")


# --------------------------------------------------------------------------
# The file format.
# --------------------------------------------------------------------------


def test_the_ics_has_the_structure_a_calendar_client_expects() -> None:
    ics = calendar_service.build_ics(
        uid="abc@ai-workspace-assistant",
        title="Quarterly review",
        description="",
        start_time=START,
        end_time=END,
        organiser=_person("Alice Smith", "alice@example.com"),
        guests=[_person("Bob Jones", "bob@example.com")],
        created_at=START,
    )

    assert ics.startswith("BEGIN:VCALENDAR\r\n")
    assert ics.endswith("END:VCALENDAR\r\n"), "RFC 5545 requires the final CRLF"
    # METHOD:REQUEST is what makes a client offer Accept/Decline rather than
    # silently filing the event.
    assert "METHOD:REQUEST" in ics
    assert "UID:abc@ai-workspace-assistant" in ics
    assert "DTSTART:20260901T100000Z" in ics
    assert "DTEND:20260901T110000Z" in ics
    assert "ORGANIZER;CN=Alice Smith:mailto:alice@example.com" in ics
    assert "mailto:bob@example.com" in _unfold(ics)
    # Every line ends CRLF, and none is left bare.
    assert "\n" not in ics.replace("\r\n", "")


def test_a_naive_timestamp_is_treated_as_utc() -> None:
    """A datetime with no tzinfo would otherwise render in whatever the server's
    local time happens to be, which on Render is not the same as on a laptop."""
    ics = calendar_service.build_ics(
        uid="u@x",
        title="Standup",
        description="",
        start_time=datetime(2026, 9, 1, 10, 0),
        end_time=datetime(2026, 9, 1, 11, 0),
        organiser=None,
        guests=[],
        created_at=START,
    )

    assert "DTSTART:20260901T100000Z" in ics


def test_special_characters_are_escaped_not_dropped() -> None:
    """Commas, semicolons, backslashes and newlines are structural in RFC 5545.

    An unescaped comma in a summary does not merely look wrong -- it splits the
    value into a list, and the rest of the title vanishes.
    """
    ics = calendar_service.build_ics(
        uid="u@x",
        title="Review; budget, forecast \\ notes",
        description="Line one\nLine two",
        start_time=START,
        end_time=END,
        organiser=None,
        guests=[],
        created_at=START,
    )
    unfolded = _unfold(ics)

    assert "SUMMARY:Review\\; budget\\, forecast \\\\ notes" in unfolded
    assert "DESCRIPTION:Line one\\nLine two" in unfolded
    # The literal newline must not survive into the output as a line break.
    assert "Line one\r\nLine two" not in ics


def test_long_lines_are_folded_at_seventy_five_octets() -> None:
    ics = calendar_service.build_ics(
        uid="u@x",
        title="A very long event title " * 8,
        description="",
        start_time=START,
        end_time=END,
        organiser=None,
        guests=[],
        created_at=START,
    )

    for line in ics.split("\r\n"):
        assert len(line.encode("utf-8")) <= ICS_FOLD_OCTETS, f"unfolded line: {line!r}"

    # Folding is reversible: the whole title is still there once unfolded.
    assert "A very long event title " * 8 in _unfold(ics).replace("SUMMARY:", "")


def test_folding_counts_octets_rather_than_characters() -> None:
    """The bug this guards: a title of accented characters is under 75
    *characters* and over 75 *octets*, so a character-counting fold emits a
    line that is illegal -- and a fold placed mid-sequence corrupts the UTF-8.
    """
    ics = calendar_service.build_ics(
        uid="u@x",
        title="é" * 70,
        description="",
        start_time=START,
        end_time=END,
        organiser=None,
        guests=[],
        created_at=START,
    )

    for line in ics.split("\r\n"):
        assert len(line.encode("utf-8")) <= ICS_FOLD_OCTETS

    assert "é" * 70 in _unfold(ics)
    # Round-trips: nothing was split inside a multi-byte sequence.
    assert ics.encode("utf-8").decode("utf-8") == ics


# --------------------------------------------------------------------------
# The routes.
# --------------------------------------------------------------------------


async def test_creating_an_event_resolves_guests_server_side(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    """The caller names people. The server decides what their addresses are --
    the same rule as email recipients (SPEC-v2 D21), applied to a route a human
    drives rather than the agent."""
    admin = await make_user(db_session, email=random_email(), name="Alice Smith")
    colleague = await make_user(db_session, email=random_email(), name="Bob Jones")
    workspace = await make_workspace(db_session, owner=admin)
    await make_member(db_session, workspace=workspace, user=colleague)

    response = await client.post(
        f"/workspaces/{workspace.id}/events",
        json={
            "title": "Quarterly review",
            "start_time": START.isoformat(),
            "end_time": END.isoformat(),
            "guests": ["Bob Jones"],
        },
        headers=auth_headers(admin),
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["guests"] == [
        {"user_id": str(colleague.id), "name": "Bob Jones", "email": colleague.email}
    ]
    assert body["ics_uid"]
    assert body["external_event_id"] is None, "the .ics path publishes nowhere external"


async def test_a_guest_outside_the_workspace_is_refused(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    admin = await make_user(db_session, email=random_email(), name="Alice Smith")
    workspace = await make_workspace(db_session, owner=admin)

    response = await client.post(
        f"/workspaces/{workspace.id}/events",
        json={
            "title": "Exfiltration",
            "start_time": START.isoformat(),
            "end_time": END.isoformat(),
            "guests": ["attacker@evil.test"],
        },
        headers=auth_headers(admin),
    )

    assert response.status_code == 404
    listed = await client.get(
        f"/workspaces/{workspace.id}/events", headers=auth_headers(admin)
    )
    assert listed.json() == [], "a refused guest must not leave a half-created event"


async def test_an_event_must_end_after_it_starts(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    admin = await make_user(db_session, email=random_email())
    workspace = await make_workspace(db_session, owner=admin)

    response = await client.post(
        f"/workspaces/{workspace.id}/events",
        json={
            "title": "Backwards",
            "start_time": END.isoformat(),
            "end_time": START.isoformat(),
            "guests": [],
        },
        headers=auth_headers(admin),
    )

    assert response.status_code == 409


async def test_the_ics_downloads_as_a_file(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    admin = await make_user(db_session, email=random_email(), name="Alice Smith")
    guest = await make_user(db_session, email=random_email(), name="Bob Jones")
    workspace = await make_workspace(db_session, owner=admin)
    await make_member(db_session, workspace=workspace, user=guest)
    event = await make_calendar_event(
        db_session, workspace=workspace, created_by=admin, guests=[guest]
    )

    response = await client.get(
        f"/workspaces/{workspace.id}/events/{event.id}/ics", headers=auth_headers(admin)
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/calendar")
    assert f"event-{event.id}.ics" in response.headers["content-disposition"]

    body = response.text
    assert f"UID:{event.ics_uid}" in body
    assert f"mailto:{guest.email}" in _unfold(body)
    assert f"ORGANIZER;CN=Alice Smith:mailto:{admin.email}" in _unfold(body)


async def test_the_downloaded_invitation_names_who_was_invited_not_who_is_a_member_now(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    """The guest list is a snapshot, like D5's quoted_text on a citation.

    Re-resolving it at download time would quietly rewrite history: an
    invitation downloaded next month would list today's membership rather than
    the people the invitation actually went to.
    """
    admin = await make_user(db_session, email=random_email(), name="Alice Smith")
    guest = await make_user(db_session, email=random_email(), name="Bob Jones")
    workspace = await make_workspace(db_session, owner=admin)
    membership = await make_member(db_session, workspace=workspace, user=guest)
    event = await make_calendar_event(
        db_session, workspace=workspace, created_by=admin, guests=[guest]
    )

    # Bob leaves the workspace.
    await db_session.delete(membership)
    await db_session.commit()

    response = await client.get(
        f"/workspaces/{workspace.id}/events/{event.id}/ics", headers=auth_headers(admin)
    )

    assert response.status_code == 200
    assert f"mailto:{guest.email}" in _unfold(response.text)


async def test_the_ics_uid_does_not_change_between_downloads(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    """A calendar client uses the UID to recognise a later invitation as an
    update to this event. A UID derived from mutable fields would turn every
    edit into a second entry in everybody's diary."""
    admin = await make_user(db_session, email=random_email(), name="Alice Smith")
    workspace = await make_workspace(db_session, owner=admin)
    event = await make_calendar_event(db_session, workspace=workspace, created_by=admin)

    first = await client.get(
        f"/workspaces/{workspace.id}/events/{event.id}/ics", headers=auth_headers(admin)
    )
    second = await client.get(
        f"/workspaces/{workspace.id}/events/{event.id}/ics", headers=auth_headers(admin)
    )

    assert f"UID:{event.ics_uid}" in first.text
    assert first.text == second.text


async def test_an_event_from_another_workspace_is_404(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    admin_a = await make_user(db_session, email=random_email())
    workspace_a = await make_workspace(db_session, owner=admin_a)
    event_a = await make_calendar_event(db_session, workspace=workspace_a, created_by=admin_a)

    admin_b = await make_user(db_session, email=random_email())
    workspace_b = await make_workspace(db_session, owner=admin_b, name="Other")

    response = await client.get(
        f"/workspaces/{workspace_b.id}/events/{event_a.id}/ics",
        headers=auth_headers(admin_b),
    )

    assert response.status_code == 404
