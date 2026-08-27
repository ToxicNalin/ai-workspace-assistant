"""Request identity reaching the log line, which is the only reason it exists.

The middleware is easy to write and easy to have quietly not work: context
variables are copied into a task at the moment it is spawned, and Starlette
runs the endpoint in a task of its own. If that copy did not carry the values,
every assertion here would still pass at the middleware layer and every log
line from inside a route would come out anonymous -- which is exactly the case
you need it for.
"""

import json
import logging
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import Headers
from starlette.requests import Request

from app.main import app
from app.middleware.request_context import (
    REQUEST_ID_HEADER,
    user_from_token,
    workspace_from_path,
)
from app.tests.factories import auth_headers, make_user, random_email
from app.utils.context import (
    current_context,
    request_id_var,
    user_id_var,
    workspace_id_var,
)
from app.utils.logger import JsonFormatter

PROBE_PATH = "/workspaces/{workspace_id}/_probe_context"


@pytest.fixture
def probe() -> Iterator[None]:
    """A throwaway route that reports the context it can see.

    Mounted for one test rather than shipped, because the property under test
    is about every route and a permanent endpoint that echoes request context
    is a thing to have to remember to protect.
    """

    async def read_context(workspace_id: uuid.UUID) -> dict[str, str]:
        return current_context()

    app.add_api_route(PROBE_PATH, read_context, methods=["GET"])
    try:
        yield
    finally:
        app.router.routes = [
            route
            for route in app.router.routes
            if getattr(route, "path", None) != PROBE_PATH
        ]


async def test_the_context_reaches_inside_the_route(
    db_session: AsyncSession, client: AsyncClient, probe: None
) -> None:
    user = await make_user(db_session, email=random_email())
    workspace_id = uuid.uuid4()

    response = await client.get(
        f"/workspaces/{workspace_id}/_probe_context", headers=auth_headers(user)
    )

    assert response.status_code == 200
    body = response.json()
    assert body["request_id"]
    # Read off the path and out of the token, without either the route or
    # anything it calls having been written to know about them.
    assert body["workspace_id"] == str(workspace_id)
    assert body["user_id"] == str(user.id)


async def test_an_inbound_request_id_is_kept(client: AsyncClient, probe: None) -> None:
    """So one request can be followed across the frontend and the API rather
    than becoming two unrelated ids at the boundary."""
    response = await client.get(
        f"/workspaces/{uuid.uuid4()}/_probe_context",
        headers={REQUEST_ID_HEADER: "abc-123"},
    )

    assert response.json()["request_id"] == "abc-123"
    assert response.headers[REQUEST_ID_HEADER] == "abc-123"


async def test_every_response_carries_a_request_id(client: AsyncClient) -> None:
    """Including ones that never reach a route -- /health does not, and it is
    the endpoint most likely to be the only thing in a log during an
    incident."""
    response = await client.get("/health")

    assert response.headers[REQUEST_ID_HEADER]


async def test_an_unauthenticated_request_has_no_user(
    client: AsyncClient, probe: None
) -> None:
    response = await client.get(f"/workspaces/{uuid.uuid4()}/_probe_context")

    assert "user_id" not in response.json()


async def test_context_does_not_leak_between_requests(
    db_session: AsyncSession, client: AsyncClient, probe: None
) -> None:
    """The reset in the middleware's finally block, tested.

    Without it a task whose context is reused would carry the previous
    request's workspace id, and the log would attribute one tenant's activity
    to another -- a quiet, plausible-looking lie.
    """
    user = await make_user(db_session, email=random_email())
    first = uuid.uuid4()

    await client.get(f"/workspaces/{first}/_probe_context", headers=auth_headers(user))
    after = await client.get(f"/workspaces/{uuid.uuid4()}/_probe_context")

    assert after.json().get("workspace_id") != str(first)
    assert "user_id" not in after.json()


def test_the_formatter_renders_whatever_is_set() -> None:
    request_token = request_id_var.set("req-1")
    user_token = user_id_var.set("user-1")
    workspace_token = workspace_id_var.set("ws-1")

    try:
        record = logging.LogRecord(
            name="app.test",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="something happened",
            args=(),
            exc_info=None,
        )
        payload: dict[str, Any] = json.loads(JsonFormatter().format(record))
    finally:
        request_id_var.reset(request_token)
        user_id_var.reset(user_token)
        workspace_id_var.reset(workspace_token)

    assert payload["request_id"] == "req-1"
    assert payload["user_id"] == "user-1"
    assert payload["workspace_id"] == "ws-1"
    assert payload["message"] == "something happened"


def test_the_formatter_omits_what_is_not_set() -> None:
    """A background job genuinely has no request id, and a field that is
    present and null invites a search that matches every one of them."""
    record = logging.LogRecord(
        name="app.workers",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="ingesting",
        args=(),
        exc_info=None,
    )

    payload = json.loads(JsonFormatter().format(record))

    assert "request_id" not in payload
    assert "workspace_id" not in payload


def test_workspace_is_read_from_the_path_only_when_there_is_one() -> None:
    workspace_id = uuid.uuid4()

    assert workspace_from_path(f"/workspaces/{workspace_id}/documents") == str(workspace_id)
    assert workspace_from_path("/workspaces") is None
    assert workspace_from_path("/auth/login") is None
    # Not a uuid, so not a workspace id -- guessing here would put junk in
    # every log line for the route.
    assert workspace_from_path("/workspaces/not-a-uuid/documents") is None


def test_a_bad_token_names_nobody() -> None:
    """A forged token must not put a chosen id in the logs. Nothing downstream
    trusts this value, but a log that can be written by the caller is worse
    than one that is empty."""

    def build(authorization: str | None) -> Request:
        headers = {"authorization": authorization} if authorization else {}
        return Request(
            {"type": "http", "headers": Headers(headers).raw, "client": ("127.0.0.1", 1)}
        )

    assert user_from_token(build(None)) is None
    assert user_from_token(build("Bearer not-a-jwt")) is None
    assert user_from_token(build("Basic abc")) is None
