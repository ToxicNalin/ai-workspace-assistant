"""The two limits that stand between a public demo and a bill.

They are different tools for different jobs and the tests are grouped that way.
Requests per minute bounds how often somebody can ask. The daily token budget
bounds what those questions cost, and it is only worth anything if it is
consulted before the model runs -- so the assertions below do not merely check
for a 429, they check that the model was never reached.
"""

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.chat_model import Completion, Usage
from app.config import get_settings
from app.constants import UsageKind
from app.database.models.usage_event import UsageEvent
from app.middleware.rate_limit import client_address, window_start
from app.tests.factories import (
    auth_headers,
    make_indexed_document,
    make_usage_event,
    make_user,
    make_workspace,
    random_email,
)


@pytest.fixture
def limits(monkeypatch: pytest.MonkeyPatch) -> None:
    """Switch the request limiter on for one test, at a countable size.

    conftest disables it for the suite: every test shares one client address,
    so a per-IP limit left on would make each test's result depend on how many
    ran before it. Three is small enough to exhaust in a loop that reads
    clearly.
    """
    settings = get_settings()
    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    monkeypatch.setattr(settings, "rate_limit_requests_per_minute", 3)
    monkeypatch.setattr(settings, "rate_limit_registrations_per_hour", 2)


class ModelThatMustNotRun:
    """A chat model that fails the test if anything calls it.

    The point of a budget check is what does *not* happen. Asserting on a 429
    alone would pass just as well for an implementation that called Gemini,
    paid for the tokens, and then noticed.
    """

    @property
    def model_name(self) -> str:
        return "must-not-run"

    async def complete(self, *, system: str, user: str) -> Completion:
        raise AssertionError("the model was invoked despite the budget being spent")

    def stream(self, *, system: str, user: str) -> object:
        raise AssertionError("the model was invoked despite the budget being spent")


# --- request limits -------------------------------------------------------


async def test_limit_trips_on_the_request_after_the_allowance(
    db_session: AsyncSession, client: AsyncClient, limits: None
) -> None:
    user = await make_user(db_session, email=random_email())
    workspace = await make_workspace(db_session, owner=user)
    headers = auth_headers(user)
    path = f"/workspaces/{workspace.id}/members"

    for attempt in range(3):
        allowed = await client.get(path, headers=headers)
        assert allowed.status_code == 200, f"request {attempt + 1} of 3 was rejected"

    refused = await client.get(path, headers=headers)

    assert refused.status_code == 429
    # Without this a client has to guess, and a client that guesses either
    # hammers the endpoint or gives up on a limit that has already lifted.
    assert int(refused.headers["Retry-After"]) > 0


async def test_the_allowance_is_per_user_not_global(
    db_session: AsyncSession, client: AsyncClient, limits: None
) -> None:
    """One busy user must not lock everybody else out.

    A limiter keyed on nothing is a denial-of-service anyone can trigger on
    everyone's behalf, which is worse than the abuse it was added to stop.
    """
    noisy = await make_user(db_session, email=random_email())
    quiet = await make_user(db_session, email=random_email())
    noisy_workspace = await make_workspace(db_session, owner=noisy)
    quiet_workspace = await make_workspace(db_session, owner=quiet)

    for _ in range(4):
        await client.get(
            f"/workspaces/{noisy_workspace.id}/members", headers=auth_headers(noisy)
        )

    response = await client.get(
        f"/workspaces/{quiet_workspace.id}/members", headers=auth_headers(quiet)
    )

    assert response.status_code == 200


async def test_health_is_never_limited(client: AsyncClient, limits: None) -> None:
    """/health is pinged every ten minutes to keep Render warm.

    Limiting it would need a Postgres round trip per ping, which is exactly
    the compute-hour burn SPEC-v2 §7 warns against -- and a keep-warm cron
    that starts getting 429s stops keeping anything warm.
    """
    for _ in range(6):
        response = await client.get("/health")
        assert response.status_code == 200


