"""What happens when the model provider, rather than this application, fails.

SPEC-v2 §7 lists this among the guardrails a public demo needs: "Gemini free
tier has its own rate limits — surface a clean 'busy, try again' rather than a
500." It is the error a reviewer is most likely to trigger, and a 500 tells
them the project is broken when the truth is that the project is free.

So the assertions here are about the *status code and the message*, not merely
that something went wrong. A 429 with `Retry-After` tells a client to wait; a
502 tells it this deployment is fine and the upstream is not; a 500 tells it
neither, and claims a bug that does not exist.
"""

from collections.abc import AsyncIterator

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.chat_model import Completion, StreamChunk, _text_of
from app.ai.upstream import (
    THE_ASSISTANT,
    as_app_error,
    is_rate_limited,
    is_retryable,
    provider_errors,
)
from app.constants import UPSTREAM_BUSY_RETRY_SECONDS
from app.exceptions import NotFound, RateLimited, UpstreamFailure
from app.tests.factories import (
    auth_headers,
    make_indexed_document,
    make_user,
    make_workspace,
    random_email,
)

# Roughly what the Google SDK raises when the free tier's allowance is gone.
# Matched on its text rather than its type on purpose -- see the docstring in
# app/ai/upstream.py for why importing google.api_core to catch this would tie
# the module to the one provider SPEC-v2 D18 exists to keep swappable.
QUOTA_ERROR = Exception(
    "429 Resource has been exhausted (e.g. check quota). "
    "quota_metric: generativelanguage.googleapis.com/generate_content_requests"
)
OUTAGE_ERROR = Exception("503 The service is currently unavailable.")


class _FailingChatModel:
    """A chat model that fails the way a provider does."""

    def __init__(self, error: Exception) -> None:
        self._error = error

    @property
    def model_name(self) -> str:
        return "failing"

    async def complete(self, *, system: str, user: str) -> Completion:
        with provider_errors(THE_ASSISTANT):
            raise self._error

    async def stream(self, *, system: str, user: str) -> AsyncIterator[StreamChunk]:
        with provider_errors(THE_ASSISTANT):
            raise self._error
        yield StreamChunk()  # pragma: no cover -- unreachable, keeps this a generator


# --------------------------------------------------------------------------
# Classification.
# --------------------------------------------------------------------------


def test_a_quota_error_is_a_rate_limit_with_a_retry_after() -> None:
    """Waiting genuinely helps here, so the response has to say how long.

    A 429 with no Retry-After leaves a client to guess, and a client that
    guesses either hammers the endpoint or gives up on an allowance that has
    already come back.
    """
    error = as_app_error(QUOTA_ERROR, what=THE_ASSISTANT)

    assert isinstance(error, RateLimited)
    assert error.status_code == 429
    assert error.retry_after == UPSTREAM_BUSY_RETRY_SECONDS


def test_an_outage_is_an_upstream_failure_not_a_rate_limit() -> None:
    """Different fact, different answer. Nobody's allowance is the problem, so
    telling the caller to wait out a quota would be a lie."""
    error = as_app_error(OUTAGE_ERROR, what=THE_ASSISTANT)

    assert isinstance(error, UpstreamFailure)
    assert error.status_code == 502


def test_an_app_error_passes_through_untouched() -> None:
    """The budget check runs inside the same call path.

    Relabelling its 429 as the provider's fault would blame an upstream for a
    limit this application imposed, and would attach the wrong Retry-After.
    """
    with pytest.raises(NotFound), provider_errors(THE_ASSISTANT):
        raise NotFound


def test_the_provider_message_never_reaches_the_caller() -> None:
    """Provider error text names the model, the project, and occasionally the
    key. It is logged; it is not handed to whoever made the request."""
    error = as_app_error(QUOTA_ERROR, what=THE_ASSISTANT)

    assert "quota_metric" not in error.detail
    assert "generativelanguage" not in error.detail
    assert THE_ASSISTANT in error.detail


@pytest.mark.parametrize(
    ("error", "retryable", "rate_limited"),
    [
        (QUOTA_ERROR, True, True),
        (OUTAGE_ERROR, True, False),
        (Exception("504 Deadline exceeded"), True, False),
        (Exception("Connection reset by peer"), True, False),
        # A bug in this application is not something a retry fixes, and must
        # not be dressed up as somebody else's outage.
        (TypeError("'NoneType' object is not subscriptable"), False, False),
    ],
)
def test_retryable_and_rate_limited_agree_about_what_is_transient(
    error: Exception, retryable: bool, rate_limited: bool
) -> None:
    """The embedder's backoff loop and the status-code classification read the
    same predicate, so they cannot drift into disagreeing."""
    assert is_retryable(error) is retryable
    assert is_rate_limited(error) is rate_limited


