class AppError(Exception):
    status_code: int = 500
    detail: str = "Internal server error"

    def __init__(self, detail: str | None = None) -> None:
        if detail is not None:
            self.detail = detail
        super().__init__(self.detail)


class Unauthorized(AppError):
    status_code = 401
    detail = "Not authenticated"


class NotFound(AppError):
    status_code = 404
    detail = "Not found"


class Forbidden(AppError):
    """A same-workspace authorisation failure (e.g. insufficient role).

    Never raise this for a resource in another workspace — that must be
    NotFound. A 403 confirms the resource exists, which leaks tenant
    structure across a workspace boundary.
    """

    status_code = 403
    detail = "Forbidden"


class Conflict(AppError):
    status_code = 409
    detail = "Conflict"


class UnsupportedMediaType(AppError):
    status_code = 415
    detail = "Unsupported or corrupted file type"


class PayloadTooLarge(AppError):
    status_code = 413
    detail = "File exceeds the maximum upload size"


class RateLimited(AppError):
    status_code = 429
    detail = "Rate limited"


class UpstreamFailure(AppError):
    """A third-party service failed while we were carrying out an approved action.

    Distinct from a 500: nothing in this application is broken. The email
    provider was unreachable, or refused the message. The distinction matters
    to whoever approved the action -- their decision stands, the side effect
    did not happen, and retrying is a reasonable thing to do.
    """

    status_code = 502
    detail = "An external service failed while carrying out this action"
