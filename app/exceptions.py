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


class RateLimited(AppError):
    status_code = 429
    detail = "Rate limited"