# An address this test owns. Unauthenticated requests are bucketed by client
# address, and the test client's is 127.0.0.1 -- the same bucket as a locally
# running instance of the app, or anything else on the machine talking to it.
# Counters are committed, so a developer with `uvicorn` open in another
# terminal would watch this test fail on its first request for reasons that
# have nothing to do with the code. Claiming an address via X-Forwarded-For is
# how the test below already reaches a bucket of its own; `client_address`
# reads the *last* entry, which is the one a proxy appends.
OWN_ADDRESS = {"X-Forwarded-For": "192.0.2.1, 198.51.100.4"}


async def test_registration_is_capped_separately_from_ordinary_requests(
    client: AsyncClient, limits: None
) -> None:
    """Sixty requests a minute must not mean sixty accounts a minute."""
    for attempt in range(2):
        created = await client.post(
            "/auth/register",
            json={"email": random_email(), "password": "password123", "name": "A"},
            headers=OWN_ADDRESS,
        )
        assert created.status_code == 201, f"registration {attempt + 1} of 2 was rejected"

    refused = await client.post(
        "/auth/register",
        json={"email": random_email(), "password": "password123", "name": "A"},
        headers=OWN_ADDRESS,
    )

    assert refused.status_code == 429
    # "accounts", not merely 429: three requests is within the per-minute
    # allowance, so a 429 here has to be the registration cap rather than the
    # general one, or the test would pass without proving the two are separate.
    assert "accounts" in refused.json()["detail"].lower()


async def test_a_forged_forwarded_for_cannot_escape_the_bucket(
    client: AsyncClient, limits: None
) -> None:
    """The client-supplied entry is first; the one Render appends is last.

    Reading the first would let anyone rotate a header value and have an
    unlimited allowance, which is the whole limit gone. Both requests below
    claim a different origin and must still land in the same bucket.
    """
    for _ in range(3):
        await client.post(
            "/auth/register",
            json={"email": random_email(), "password": "password123", "name": "A"},
            headers={"X-Forwarded-For": f"{random_email()}, 203.0.113.7"},
        )

    refused = await client.post(
        "/auth/register",
        json={"email": random_email(), "password": "password123", "name": "A"},
        headers={"X-Forwarded-For": "10.0.0.9, 203.0.113.7"},
    )

    assert refused.status_code == 429


def test_forwarded_for_parsing_prefers_the_proxy_appended_entry() -> None:
    from starlette.datastructures import Headers
    from starlette.requests import Request

    def build(header: str) -> Request:
        scope = {
            "type": "http",
            "headers": Headers({"x-forwarded-for": header}).raw,
            "client": ("127.0.0.1", 1234),
        }
        return Request(scope)

    assert client_address(build("1.1.1.1, 2.2.2.2")) == "2.2.2.2"
    assert client_address(build("  1.1.1.1 ,  2.2.2.2  ")) == "2.2.2.2"
    # A trailing comma must not produce an empty bucket that everyone shares.
    assert client_address(build("1.1.1.1,")) == "1.1.1.1"


def test_windows_floor_to_a_shared_boundary() -> None:
    """Every caller in the same window has to derive the same timestamp, or
    the upsert makes a new row per request and nothing is ever counted."""
    from datetime import UTC, datetime

    first = window_start(datetime(2026, 8, 26, 12, 30, 5, tzinfo=UTC), 60)
    second = window_start(datetime(2026, 8, 26, 12, 30, 59, tzinfo=UTC), 60)
    third = window_start(datetime(2026, 8, 26, 12, 31, 0, tzinfo=UTC), 60)

    assert first == second
    assert third != first