def test_a_programming_error_is_still_reported_as_an_upstream_failure() -> None:
    """Non-transient, but it happened behind a provider call.

    502 rather than 500 is the deliberate choice: the caller's useful next
    action is the same either way, and the alternative -- letting arbitrary
    exceptions through as 500s -- is what this wrapper exists to stop.
    """
    error = as_app_error(TypeError("boom"), what=THE_ASSISTANT)

    assert isinstance(error, UpstreamFailure)
    assert "boom" not in error.detail


# --------------------------------------------------------------------------
# Reading a message's text.
# --------------------------------------------------------------------------


class _TextAccessor(str):
    """Stands in for langchain's `TextAccessor`: a `str` that is also callable.

    The callable half exists only for back-compat with the older `.text()`
    method and is deprecated. Reading it as a property has to be preferred, or
    every real provider response takes the deprecated path -- and breaks
    outright when that back-compat is finally removed.
    """

    was_called: bool

    def __new__(cls, value: str) -> "_TextAccessor":
        accessor = super().__new__(cls, value)
        accessor.was_called = False
        return accessor

    def __call__(self) -> str:
        self.was_called = True
        return str(self)


class _Response:
    def __init__(self, text: _TextAccessor) -> None:
        self.text = text
        self.content = [{"type": "text", "text": "block form"}]


def test_message_text_is_read_as_a_property_not_the_deprecated_method() -> None:
    accessor = _TextAccessor("Sent.")

    assert _text_of(_Response(accessor)) == "Sent."
    assert not accessor.was_called, "the deprecated .text() call path was taken"


def test_a_message_with_only_content_blocks_still_yields_text() -> None:
    """Gemini 3.x returns a list of blocks. `str(content)` on one of those is a
    Python repr, which would be persisted and shown to a user verbatim."""

    class _BlocksOnly:
        content = [{"type": "text", "text": "Hello"}, {"type": "text", "text": " there"}]

    assert _text_of(_BlocksOnly()) == "Hello there"


# --------------------------------------------------------------------------
# End to end, through the route that actually answers a person.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "expected_status"),
    [(QUOTA_ERROR, 429), (OUTAGE_ERROR, 502)],
)
async def test_a_provider_failure_is_never_a_500(
    db_session: AsyncSession,
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    expected_status: int,
) -> None:
    monkeypatch.setattr(
        "app.api.chat.get_chat_model", lambda: _FailingChatModel(error)
    )

    user = await make_user(db_session, email=random_email())
    workspace = await make_workspace(db_session, owner=user)
    await make_indexed_document(
        db_session, workspace=workspace, uploaded_by=user, texts=("Anything at all.",)
    )

    response = await client.post(
        f"/workspaces/{workspace.id}/chat/query",
        json={"question": "What is in these documents?"},
        headers=auth_headers(user),
    )

    assert response.status_code == expected_status, response.text
    assert "try again" in response.json()["detail"].lower()


async def test_a_quota_failure_carries_retry_after_over_http(
    db_session: AsyncSession, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The header, not just the status.

    app/middleware/errors.py renders it, and app/main.py exposes it through
    CORS -- a browser client cannot read a header the API has not named.
    """
    monkeypatch.setattr(
        "app.api.chat.get_chat_model", lambda: _FailingChatModel(QUOTA_ERROR)
    )

    user = await make_user(db_session, email=random_email())
    workspace = await make_workspace(db_session, owner=user)
    await make_indexed_document(
        db_session, workspace=workspace, uploaded_by=user, texts=("Anything at all.",)
    )

    response = await client.post(
        f"/workspaces/{workspace.id}/chat/query",
        json={"question": "Anything?"},
        headers=auth_headers(user),
    )

    assert response.status_code == 429
    assert int(response.headers["Retry-After"]) == UPSTREAM_BUSY_RETRY_SECONDS


async def test_the_agent_route_reports_a_provider_failure_the_same_way(
    db_session: AsyncSession, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An agent turn is several provider calls behind one await, so it is the
    most expensive thing here and the likeliest to hit the free tier's limit."""

    class _FailingAgentModel:
        def bind_tools(self, *args: object, **kwargs: object) -> "_FailingAgentModel":
            return self

        async def ainvoke(self, *args: object, **kwargs: object) -> object:
            raise QUOTA_ERROR

    monkeypatch.setattr(
        "app.api.approvals.get_agent_model", lambda: _FailingAgentModel()
    )

    user = await make_user(db_session, email=random_email())
    workspace = await make_workspace(db_session, owner=user)

    response = await client.post(
        f"/workspaces/{workspace.id}/agent",
        json={"message": "email the team a summary"},
        headers=auth_headers(user),
    )

    assert response.status_code == 429, response.text
    assert int(response.headers["Retry-After"]) == UPSTREAM_BUSY_RETRY_SECONDS
