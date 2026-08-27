"""The usage views, and who is allowed to read them.

These are the only routes in the application whose whole purpose is to report
on other people, so the interesting assertions are as much about the reader as
the numbers: a member must not be able to see what their colleagues have spent,
and neither must anybody in another workspace.
"""

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants import UsageKind, WorkspaceRole
from app.tests.factories import (
    auth_headers,
    make_member,
    make_usage_event,
    make_user,
    make_workspace,
    random_email,
)


async def test_usage_totals_what_the_workspace_spent(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    admin = await make_user(db_session, email=random_email())
    workspace = await make_workspace(db_session, owner=admin)
    await make_usage_event(
        db_session,
        workspace=workspace,
        user=admin,
        kind=UsageKind.CHAT,
        tokens_in=100,
        tokens_out=50,
    )
    await make_usage_event(
        db_session,
        workspace=workspace,
        user=admin,
        kind=UsageKind.EMBEDDING,
        tokens_in=20,
        tokens_out=0,
    )

    response = await client.get(
        f"/workspaces/{workspace.id}/admin/usage", headers=auth_headers(admin)
    )

    assert response.status_code == 200
    body = response.json()
    assert body["calls"] == 2
    assert body["tokens_in"] == 120
    assert body["tokens_out"] == 50
    # Broken down by what spent it, because "we used 170 tokens" and "150 of
    # them were the model and 20 were embeddings" answer different questions.
    assert body["by_kind"] == {"chat": 150, "embedding": 20}


async def test_usage_says_how_much_of_its_own_total_is_a_guess(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    """Embeddings never report token counts -- the API returns vectors -- so
    part of every workspace's ledger is a characters-per-token estimate.
    Presenting the total without saying so would imply a precision the number
    does not have."""
    admin = await make_user(db_session, email=random_email())
    workspace = await make_workspace(db_session, owner=admin)
    await make_usage_event(
        db_session,
        workspace=workspace,
        user=admin,
        kind=UsageKind.CHAT,
        tokens_in=800,
        tokens_out=200,
        estimated=False,
    )
    await make_usage_event(
        db_session,
        workspace=workspace,
        user=admin,
        kind=UsageKind.EMBEDDING,
        tokens_in=60,
        tokens_out=0,
        estimated=True,
    )

    body = (
        await client.get(
            f"/workspaces/{workspace.id}/admin/usage", headers=auth_headers(admin)
        )
    ).json()

    assert body["tokens_in"] + body["tokens_out"] == 1_060
    assert body["tokens_estimated"] == 60


async def test_usage_reports_what_is_left_of_todays_allowance(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    """The question an admin actually has is "are we about to be cut off"."""
    admin = await make_user(db_session, email=random_email())
    workspace = await make_workspace(db_session, owner=admin)
    await make_usage_event(
        db_session, workspace=workspace, user=admin, tokens_in=400, tokens_out=100
    )

    body = (
        await client.get(
            f"/workspaces/{workspace.id}/admin/usage", headers=auth_headers(admin)
        )
    ).json()

    assert body["tokens_used_today"] == 500
    assert body["tokens_remaining_today"] == body["daily_budget"] - 500


async def test_usage_counts_only_this_workspace(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    admin = await make_user(db_session, email=random_email())
    mine = await make_workspace(db_session, owner=admin, name="Mine")
    theirs = await make_workspace(db_session, owner=admin, name="Theirs")
    await make_usage_event(
        db_session, workspace=theirs, user=admin, tokens_in=9_999, tokens_out=9_999
    )

    body = (
        await client.get(
            f"/workspaces/{mine.id}/admin/usage", headers=auth_headers(admin)
        )
    ).json()

    assert body["calls"] == 0
    assert body["tokens_in"] == 0


async def test_users_lists_every_member_including_the_silent_ones(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    """An outer join, deliberately.

    An inner join would answer "who has spent tokens" while appearing to
    answer "who is in this workspace", and a member who has never asked
    anything would look like a missing one.
    """
    admin = await make_user(db_session, email=random_email(), name="Admin")
    quiet = await make_user(db_session, email=random_email(), name="Quiet")
    workspace = await make_workspace(db_session, owner=admin)
    await make_member(db_session, workspace=workspace, user=quiet)
    await make_usage_event(
        db_session, workspace=workspace, user=admin, tokens_in=30, tokens_out=70
    )

    response = await client.get(
        f"/workspaces/{workspace.id}/admin/users", headers=auth_headers(admin)
    )

    assert response.status_code == 200
    by_name = {row["name"]: row for row in response.json()}
    assert set(by_name) == {"Admin", "Quiet"}
    assert by_name["Admin"]["tokens_in"] == 30
    assert by_name["Admin"]["tokens_out"] == 70
    assert by_name["Admin"]["calls"] == 1
    assert by_name["Quiet"]["calls"] == 0
    assert by_name["Quiet"]["tokens_in"] == 0


async def test_users_does_not_attribute_another_workspaces_spend(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    """The same person is often in two workspaces. Their spend in one must not
    appear against them in the other."""
    admin = await make_user(db_session, email=random_email(), name="Admin")
    here = await make_workspace(db_session, owner=admin, name="Here")
    elsewhere = await make_workspace(db_session, owner=admin, name="Elsewhere")
    await make_usage_event(
        db_session, workspace=elsewhere, user=admin, tokens_in=5_000, tokens_out=5_000
    )

    rows = (
        await client.get(
            f"/workspaces/{here.id}/admin/users", headers=auth_headers(admin)
        )
    ).json()

    assert rows[0]["tokens_in"] == 0
    assert rows[0]["tokens_out"] == 0


async def test_a_member_cannot_read_what_their_colleagues_spent(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    """403, not 404: this member is genuinely in the workspace and the
    workspace genuinely exists. Hiding that would be the wrong lie -- 404 is
    for a boundary they are not supposed to know they are standing at."""
    admin = await make_user(db_session, email=random_email())
    member = await make_user(db_session, email=random_email())
    workspace = await make_workspace(db_session, owner=admin)
    await make_member(
        db_session, workspace=workspace, user=member, role=WorkspaceRole.MEMBER
    )

    usage = await client.get(
        f"/workspaces/{workspace.id}/admin/usage", headers=auth_headers(member)
    )
    users = await client.get(
        f"/workspaces/{workspace.id}/admin/users", headers=auth_headers(member)
    )

    assert usage.status_code == 403
    assert users.status_code == 403


async def test_the_reporting_window_is_bounded(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    """Reporting over all time would get slower every week to answer a
    question nobody asks, on a database capped at 0.5 GB."""
    admin = await make_user(db_session, email=random_email())
    workspace = await make_workspace(db_session, owner=admin)

    too_wide = await client.get(
        f"/workspaces/{workspace.id}/admin/usage?days=365", headers=auth_headers(admin)
    )
    fine = await client.get(
        f"/workspaces/{workspace.id}/admin/usage?days=30", headers=auth_headers(admin)
    )

    assert too_wide.status_code == 422
    assert fine.status_code == 200
    assert fine.json()["days"] == 30