async def test_the_limiter_fails_open_when_the_counter_is_unavailable(
    db_session: AsyncSession,
    client: AsyncClient,
    limits: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Neon scales to zero after five minutes. A limiter that turns a
    transient database error into an outage has caused more downtime than the
    abuse it prevents."""
    from sqlalchemy.exc import OperationalError

    user = await make_user(db_session, email=random_email())
    workspace = await make_workspace(db_session, owner=user)

    async def unavailable(*args: object, **kwargs: object) -> tuple[int, int]:
        raise OperationalError("SELECT 1", {}, Exception("connection lost"))

    monkeypatch.setattr("app.middleware.rate_limit.hit", unavailable)

    response = await client.get(
        f"/workspaces/{workspace.id}/members", headers=auth_headers(user)
    )

    assert response.status_code == 200


# --- the token budget -----------------------------------------------------


async def test_budget_exhaustion_blocks_before_the_model_is_invoked(
    db_session: AsyncSession, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "daily_token_budget", 1_000)
    monkeypatch.setattr("app.api.chat.get_chat_model", ModelThatMustNotRun)

    user = await make_user(db_session, email=random_email())
    workspace = await make_workspace(db_session, owner=user)
    await make_indexed_document(
        db_session, workspace=workspace, uploaded_by=user, texts=("Anything at all.",)
    )
    await make_usage_event(
        db_session, workspace=workspace, user=user, tokens_in=600, tokens_out=400
    )

    response = await client.post(
        f"/workspaces/{workspace.id}/chat/query",
        json={"question": "What is in these documents?"},
        headers=auth_headers(user),
    )

    assert response.status_code == 429
    assert int(response.headers["Retry-After"]) > 0
    assert "allowance" in response.json()["detail"].lower()


async def test_the_budget_is_per_workspace(
    db_session: AsyncSession, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One workspace overspending must not silence another.

    The budget is a tenant-scoped quantity like everything else here; a global
    counter would make every workspace share one allowance and let any tenant
    deny service to all the others.
    """
    monkeypatch.setattr(get_settings(), "daily_token_budget", 1_000)

    user = await make_user(db_session, email=random_email())
    spent = await make_workspace(db_session, owner=user, name="Spent")
    solvent = await make_workspace(db_session, owner=user, name="Solvent")
    await make_indexed_document(
        db_session, workspace=solvent, uploaded_by=user, texts=("Leave policy text.",)
    )
    await make_usage_event(
        db_session, workspace=spent, user=user, tokens_in=2_000, tokens_out=0
    )

    response = await client.post(
        f"/workspaces/{solvent.id}/chat/query",
        json={"question": "What is the leave policy?"},
        headers=auth_headers(user),
    )

    assert response.status_code == 200


async def test_an_answer_records_what_it_cost(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    """A budget computed from a ledger nothing writes to is decoration."""
    user = await make_user(db_session, email=random_email())
    workspace = await make_workspace(db_session, owner=user)
    await make_indexed_document(
        db_session,
        workspace=workspace,
        uploaded_by=user,
        texts=("Expenses must be claimed within thirty days.",),
    )

    response = await client.post(
        f"/workspaces/{workspace.id}/chat/query",
        json={"question": "When must expenses be claimed?"},
        headers=auth_headers(user),
    )
    assert response.status_code == 200

    kinds = set(
        (
            await db_session.scalars(
                select(UsageEvent.kind).where(UsageEvent.workspace_id == workspace.id)
            )
        ).all()
    )
    total = await db_session.scalar(
        select(func.sum(UsageEvent.tokens_in + UsageEvent.tokens_out)).where(
            UsageEvent.workspace_id == workspace.id
        )
    )

    # The completion and the query embedding are both real spend, and a
    # ledger that counted only the loud one would under-report every question
    # ever asked.
    assert kinds == {UsageKind.CHAT, UsageKind.EMBEDDING}
    assert (total or 0) > 0


async def test_usage_is_attributed_to_the_person_who_asked(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    user = await make_user(db_session, email=random_email())
    workspace = await make_workspace(db_session, owner=user)
    await make_indexed_document(
        db_session, workspace=workspace, uploaded_by=user, texts=("Some content.",)
    )

    await client.post(
        f"/workspaces/{workspace.id}/chat/query",
        json={"question": "Some content?"},
        headers=auth_headers(user),
    )

    owners = set(
        (
            await db_session.scalars(
                select(UsageEvent.user_id).where(UsageEvent.workspace_id == workspace.id)
            )
        ).all()
    )

    assert owners == {user.id}


def test_estimated_usage_is_flagged_as_such() -> None:
    """"We think this cost 900 tokens" and "this cost 900 tokens" are
    different claims, and an admin page that cannot tell them apart is
    inventing precision it does not have."""
    estimated = Usage.estimate(prompt="a" * 400, completion="b" * 40)
    reported = Usage(tokens_in=100, tokens_out=10)

    assert estimated.estimated is True
    assert estimated.tokens_in == 100
    assert reported.estimated is False
