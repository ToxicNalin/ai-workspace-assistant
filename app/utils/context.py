"""The per-request values that every log line should carry.

Deliberately dependency-free. Both the middleware that sets these and the log
formatter that reads them need them, and giving them a home in either one
would make the other import it -- the formatter would end up pulling in JWT
decoding and settings just to render a field.

asyncio gives every task its own copy of a context variable, so two requests
being served concurrently cannot see each other's values.
"""

from contextvars import ContextVar

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
user_id_var: ContextVar[str | None] = ContextVar("user_id", default=None)
workspace_id_var: ContextVar[str | None] = ContextVar("workspace_id", default=None)


def current_context() -> dict[str, str]:
    """Whatever is set, omitting what is not.

    Omitting rather than emitting nulls: a log line from a background job
    genuinely has no request id, and a field that is present and null invites
    a search that matches every one of them.
    """
    values = {
        "request_id": request_id_var.get(),
        "user_id": user_id_var.get(),
        "workspace_id": workspace_id_var.get(),
    }
    return {key: value for key, value in values.items() if value is not None}
