"""Request identity, carried into every log line without being passed around.

The alternative is threading a request id through every service signature so
that logging can mention it, which pollutes the whole call graph to serve one
cross-cutting concern. Context variables are the right tool: asyncio gives each
task its own copy, so two concurrent requests cannot read each other's values,
and app/utils/logger.py reads them at format time rather than at call time.

What this buys, concretely: a 500 in production arrives with the request that
caused it, who was making it and which workspace they were in -- without any
of the code that raised it having been written to know that.
"""

import re
import uuid
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.auth.jwt import decode_access_token
from app.exceptions import Unauthorized
from app.utils.context import request_id_var, user_id_var, workspace_id_var

REQUEST_ID_HEADER = "X-Request-ID"

# Every tenant-scoped route is /workspaces/{uuid}/..., so the path is a
# reliable source for this and needs no cooperation from the route itself.
_WORKSPACE_PATH = re.compile(
    r"/workspaces/([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
    r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
)


def workspace_from_path(path: str) -> str | None:
    match = _WORKSPACE_PATH.search(path)
    return match.group(1) if match else None


def user_from_token(request: Request) -> str | None:
    """Decoded, never looked up.

    This runs on every request including unauthenticated ones, so it must not
    touch the database. A forged token would put a false id in the logs, which
    is why nothing downstream trusts this value for anything -- it is for
    reading logs, not for authorisation. get_current_user does that properly,
    against the database, inside the route.
    """
    authorization = request.headers.get("authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None

    try:
        subject = decode_access_token(token).get("sub")
    except Unauthorized:
        return None

    return str(subject) if subject else None


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        # An inbound id is honoured so one request can be followed across the
        # frontend and the API; one is minted when the caller supplied none.
        request_id = request.headers.get(REQUEST_ID_HEADER) or uuid.uuid4().hex

        request_token = request_id_var.set(request_id)
        user_token = user_id_var.set(user_from_token(request))
        workspace_token = workspace_id_var.set(workspace_from_path(request.url.path))

        try:
            response = await call_next(request)
            response.headers[REQUEST_ID_HEADER] = request_id
            return response
        finally:
            # Reset rather than set-to-None: this runs on a task whose context
            # may be reused, and a stale workspace id left behind would
            # mislabel whatever ran next.
            request_id_var.reset(request_token)
            user_id_var.reset(user_token)
            workspace_id_var.reset(workspace_token)
